#!/usr/bin/env python3
"""长按即时应答 + 字幕累积 + 多条提醒同屏 —— 静态接线冒烟。

为什么是"静态"断言而不是跑起来：
  这三件事全部发生在**真机上**（按键回调、LVGL 布局、提醒引擎任务），
  本机没有屏也没有按键。可验证的部分只有"接线是否正确"：
  片段有没有被调用、追加分支还在不在、旧文案有没有被改回去。

⚠️ 一律**先剥注释再匹配**：本仓踩过"防回归注释自己踩红"的坑（见 MEMORY.md
   第 3 条）。这里剥 // 与 /* */，并且只在**代码位**上找调用点。

对应需求（2026-11-03 用户原话）：
  1. 长按屏幕显示日程 → 播「日程已确认完成，我就不在屏幕显示了」并退回对话态；
     长按屏幕显示服药提醒 → 播「您确认已经完成这次服药了吗？」并进入聆听。
  2. 累积多条提醒 → 同时显示在一屏上。
  3. 字幕显示不完整 → 要能翻页/滚动（根因是每句覆盖，见第 3 组）。

2026-11-04 新增第 5 组：#1 按键指示灯的长按灯效改成
「天青 #00E5FF 由暗变亮 → 3 秒达标天蓝 #00BFFF 全亮闪 3 次 / 中途松开粉红
 #FF69B4 全亮闪 5 次」。灯效只能在真机上看，本机唯一能守的是**代码位**：
渐变是不是还走同一色相（scale_color）、是不是还用墙上时钟、追问路径有没有
真的闪天蓝、灯珠宏有没有被顺手改掉。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "firmware" / "components" / "elder" / "src" / "elder_bridge.cc"
CLIP_C = ROOT / "firmware" / "components" / "elder" / "src" / "elder_clip.c"
CLIP_H = ROOT / "firmware" / "components" / "elder" / "include" / "elder_clip.h"
REMINDER_C = ROOT / "firmware" / "components" / "elder" / "src" / "reminder.c"
LCD = ROOT / "firmware" / "xiaozhi" / "main" / "display" / "lcd_display.cc"
#: #1 按键灯的真源（`firmware/xiaozhi/components/elder` 是 merge 生成的副本，不读它）
RGB_C = ROOT / "firmware" / "components" / "elder" / "src" / "rgb.c"
BOARD_H = ROOT / "firmware" / "components" / "elder" / "include" / "elder_board.h"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


def strip_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，保留字符串字面量。

    顺序很重要：先块注释再行注释，且**行注释不能吃掉字符串里的 //**
    （本文件里有 "http://…" 这类字面量，虽然现在没有，但别留坑）。
    """
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


def t1_clip_ids() -> None:
    print("\n── 1. 两条新片段必须在枚举里，且时长表/名字表同步 ──")
    h = CLIP_H.read_text(encoding="utf-8")
    for name in ("ELDER_CLIP_SCHED_DONE", "ELDER_CLIP_MED_CONFIRM"):
        check(name in h, f"枚举里有 {name}", "elder_clip.h 找不到")
    c = strip_comments(CLIP_C.read_text(encoding="utf-8"))
    # 时长表必须比原来多两项（7 → 9）。少一项 = 下标错位，症状是"服药播了日程的话"。
    m = re.search(r"kClipMs\[ELDER_CLIP_COUNT\]\s*=\s*\{(.*?)\};", c, re.S)
    nums = re.findall(r"\b(\d{3,5})\b", m.group(1)) if m else []
    check(len(nums) == 9, "kClipMs 有 9 项（7 旧 + 2 新）", f"实际 {len(nums)} 项")
    check("3780" in nums and "2700" in nums,
          "新片段时长 3780 / 2700 在表里（sched_done / med_confirm）",
          f"nums={nums}")
    check('"sched_done"' in c and '"med_confirm"' in c,
          "kClipName 同步加了 sched_done / med_confirm",
          "日志名没加，现场排查时分不清是哪条片段")


