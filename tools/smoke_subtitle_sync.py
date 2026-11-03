#!/usr/bin/env python3
"""字幕与语音同步（字幕不再抢跑 2 秒）—— 静态接线冒烟。

为什么只能是静态断言：
  真正的判据是"老人听到第一个字的那一刻字幕才出现"，这只能在真机上用
  耳朵验证。本机没有 I2S、没有 WS、没有 TTS，能验证的只有**接线**：
  计数有没有加在正确的函数里、门的判据是不是「播放帧数 > 收到字幕时的
  已收帧数」、收尾有没有把残句清掉。

⚠️ 一律**先剥注释、再按函数体定位**（MEMORY.md 第 3 条）：
  本仓踩过"防回归注释自己踩红"和"整文件子串匹配"两种假信号。
  这里的每个断言都限定在**某个函数体内**或**某条消息分支内**，
  不做整文件子串匹配 —— "文件里提到了 SetChatMessage"毫无意义，
  要看的是"sentence_start 分支里还有没有它"。

对应需求（2026-11-03 用户原话）：
  「对话状态下字幕与语音时间差距在2秒以上，要尽量做到同步显示。」
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "firmware" / "xiaozhi" / "main"
AUDIO_H = MAIN / "audio" / "audio_service.h"
AUDIO_CC = MAIN / "audio" / "audio_service.cc"
APP_H = MAIN / "application.h"
APP_CC = MAIN / "application.cc"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


def strip_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，保留字符串字面量（与 smoke_longpress_subtitle 同款）。"""
    out: list[str] = []
    i, n = 0, len(src)
    in_str = False
    quote = ""
    while i < n:
        ch = src[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == quote:
                in_str = False
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def func_body(src: str, signature: str) -> str:
    """取 `signature` 开始的那个函数体（花括号配对），找不到返回 ""。

    为什么必须按函数体取而不是 `src.find("fetch_add")`：
      `received_frames_.fetch_add` 和 `played_frames_.fetch_add` 都在
      audio_service.cc 里，整文件搜到哪一个都"通过"，但它们必须在**两个
      不同的函数**里 —— 放错了就是「字幕永不显示」或「字幕抢跑一帧」，
      而编译一点都不报错。这条断言要挡的正是这种错位。
    """
    i = src.find(signature)
    if i < 0:
        return ""
    j = src.find("{", i)
    if j < 0:
        return ""
    depth = 0
    k = j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j : k + 1]
        k += 1
    return ""


def branch(src: str, key: str, end_key: str) -> str:
    """取协议消息分支：从 `key` 到 `end_key` 之间（含），找不到返回 ""。"""
    i = src.find(key)
    if i < 0:
        return ""
    j = src.find(end_key, i)
    if j < 0:
        return ""
    return src[i:j]


def t1_frame_counters() -> None:
    print("\n── 1. 两个帧计数必须在正确的函数里 ──")
    h = strip_comments(AUDIO_H.read_text(encoding="utf-8"))
    # 空白归一化后再比（声明可能写成 `std::atomic<int> x{0};` 或带空格），
    # 针也要一起去空格 —— 只归一化一边等于永远匹配不上（这次就踩了一次）。
    hn = re.sub(r"\s+", "", h)
    check("std::atomic<int>received_frames_{0};" in hn,
          "received_frames_ 是 std::atomic<int>",
          "普通 int 会被两个任务同时读写 ⇒ 计数漂移，门永远等不到")
    check("std::atomic<int>played_frames_{0};" in hn,
          "played_frames_ 是 std::atomic<int>", "")
    check("int ReceivedFrameCount()" in h and "int PlayedFrameCount()" in h,
          "两个只读访问器都在", "缺了就没法从协议任务里取 mark")

    cc = strip_comments(AUDIO_CC.read_text(encoding="utf-8"))
    push = func_body(cc, "bool AudioService::PushPacketToDecodeQueue")
    check("received_frames_.fetch_add" in push,
          "received_frames_++ 在 PushPacketToDecodeQueue 里",
          "放错函数 ⇒ 收到的帧数永远不变，门要么秒开要么永不开")
    check(push.find("audio_decode_queue_.push_back") <
          push.find("received_frames_.fetch_add"),
          "计数加在 push_back **之后**（只有真进队列的帧才算）",
          "加在前面会把被丢弃的帧也计进去 ⇒ 门偏后")

    out_task = func_body(cc, "void AudioService::AudioOutputTask")
    check("played_frames_.fetch_add" in out_task,
          "played_frames_++ 在 AudioOutputTask 里", "")
    od = out_task.find("codec_->OutputData(task->pcm)")
    pf = out_task.find("played_frames_.fetch_add")
    check(od >= 0 and pf > od,
          "计数加在 OutputData **之后**（写进 codec 才算听到）",
          "加在前面 ⇒ 字幕抢跑一帧（60ms），且队列越深抢得越多")

    # 音频通路硬约束（用户原话：声音已经正常，不许再修出破音/卡顿）：
    # 这次修法刻意**不动**缓冲深度，靠"等播放"而不是"缩缓冲"来对齐。
    m = re.search(r"#define\s+MAX_PLAYBACK_TASKS_IN_QUEUE\s+(\d+)", h)
    check(m is not None and m.group(1) == "16",
          "MAX_PLAYBACK_TASKS_IN_QUEUE 仍是 16（不靠缩缓冲换同步）",
          f"实际 {m.group(1) if m else '找不到'} —— 缩缓冲会重新打开 P2 断续/破音")
    check("SetChatMessage" not in cc,
          "audio_service.cc 里没有任何显示调用",
          "在音频任务里碰显示 = 给播放通路加阻塞风险")


