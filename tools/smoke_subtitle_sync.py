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
对应需求（2026-11-04 用户原话）：
  「多行的时候显示不完整，也没有把用户说的字幕和设备的字幕分到不同页面，
    字幕刷新率很低，完全跟不上语音。」
  第 4 组断言覆盖后三条：分页判据、追加落点、翻页不再被 follow 挡住、
  兜底超时 ≤ 1 秒。
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


def last_func_body(src: str, signature: str) -> str:
    """取**最后**一个 `signature` 的函数体（花括号配对），找不到返回 ""。

    为什么不能复用 func_body：`lcd_display.cc` 里有两份 `SetChatMessage`
    —— 前面那份在 `#ifdef CONFIG_USE_WECHAT_MESSAGE_STYLE` 下、本板不编译，
    后面 `#else` 那份才是真正跑在屏上的。`find()` 取到的是不编译的那份，
    断言就变成"改坏了也绿"的假信号。取最后一个 = 取 `#else` 分支。
    """
    i = src.rfind(signature)
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
    # 兜底值必须 ≤ 1 秒
    m = re.search(r"kSubtitleMaxWaitUs\s*=\s*([\d.]+)\s*LL\s*\*\s*1000\s*\*\s*1000", cc)
    check(m is not None and float(m.group(1)) <= 1.0,
          "兜底超时 ≤ 1 秒（2026-11-04 从 6 秒收紧）",
          f"实际 {m.group(1) if m else '找不到'} 秒 —— 解码+播放队列积压 3.36 秒时，"
          "6 秒兜底会把字幕拖到 3 秒以上，老人看到的就是「字追不上声音」")
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