def t2_long_press_wiring() -> None:
    print("\n── 2. 长按分支：日程播第一句、服药播第二句并进聆听 ──")
    b = strip_comments(BRIDGE.read_text(encoding="utf-8"))

    check("elder_clip_play(ELDER_CLIP_SCHED_DONE)" in b,
          "日程分支播 ELDER_CLIP_SCHED_DONE",
          "没接片段 ⇒ 老人按完只有一声哔")
    check("elder_clip_play(ELDER_CLIP_MED_CONFIRM)" in b,
          "服药分支播 ELDER_CLIP_MED_CONFIRM",
          "没接片段 ⇒ 又要等 1~3 秒 TTS，正是用户投诉的点")
    check("ListenAfterClip(ELDER_CLIP_MED_CONFIRM)" in b,
          "服药分支播完进聆听（ListenAfterClip）",
          "只播不问 ⇒ 老人答了也没人听")

    # 顺序：先确认（收屏）再播片段。反了会让老人听完 3.8 秒才看到屏收起来。
    sched = b.find("elder_clip_play(ELDER_CLIP_SCHED_DONE)")
    confirm = b.find("ConfirmMedicine(idx, ELDER_CFM_BUTTON);\n                if (!elder_clip_play")
    check(confirm >= 0 and confirm < sched,
          "日程分支：先 ConfirmMedicine（收屏）再播片段",
          "顺序反了 ⇒ 按完 3.8 秒内屏幕还挂着提醒")

    # 服药分支的片段调用必须在 TTS 兜底之前（否则先发 TTS 就失去"零延迟"的意义）。
    med = b.find("elder_clip_play(ELDER_CLIP_MED_CONFIRM)")
    tts = b.find("new std::string(prompt)")
    check(med >= 0 and tts >= 0 and med < tts,
          "服药分支：片段在 TTS 兜底之前（零延迟）",
          "顺序反了 ⇒ 仍然先走 TTS")

    # 片段不可用 / 开不了麦时必须还能退回原路径，不能变成"按了没反应"。
    #
    # 判据 2026-11-03 晚从 CanStartDialog() 换成 CanPlayClip()：前者要求
    # kDeviceStateIdle，而 realtime 对话期间设备一直停在 kDeviceStateListening
    # ⇒ 长按 3 秒既播不出片段、也进不了这条 fail-safe，现场就是**一片安静**。
    # CanPlayClip() 放行 Idle 与 Listening，只排除 Speaking/Connecting。
    check("if (med_n == 0 && !CanPlayClip())" in b,
          "药数未知 + 播不了片段 ⇒ 退回直接确认（fail-safe）",
          "删掉这条会让离线老人按了没反应")
    check("if (med_n == 0 && !CanStartDialog())" not in b,
          "fail-safe 不再用 CanStartDialog()（Listening 态会被判成开不了麦）",
          "回到 CanStartDialog ⇒ 对话中长按服药键静默无响应")


def t3_subtitle_accumulate() -> None:
    print("\n── 3. 字幕必须累积（每句覆盖 = 永远只有最后一句）──")
    src = strip_comments(LCD.read_text(encoding="utf-8"))
    check("merged += text;" in src,
          "assistant 字幕走追加（merged += text）",
          "回到 lv_label_set_text(target, text) 就是覆盖，分页永远不会触发")
    check("chat_assistant_tick_" in src,
          "有跨轮兜底（20 秒没新句 ⇒ 视为新一轮，覆盖不追加）",
          "没有它，无用户话的轮次会把两轮回答拼在一起")
    check("const bool dup =" in src,
          "重复句保护（同一句重发不追加两遍）",
          "工具轮重发同一句时老人会看到两遍")
    check("ChatPageLayout(anchor_px)" in src,
          "写完文字后仍然调 ChatPageLayout(anchor_px)（分页入口）",
          "删了就没分页；不带 anchor_px 就等于每句都弹回第 1 页")


def t4_reminder_list() -> None:
    print("\n── 4. 多条提醒逐条列出（不再只写「还有 N 条」）──")
    src = strip_comments(REMINDER_C.read_text(encoding="utf-8"))
    check("（还有 %u 条）" not in src,
          "旧的「（还有 N 条）」已删除",
          "还在 ⇒ 老人知道不止一条却不知道是哪几条")
    check('"\\n等 %u 件"' in src or "\\n等 %u 件" in src,
          "超屏时有「等 N 件」兜底",
          "没有兜底 ⇒ 第 4 条起无声消失")
    check("pending_same_minute(item->fired_due_at)" in src,
          "明细与条数用**同一个**判据（pending_same_minute）",
          "两套判据会漂移：屏幕写 3 条、实际只列 2 条")


def _define_value(src: str, name: str) -> str:
    """取 `#define NAME value` 的 value（src 必须已经剥过注释）。找不到返回空串。"""
    m = re.search(rf"^[ \t]*#define[ \t]+{re.escape(name)}[ \t]+(\S+)", src, re.M)
    return m.group(1) if m else ""


def _brace_body(src: str, start: int) -> str:
    """从 start 往后找第一个 `{`，按花括号配平返回其内部文本。

    只用于**已剥注释**的源码；这几个函数体里没有带花括号的字符串字面量。
    """
    i = src.find("{", start)
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    return ""


