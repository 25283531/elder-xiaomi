#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""空识别退避 / 结束语直判回归（2026-11-03 用户实报）。

为什么单独一条冒烟
------------------
用户原话：「他已经说了先不聊了，还是频繁在说抱歉我没听清。」

根因是**两个判据都不存在**：
  1. 「不聊了 / 晚安 / 我睡了」原先都在**弱档**关键词里，只置
     ``_dismiss_candidate`` 交大模型复核；模型不调 ``enter_standby`` 时
     本轮什么都不发生，老人明确说了不聊，服务端却当没听见；
  2. 空识别分支**无条件**念 ``EMPTY_UTTERANCE_REPLY``，没有计数、没有退避。
     realtime 模式下设备念完就回 Listening、且 listening 态没有超时也不跑
     唤醒词 ⇒ 「念没听清 → 再空 ASR → 再念」这个环一旦启动就永不停止。

这条链路在真机上验证的代价是"对着设备说十几句话 + 等三个来回"，
所以判据必须能在进程内直接断言 —— 否则就是"写了不跑"。

判据口径
--------
· 分档是**纯函数**（``_dismiss_tier``），直接喂句子断言档位；
  同时断言「同一句不能同时留在两档」（重复虽不出错，但会让人以为两档都管）。
· 空识别计数走 ``_handle_empty_asr``（真正的下发入口），不是自己复刻一遍
  计数逻辑 —— 复刻出来的断言只能证明"测试和实现想法一致"，证明不了实现。
  ``_speak_reply`` / ``_enter_standby`` 用假实现替换，断言**调没调、传了什么**。
· 会话对象用真实 ``VoiceSession.__init__``（只存字段、无 IO），不手搓属性，
  免得"实现加了新字段、测试还绿"。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from app.voice.prompts import EMPTY_UTTERANCE_REPLY  # noqa: E402
from app.voice.session import (  # noqa: E402
    _EMPTY_ASR_STANDBY_STREAK,
    _DISMISS_STRONG_RE,
    _DISMISS_WEAK_RE,
    VoiceSession,
    _dismiss_tier,
)

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


# --------------------------------------------------------------- 1. 分档
# 2026-11-03 实报：这些是**明确的结束语**，必须直判（strong）。
# 逐个列而不是只测「先不聊了」：分档是正则并集，删掉任何一个分支词
# 都只影响那一句话，用一条用例守不住。
STRONG_CASES = [
    "先不聊了", "不聊了", "不聊了，我去做饭", "别聊了", "不用聊了",
    "聊到这儿吧", "聊到这吧", "不说了", "别说了", "不用说了",
    "不要再说了", "晚安", "晚安，我睡了", "睡吧", "我睡了", "我要睡了",
    "要睡了", "该睡了", "去睡了",
    # 原有的强档不能被这次改动挤掉
    "退下吧", "待机", "闭嘴", "别出声",
]
for _t in STRONG_CASES:
    _tier = _dismiss_tier(_t)
    check(_tier == "strong", f"「{_t}」应判强档（直判待机）", f"实际 {_tier!r}")

# 这些是「可能只是随口一句」，必须继续交模型复核（weak）。
# 误伤它们等于把老人正常聊天时的一句话变成"设备突然不理我了"。
WEAK_CASES = [
    "不用了", "没事了", "停一下", "再见", "拜拜", "安静", "小点声",
    "休息吧", "我走了", "困了", "睡觉吧", "先这样吧",
]
for _t in WEAK_CASES:
    _tier = _dismiss_tier(_t)
    check(_tier == "weak", f"「{_t}」应仍判弱档（交模型复核）", f"实际 {_tier!r}")

# 完全不该命中的普通聊天（避免"多说一句就被关掉"）。
for _t in ["今天天气不错", "我想吃饺子", "孙子回来了吗", "帮我记一下吃药"]:
    _tier = _dismiss_tier(_t)
    check(_tier == "", f"「{_t}」不该命中任何档", f"实际 {_tier!r}")

# 同一句不能同时出现在两档：_dismiss_tier 先强后弱，重复不会出错，
# 但日后改一档忘另一档时，两份判据会开始给出不同答案。
_overlap = [
    _t for _t in STRONG_CASES
    if _DISMISS_STRONG_RE.search(_t) and _DISMISS_WEAK_RE.search(_t)
]
check(not _overlap, "强/弱两档没有重复覆盖同一句", f"重复：{_overlap}")


# --------------------------------------------------- 2. 空识别计数与退避
class _Recorder:
    """替身：记录 _speak_reply / _enter_standby 的调用。"""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.standby: list[str] = []


def _make_session(rec: _Recorder) -> VoiceSession:
    """真实构造（__init__ 只存字段，不碰 ws / 不连库）。"""
    sess = VoiceSession(websocket=object())  # type: ignore[arg-type]
    sess.session_id = "smoke-empty-asr"

    async def _fake_speak(reply: str, started: float) -> None:
        rec.spoken.append(reply)

    async def _fake_standby(reason: str, farewell: str = "") -> None:
        rec.standby.append(reason)

    sess._speak_reply = _fake_speak          # type: ignore[method-assign]
    sess._enter_standby = _fake_standby      # type: ignore[method-assign]
    sess.state = "listening"
    return sess