def t2_sentence_start_deferred() -> None:
    print("\n── 2. sentence_start 不再直接上屏 ──")
    cc = strip_comments(APP_CC.read_text(encoding="utf-8"))
    seg = branch(cc, '"sentence_start"', '"stt"')
    check(bool(seg), "找到 sentence_start 分支", "分支被改名/删除")
    check('SetChatMessage("assistant"' not in seg,
          "sentence_start 分支里**没有** SetChatMessage",
          "直接上屏就是老行为：字幕收到即显示，比声音早 1~2 秒")
    check("ReceivedFrameCount()" in seg,
          "分支里取了 ReceivedFrameCount() 当 mark", "")

    # 最关键的一条：mark 必须在 Schedule **之前**取。
    # 放进 lambda 里取 ⇒ 回调跑起来时音频帧可能已经收进来了，门提前打开，
    # 字幕又跑回声音前面 —— 正是这次要修的现象，而且看起来"改过了"。
    rc = seg.find("ReceivedFrameCount()")
    sc = seg.find("Schedule(")
    check(sc >= 0 and rc >= 0 and rc < sc,
          "mark 在 Schedule **之前**取（协议任务里，不是主任务回调里）",
          "在 lambda 里取 ⇒ 门提前打开，字幕仍然抢跑")

    h = strip_comments(APP_H.read_text(encoding="utf-8"))
    check("struct PendingSubtitle" in h and "frame_mark" in h,
          "待上屏字幕带 frame_mark", "")
    check("pending_subtitles_" in h and "subtitle_timer_handle_" in h,
          "有 pending 队列与轮询定时器句柄", "")