def t5_button_led() -> None:
    """#1 按键灯新语义（2026-11-04）。

    本组每条断言都做过「临时改坏真源 → 跑本脚本 → 确认对应断言变红 → 还原」
    的验证（2026-11-04，改坏点记录如下，避免以后有人以为是装饰性断言）：
      · 渐变改回 lerp_color            ⇒ 5.1 的 scale_color / lerp_color 两条红
      · 渐变 el 偷换成 g.btn_anim_ms   ⇒ 5.1 的墙上时钟 / 渲染时钟两条红
      · GRAD_COLOR 0x00E5FF→0x00E6FF   ⇒ 天青常量红
      · OK_COLOR 0x00BFFF→0x00CED1     ⇒ 天蓝常量红
      · FAIL_COLOR 0xFF69B4→0xFF1493   ⇒ 粉红常量红
      · GRAD_MIN_LEVEL 20→0            ⇒ 起点亮度红
      · OK_TIMES 3→4 / FAIL_TIMES 5→4  ⇒ 次数两条分别红
      · acknowledged 退回 BTN_ATTRACT  ⇒ 5.3 两条都红
      · ELDER_RGB_CHAIN 20→9 / LEDS 9→8⇒ 灯珠宏防回归红
    """
    print("\n── 5. #1 按键灯：天青变亮 → 天蓝闪 3 次 / 粉红闪 5 次 ──")
    src = strip_comments(RGB_C.read_text(encoding="utf-8"))

    # 5.1 渐变分支（只切 `case BTN_GRADIENT:` 到 `case BTN_FLASH:` 这一小段）。
    #     不能整文件匹配：同一个 btn_step 里 FLASH 分支本来就该用
    #     g.btn_anim_ms（渲染时钟，防止系统忙时少闪），整文件断言会误杀。
    m = re.search(r"case BTN_GRADIENT\s*:(.*?)case BTN_FLASH\s*:", src, re.S)
    grad = m.group(1) if m else ""
    check(bool(grad), "能定位 BTN_GRADIENT 分支",
          "找不到 case BTN_GRADIENT … case BTN_FLASH，状态机被重构过？")
    check("scale_color(ELDER_RGB_BTN_GRAD_COLOR" in grad,
          "渐变用 scale_color(ELDER_RGB_BTN_GRAD_COLOR)（同一色相只改亮度）",
          "换成 lerp/dim ⇒ 要么变「换颜色」，要么暗端死黑看不出渐变")
    check("lerp_color" not in grad,
          "渐变分支不再用 lerp_color（它会把天青插值成别的色相）",
          "回到 lerp_color ⇒ 老人看到的是「换了个颜色」而不是进度")
    check("g.btn_phase_ms" in grad,
          "渐变进度用墙上时钟 g.btn_phase_ms（与 input.c 的 3 秒判定同源）",
          "不用墙上时钟 ⇒ 系统忙时灯还没亮满、按键已经算确认，灯在骗人")
    check("g.btn_anim_ms" not in grad,
          "渐变分支没有偷换成渲染时钟 g.btn_anim_ms",
          "渲染时钟会被 ELDER_RGB_ANIM_MAX_STEP_MS 拖慢，和 3 秒判定错位")

    # 5.2 颜色 / 次数常量（对应需求原词：天青、天蓝、粉红、3 次、5 次）
    for name, want, why in (
        ("ELDER_RGB_BTN_GRAD_COLOR", "0x00e5ffu", "天青色"),
        ("ELDER_RGB_BTN_OK_COLOR", "0x00bfffu", "天蓝色"),
        ("ELDER_RGB_BTN_FAIL_COLOR", "0xff69b4u", "粉红色"),
        ("ELDER_RGB_BTN_GRAD_MIN_LEVEL", "20u", "渐变起点亮度（不从全黑起）"),
    ):
        got = _define_value(src, name)
        check(got.lower() == want, f"{name} == {want}（{why}）",
              f"实际 {got or '缺失'}")
    for name, want in (("ELDER_RGB_BTN_OK_TIMES", "3"),
                       ("ELDER_RGB_BTN_FAIL_TIMES", "5")):
        got = _define_value(src, name)
        check(got == want, f"{name} == {want}（用户要求闪 {want} 次）",
              f"实际 {got or '缺失'}")

    # 5.3 追问路径：3 秒达标即闪天蓝，不能再退回橙色呼吸。
    pos = src.find("void elder_rgb_button_acknowledged(void)")
    ack = _brace_body(src, pos) if pos >= 0 else ""
    check("btn_enter_flash(ELDER_RGB_BTN_OK_COLOR, ELDER_RGB_BTN_OK_TIMES)" in ack,
          "elder_rgb_button_acknowledged() 调 btn_enter_flash(OK, 3)",
          "退回「只回橙色呼吸」⇒ 老人按满 3 秒却看不到任何回执")
    check("BTN_ATTRACT" not in ack,
          "acknowledged() 不再自己切 BTN_ATTRACT（迁移交给 btn_enter_flash）",
          "又写回 BTN_ATTRACT ⇒ 与 confirmed() 不一致，追问路径不闪结果色")

    # 5.4 灯珠宏防回归：CHAIN != LEDS 是本项目硬约束。
    #     后 11 颗必须每帧写 0，否则它们停在开机随机态常亮（2026-10-27 实报）。
    b = strip_comments(BOARD_H.read_text(encoding="utf-8"))
    for name, want in (("ELDER_RGB_LEDS", "9"), ("ELDER_RGB_CHAIN", "20")):
        got = _define_value(b, name)
        check(got == want, f"{name} 仍是 {want}",
              f"实际 {got or '缺失'} —— 改了它后 11 颗会停在开机随机态常亮")


def main() -> int:
    t1_clip_ids()
    t2_long_press_wiring()
    t3_subtitle_accumulate()
    t4_reminder_list()
    t5_button_led()

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