def t4_paging_anchor_and_rate() -> None:
    """2026-11-04 重做：分页判据、追加落点、翻页不再被 follow 挡住。

    每条断言都做过「改坏 → 变红」验证（临时改坏、跑本脚本、确认红、还原）：
      · split 判据改回 `(user_h + bot_h + gap) > page_h`  ⇒ 第 2 条红
      · `chat_page_index_ = idx;` 改回 `= pages - 1;`      ⇒ 第 4、5 条红
      · ChatPageStep 开头插 `if (chat_page_index_ >= 0) return;` ⇒ 第 6、8 条红
      · `kSubtitleMaxWaitUs` 改回 6 秒                    ⇒ 第 3 组红
      · `ChatPageLayout(anchor_px)` 改回 `ChatPageLayout()` ⇒ 第 10 条红
    """
    print("\n── 4. 分页：两段必分页 + 落点跟语音 + 翻页不被挡住 ──")
    src = strip_comments((MAIN / "display" / "lcd_display.cc").read_text(encoding="utf-8"))
    hdr = strip_comments((MAIN / "display" / "lcd_display.h").read_text(encoding="utf-8"))

    # ⚠️ 必须取**最后**一份：文件里有两份 SetChatMessage，前一份在
    # CONFIG_USE_WECHAT_MESSAGE_STYLE 下、本板不编译。
    layout = last_func_body(src, "void LcdDisplay::ChatPageLayout")
    step = last_func_body(src, "void LcdDisplay::ChatPageStep")
    scm = last_func_body(src, "void LcdDisplay::SetChatMessage")

    check(bool(layout) and bool(step) and bool(scm),
          "找到生效的 ChatPageLayout / ChatPageStep / SetChatMessage（#else 分支）",
          "函数被改名，或只剩 WECHAT 分支那一份（那份本板不编译）")

    # 1) 两段都有内容就分页 —— 不再看"合起来放不放得下"
    m = re.search(r"const bool split = \(([^\n]*)\);", layout)
    cond = m.group(1) if m else ""
    check(m is not None and "user_h > 0" in cond and "bot_h > 0" in cond
          and "page_h" not in cond,
          "split 判据只看「两段都有内容」（不再看合起来放不放得下）",
          "回到 (user_h + bot_h + gap) > page_h 就是老行为："
          "「今天几号 / 11 月 3 号」这种短问答仍然同页，老人分不清哪句是自己说的")

    # 2) 用户话被撑成整页 ⇒ 设备字幕必然落到下一页
    check(re.search(r"lv_obj_set_height\(chat_user_label_,\s*user_block - gap\)",
                    layout) is not None
          and "user_block = user_pages * page_h;" in layout,
          "用户话撑成整页（user_block = user_pages * page_h）",
          "不撑 ⇒ 设备字幕从用户话下面接着排，两段仍在同一页")
    check("lv_obj_update_layout(bottom_bar_);" in layout,
          "撑高后重新 update_layout 再算页数",
          "不重算 ⇒ 页数按旧布局算，落点和页数都对不上")
    check(re.search(r"lv_obj_set_height\(chat_message_label_,\s*bot_h \+ \(pad_target - natural\)\)",
                    layout) is not None,
          "给最后一段补足尾部留白（pad_target），保证每个页起点都能精确滚到",
          "不补 ⇒ 内容比 (pages-1)*page_h + 视口高 少一截，滚动位置被夹住，"
          "第 2 页顶端露出上一段最后一行的半截，两段又出现在同一屏")
    check("const int excess = (avail > page_h) ? (avail - page_h) : 0;" in layout,
          "页数与留白按同一个 excess（视口高 - 页高）口径算",
          "两套口径会漂移：页数说 2 页、实际只能滚到 1.5 页")

    # 3) 落点 = 本句起始页，不是最后一页
    check("chat_page_index_ = idx;" in layout and "anchor_px" in layout,
          "chat_page_index_ 由 anchor_px 换算的 idx 决定",
          "回到 pages - 1 ⇒ 屏跳到句子末尾，声音才念第一句")
    check("chat_page_index_ = pages - 1" not in layout and
          "chat_page_index_ = chat_follow_tail_" not in layout,
          "不再有「追加即跳最后一页」",
          "追加跳末尾 = 前面几行永远轮不到，正是「多行显示不完整」")
    check("chat_follow_tail_" not in layout and "chat_follow_tail_" not in hdr,
          "follow_tail 状态已彻底删除（源码与头文件都没有）",
          "留着这个成员，下一个人会以为它还在生效")

    # 4) ChatPageStep 不再"有字幕就早退"
    check("chat_follow_tail_" not in step,
          "ChatPageStep 里没有「有字幕就不翻页」的早退",
          "早退 + 屏停在末页 = 老人永远只看到句子尾巴")
    # ⚠️ 只查 `chat_follow_tail_` 不够：换一种写法的早退（例如
    # `if (chat_page_index_ >= 0) return;`）照样能让翻页彻底停摆，而上面那条
    # 断言看不见。所以按**结构**查：整个函数体只允许两个 return，且每个
    # return 前面必须是那两条合法守卫（页数 ≤ 1 / 已到最后一页）。
    returns = [m.start() for m in re.finditer(r"\breturn\s*;", step)]
    stray = []
    for pos in returns:
        head = step[max(0, pos - 260):pos]
        if ("chat_page_count_ <= 1" in head or
                "chat_page_index_ + 1 >= chat_page_count_" in head):
            continue
        stray.append(" ".join(head[-70:].split()))
    check(len(returns) == 2 and not stray,
          "ChatPageStep 只有两个 return（页数≤1 / 已到最后一页），没有别的早退",
          f"发现 {len(returns)} 个 return，其中无守卫的：{stray} —— "
          "任何多余早退都会让长回答后面的页永远翻不到")
    check(re.search(r"lv_obj_scroll_to_y\(bottom_bar_,\s*0\s*,", step) is None,
          "翻完不回第 1 页（最后一页停住）",
          "回第 1 页会把老人正在读的结论翻走，且循环重置防烧屏计时")
    check("chat_page_index_++" in step,
          "每 CHAT_PAGE_DWELL_MS 前进一页",
          "不前进 ⇒ 长回答后面的页永远看不到")
    check("lv_timer_pause(chat_page_timer_);" in step,
          "到最后一页停住定时器",
          "不停 ⇒ 空转烧 CPU")

    # 5) 语音同步：先量本句起始行，再追加，再按落点上屏
    i_anchor = scm.find("anchor_px = prev_lines * line_h")
    i_set = scm.find("lv_label_set_text(target, merged.c_str())")
    check(i_anchor >= 0 and i_set >= 0 and i_anchor < i_set,
          "先量「本句第一行」再追加文本",
          "先追加再量 ⇒ 量到的是本句之后的高度，落点永远差一句")
    check("ChatPageLayout(anchor_px)" in scm,
          "SetChatMessage 把 anchor_px 传给 ChatPageLayout",
          "传默认值 ⇒ 每句都弹回第 1 页，屏和声音分家")

    # 6) 两份字幕仍是两个独立控件（分页才谈得上"分到不同页"）
    check("chat_user_label_ = lv_label_create(bottom_bar_);" in src and
          "chat_message_label_ = lv_label_create(bottom_bar_);" in src,
          "用户字幕与设备字幕是两个独立 lv_obj_t（各自 create）",
          "合并成一个控件 ⇒ 颜色/清空逻辑互相踩，也无法分页")
    check("target = is_user ? chat_user_label_ : chat_message_label_;" in scm,
          "写入目标是二选一（不会同时写两个）",
          "同时写两个 ⇒ 两段文字挤在同一页")

    # 7) 头文件签名
    check("void ChatPageLayout(int anchor_px = -1);" in hdr,
          "头文件签名是 ChatPageLayout(int anchor_px = -1)",
          "还是 bool follow_newest ⇒ 落点逻辑没接上")

    # 8) 不再有 8 秒静默窗口
    check("#define CHAT_PAGE_LIVE_MS" not in src,
          "CHAT_PAGE_LIVE_MS 已删除（翻页不再等静默窗口）",
          "留着它就是「有字幕期间不翻页」那套旧行为")

    # 9) 扩容量：列宽从 183 提到 242（8 字/行），且没动字号
    check("ChatAvatarSize()" in src and "ChatPadX()" in src,
          "对话态用专用的头像/边距（列宽 242 ⇒ 8 字/行）",
          "回到 AvatarSize() 的 90px ⇒ 列宽 183，一行只有 6 个汉字")
    check("chat_font" in src,
          "字号仍是 30px 的 chat_font（不许降到 20px）",
          "20px 汉字只有 2mm 高，老人看不清")


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
    t4_paging_anchor_and_rate()
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