def t3_gate_and_teardown() -> None:
    print("\n── 3. 门的判据 + 三种收尾 ──")
    cc = strip_comments(APP_CC.read_text(encoding="utf-8"))
    flush = func_body(cc, "void Application::FlushSubtitles")
    check(bool(flush), "找到 FlushSubtitles", "")
    check("PlayedFrameCount()" in flush, "门读的是 PlayedFrameCount()", "")
    check(re.search(r"played\s*>\s*p\.frame_mark", flush) is not None,
          "判据是 played > frame_mark（严格大于）",
          "写成 >= 会在本句还没出声时就放行")
    check("kSubtitleMaxWaitUs" in flush,
          "有兜底超时（本句没有音频时不至于永不上屏）",
          "TTS 失败/丢帧时字幕会永久卡在队列里")
    check("SetChatMessage(\"assistant\"" in flush,
          "真正上屏只在 FlushSubtitles 里发生（单一出口）", "")

    check("esp_timer_create(&subtitle_timer_args" in cc,
          "轮询定时器被创建", "")
    check("esp_timer_start_periodic(subtitle_timer_handle_" in cc,
          "有 pending 时才启动轮询（空闲不烧 CPU）", "")
    check("esp_timer_delete(subtitle_timer_handle_)" in cc,
          "析构里删了定时器", "")
    check(re.search(r"kSubtitlePollMs\s*=\s*(\d+)", cc) is not None and
          int(re.search(r"kSubtitlePollMs\s*=\s*(\d+)", cc).group(1)) <= 60,
          "轮询周期 ≤ 60ms（一帧时长，误差不可感知）",
          "周期太大 ⇒ 判据成立到真上屏之间肉眼可见")

    start_seg = branch(cc, '"start") == 0', '"stop") == 0')
    check("ClearPendingSubtitles()" in start_seg,
          "tts start 清掉上一轮残句",
          "不清 ⇒ 上一轮没等到音频的半截话会冒在新一轮开头")
    stop_seg = branch(cc, '"stop") == 0', '"sentence_start"')
    check("FlushSubtitles(true)" in stop_seg,
          "tts stop 强制上屏（收尾不丢字）",
          "不强制 ⇒ 最后一句没有音频时字幕永远不出现")

    abort = func_body(cc, "void Application::AbortSpeaking")
    check("ClearPendingSubtitles()" in abort,
          "打断时丢弃待上屏字幕",
          "不丢 ⇒ 老人按停之后屏幕上又冒出一句上一轮的话")


def t4_follow_newest_page() -> None:
    print("\n── 4. 长回答分页要跟着最新一句（否则屏停在第一页）──")
    src = strip_comments((MAIN / "display" / "lcd_display.cc").read_text(encoding="utf-8"))
    hdr = strip_comments((MAIN / "display" / "lcd_display.h").read_text(encoding="utf-8"))

    check("ChatPageLayout(!is_user && !is_system)" in src,
          "assistant 追加时传 follow_newest=true",
          "传默认 false ⇒ 每来一句都弹回第一页，屏永远停在开头")
    check("chat_follow_tail_ = follow_newest && (pages > 1);" in src,
          "follow 状态只在多页时成立", "")
    check("chat_page_index_ = chat_follow_tail_ ? (pages - 1) : 0;" in src,
          "起始页 = 最后一页（多页且 follow 时）",
          "回到 0 就是老行为：声音念第三页、屏显示第一页")

    step = func_body(src, "void LcdDisplay::ChatPageStep")
    check("chat_follow_tail_" in step,
          "ChatPageStep 里判 follow 状态",
          "不判 ⇒ 6 秒后照样翻页，把正在念的字翻走")
    m = re.search(r"#define\s+CHAT_PAGE_LIVE_MS\s+(\d+)", src)
    check(m is not None and int(m.group(1)) >= 5000,
          "静默窗口 CHAT_PAGE_LIVE_MS ≥ 5 秒（容得下一次 TTS 抖动）",
          f"实际 {m.group(1) if m else '找不到'} —— 太短会把'换气'当成'说完'")
    check("lv_obj_scroll_to_y(bottom_bar_, 0, LV_ANIM_ON);" in step,
          "说完后回到第一页（让老人从头读）",
          "不回 ⇒ 前面的内容再也看不到")

    check("bool chat_follow_tail_ = false;" in hdr and
          "uint32_t chat_last_append_tick_ = 0;" in hdr,
          "两个状态成员都在头文件里", "")


def t5_workflow_registered() -> None:
    print("\n── 5. 这个脚本必须在 CI 里真的被执行 ──")
    wf = (ROOT / ".github" / "workflows" / "firmware-build.yml").read_text(encoding="utf-8")
    check("tools/smoke_subtitle_sync.py" in wf,
          "firmware-build.yml 里跑了 smoke_subtitle_sync.py",
          "写了不跑 = 没门禁，而且不报错（MEMORY.md 第 1 条）")


def main() -> int:
    t1_frame_counters()
    t2_sentence_start_deferred()
    t3_gate_and_teardown()
    t4_follow_newest_page()
    t5_workflow_registered()

    passed = sum(1 for ok, _, _ in results if ok)
    failed = len(results) - passed
    for ok, name, detail in results:
        if not ok:
            print(f"  [FAIL] {name}  {detail}")
    print("=" * 64)
    print(f"通过 {passed} 项，失败 {failed} 项")
    if failed:
        print("结论：**红**")
        return 1
    print("结论：**绿**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