async def _three_empties() -> None:
    rec = _Recorder()
    sess = _make_session(rec)

    # 第 1 次：照旧念固定回复（老人可能只说了一半）。
    await sess._handle_empty_asr(0.0)
    check(len(rec.spoken) == 1 and rec.spoken[0] == EMPTY_UTTERANCE_REPLY,
          "空识别第 1 次：念一次 EMPTY_UTTERANCE_REPLY",
          f"spoken={rec.spoken}")
    check(not rec.standby, "空识别第 1 次：不进待机", f"standby={rec.standby}")
    check(sess._empty_asr_streak == 1, "空识别第 1 次：计数 = 1",
          f"streak={sess._empty_asr_streak}")

    # 第 2 次：**不出声**（避免"自言自语"观感），也不待机。
    await sess._handle_empty_asr(0.0)
    check(len(rec.spoken) == 1, "空识别第 2 次：不再出声",
          f"spoken={rec.spoken}")
    check(not rec.standby, "空识别第 2 次：仍不进待机", f"standby={rec.standby}")
    check(sess._empty_asr_streak == 2, "空识别第 2 次：计数 = 2",
          f"streak={sess._empty_asr_streak}")

    # 第 3 次：进待机，会话真正结束（设备回 Idle + 重开唤醒词）。
    await sess._handle_empty_asr(0.0)
    check(len(rec.spoken) == 1, "空识别第 3 次：也不出声（不念告别语）",
          f"spoken={rec.spoken}")
    check(len(rec.standby) == 1, "空识别第 3 次：进待机",
          f"standby={rec.standby}")
    check(bool(rec.standby) and "空" in rec.standby[0],
          "空识别第 3 次：待机理由写明「连续识别为空」",
          f"reason={rec.standby}")
    check(_EMPTY_ASR_STANDBY_STREAK == 3, "阈值常量为 3（第 3 次才待机）",
          f"常量={_EMPTY_ASR_STANDBY_STREAK}")


# ------------------------------------- 3. 冷却期 / 已待机：一律不出声
async def _quiet_while_dismissed() -> None:
    # 冷却期内（老人刚说过"不聊了"）：任何"我没听清"都是打脸。
    rec = _Recorder()
    sess = _make_session(rec)
    sess._dismissed_until = time.monotonic() + 180
    await sess._handle_empty_asr(0.0)
    check(not rec.spoken and not rec.standby,
          "冷却期内的空识别：不出声、不待机", f"{rec.spoken} {rec.standby}")
    check(sess._empty_asr_streak == 0,
          "冷却期内的空识别：不计数（冷却后从第 1 次重新开始）",
          f"streak={sess._empty_asr_streak}")

    # 服务端已 idle（待机命令已下发，设备可能还在推流）。
    rec2 = _Recorder()
    sess2 = _make_session(rec2)
    sess2.state = "idle"
    await sess2._handle_empty_asr(0.0)
    check(not rec2.spoken and not rec2.standby,
          "已 idle 态的空识别：不出声、不待机", f"{rec2.spoken} {rec2.standby}")
    check(sess2._empty_asr_streak == 0, "已 idle 态的空识别：不计数",
          f"streak={sess2._empty_asr_streak}")

    # 冷却过期后（> _dismissed_until）应恢复正常处理：第 1 次照常出声。
    rec3 = _Recorder()
    sess3 = _make_session(rec3)
    sess3._dismissed_until = time.monotonic() - 1.0
    await sess3._handle_empty_asr(0.0)
    check(len(rec3.spoken) == 1,
          "冷却过期后的空识别：回到第 1 次（照常出声）", f"spoken={rec3.spoken}")


# --------------------------------- 4. 重新武装时清零（否则一开口就待机）
async def _rearm_clears_streak() -> None:
    rec = _Recorder()
    sess = _make_session(rec)
    sess._empty_asr_streak = _EMPTY_ASR_STANDBY_STREAK

    async def _noop_watchdog() -> None:
        return

    sess._silence_watchdog = _noop_watchdog   # type: ignore[method-assign]
    sess._rearm_listening()
    await asyncio.sleep(0)                    # 让 create_task 起的看门狗跑完
    check(sess._empty_asr_streak == 0,
          "重新武装监听：连续空识别计数清零", f"streak={sess._empty_asr_streak}")
    check(sess.state == "listening", "重新武装监听：state = listening",
          f"state={sess.state}")


# ------------------- 5. 有效识别处必须清零（接线断言，无其它覆盖手段）
def _reset_on_real_content_wired() -> None:
    """``_run_turn`` 里"有效内容"那段必须紧跟着清零计数。

    这段没有可直调的纯函数（它在一轮对话的中段），但它一旦被删，
    计数就会跨轮累计 —— 老人上午空过两次、下午随便说一句，第三次
    空识别就把他直接待机。所以按**源码相邻性**断言，宁可变脆也不放任。
    """
    src = (ROOT / "server" / "app" / "voice" / "session.py").read_text(
        encoding="utf-8")
    anchor = "self._last_active_at = time.monotonic()"
    # 这个赋值在 __init__ / 唤醒词 / _run_turn 里都有，所以逐处看窗口：
    # 只要**有一处**紧跟清零就算接线成功（_run_turn 那处）。
    ok = False
    start = 0
    while True:
        idx = src.find(anchor, start)
        if idx < 0:
            break
        if "self._empty_asr_streak = 0" in src[idx: idx + 600]:
            ok = True
            break
        start = idx + 1
    check(ok, "有效识别处清零连续空识别计数（源码接线）",
          "未在任何 _last_active_at 赋值之后找到 _empty_asr_streak = 0")


def main() -> int:
    asyncio.run(_three_empties())
    asyncio.run(_quiet_while_dismissed())
    asyncio.run(_rearm_clears_streak())
    _reset_on_real_content_wired()

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
    raise SystemExit(main())
