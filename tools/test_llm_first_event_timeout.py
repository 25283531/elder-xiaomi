#!/usr/bin/env python3
"""LLM 首字超时 —— 「厂商卡住 60 秒，老人对着设备干等」的防线。

为什么必须有这条门禁
--------------------
2026-11-04 生产日志里 9 轮对话的首事件时延：

    最小 723ms / 中位 1023ms / 最大 **94007ms**，其中 **3 轮 > 20 秒**

三分之一轮次让老人干等半分钟以上。这段时间的连带损失（同一份日志）：

  * 设备持续推上行音频，服务端在忙 ⇒ 整段丢弃
    （``思考期丢弃上行音频 1160 帧（69600ms）``）；
  * 老人以为设备坏了，自己长按按键把提醒确认掉；
  * 服务端 65 秒后才执行 ``confirm_reminder``，待确认清单已经空了 ⇒
    回一句「请让老人按一下设备上的按键」—— 他 6 秒前刚按完。

最后一条就是用户实报「服药提醒按键按了又是不能确认」的真正来源。所以这条
超时不是"优化"，是把一条会让老人误判设备坏了的链路掐断。

钉死三件事
----------
1. ``_FIRST_EVENT_TIMEOUT_SEC`` 存在、且 <= 30 秒（再大就等于没有）；
2. ``_chat_round`` 在**第一个事件**之前真的会超时，并返回 ``_FIRST_EVENT_TIMEOUT``
   这个哨兵 —— 用行为验证：把阈值调到 0.1 秒、让假网关睡 1 秒，看它 0.5 秒内
   就返回；
3. 哨兵 **不是** ``None``（``None`` 的语义是"被 abort 叫停"，混用会让超时被
   当成打断处理：不播任何话、日志一个字都没有，老人听到静音）。

跑法
----
    python tools/test_llm_first_event_timeout.py
退出码 0 = 全绿。CI 的 server-ci job 会跑它。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

_TMP = Path(tempfile.mkdtemp(prefix="llm-timeout-"))
os.environ.setdefault("ELDER_DB", f"sqlite:///{(_TMP / 'test.db').as_posix()}")
os.environ.setdefault("ELDER_JWT_SECRET", "llm-timeout-secret-0123456789abcdef")
os.environ.setdefault("ELDER_SCHEDULER_INTERVAL", "3600")
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_k, None)

_g_pass = 0
_g_fail = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _g_pass, _g_fail
    if ok:
        _g_pass += 1
        print(f"  [OK]   {name}")
    else:
        _g_fail += 1
        _failures.append(name)
        print(f"  [FAIL] {name}" + (f"  —— {detail}" if detail else ""))


# ================================================================ 假设备 / 假网关
class FakeWS:
    """只提供 send_text 的假 WS（本用例不下发任何协议消息）。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:   # pragma: no cover
        pass


class RecordingSpeaker:
    """记下被推出去的文本，不碰 TTS / 协议。"""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.closed = False

    async def push_text(self, text: str) -> None:
        self.texts.append(text)

    async def flush_rest(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


# ================================================================ 第 1 组
def t_constants() -> None:
    print("\n[1] 超时常量与哨兵")
    from app.voice import session as S

    check("_FIRST_EVENT_TIMEOUT_SEC 存在",
          hasattr(S, "_FIRST_EVENT_TIMEOUT_SEC"))
    t = float(getattr(S, "_FIRST_EVENT_TIMEOUT_SEC", 0) or 0)
    check("首字超时 <= 30 秒（再大就等于没有，实测最大 94 秒）",
          0 < t <= 30.0, f"_FIRST_EVENT_TIMEOUT_SEC={t}")
    check("首字超时 >= 5 秒（太短会误杀正常轮次，中位 1 秒、正常波动到 3~5 秒）",
          t >= 5.0, f"_FIRST_EVENT_TIMEOUT_SEC={t}")

    check("★ 哨兵不是 None（None 的语义是「被 abort 叫停」，混用会让超时静默）",
          S._FIRST_EVENT_TIMEOUT is not None)
    check("哨兵不是字符串/数字（不会被当成正常 ChatResult 混进 results）",
          not isinstance(S._FIRST_EVENT_TIMEOUT, (str, int, float, bytes)))

    check("兜底话术存在且是短句（老人等了 20 秒，听不懂解释，只想知道该再说一遍）",
          isinstance(S.LLM_SLOW_FALLBACK, str)
          and 5 <= len(S.LLM_SLOW_FALLBACK) <= 30,
          f"{getattr(S, 'LLM_SLOW_FALLBACK', None)!r}")
    check("兜底话术把球抛回给老人（含「再说一遍」）",
          "再说一遍" in str(getattr(S, "LLM_SLOW_FALLBACK", "")))


# ================================================================ 第 2 组
def t_timeout_behaviour() -> None:
    """行为验证：假网关睡 1 秒，阈值调到 0.1 秒 ⇒ _chat_round 必须很快返回哨兵。

    只做静态断言是不够的 —— `wait_for` 包错位置（例如包在 `run_in_threadpool`
    外面但 `await` 写在别处）、或者只在 `got_first` 之后才生效，静态都看不出来。
    """
    print("\n[2] _chat_round 真的会在首事件前超时")
    from app.voice import session as S

    real_timeout = S._FIRST_EVENT_TIMEOUT_SEC
    real_stream = S.chat_stream

    def slow_stream(db, kind, messages, tools=None, tool_choice=None):
        """假网关：先睡 1 秒才吐第一个事件（模拟厂商卡住）。"""
        time.sleep(1.0)
        yield ("text", "迟到的回答")

    try:
        S._FIRST_EVENT_TIMEOUT_SEC = 0.1
        S.chat_stream = slow_stream

        ws = FakeWS()
        sess = S.VoiceSession(ws, protocol_version=1)
        sess.session_id = "test-timeout"
        speaker = RecordingSpeaker()

        t0 = time.monotonic()
        out = asyncio.run(S.VoiceSession._chat_round(
            sess, [{"role": "user", "content": "你好"}], speaker, 1,
            {"round_ms": [], "ttft_ms": [], "tool_ms": 0}))
        elapsed = time.monotonic() - t0

        check("首事件前超时会返回哨兵", out is S._FIRST_EVENT_TIMEOUT, f"out={out!r}")
        check("返回哨兵时确实是「很快就返回」，不是等满 1 秒",
              elapsed < 0.8, f"elapsed={elapsed:.2f}s")
        check("超时那一轮没有把任何文字推给 speaker（没说话才对）",
              speaker.texts == [], f"texts={speaker.texts}")

        # ---- 正常轮次不能被误杀：网关立刻吐事件，阈值 0.1 秒也够 ----
        def fast_stream(db, kind, messages, tools=None, tool_choice=None):
            yield ("text", "正常回答")

        S.chat_stream = fast_stream
        speaker2 = RecordingSpeaker()
        out2 = asyncio.run(S.VoiceSession._chat_round(
            sess, [{"role": "user", "content": "你好"}], speaker2, 1,
            {"round_ms": [], "ttft_ms": [], "tool_ms": 0}))
        check("正常轮次不会被超时误杀（返回的不是哨兵）",
              out2 is not S._FIRST_EVENT_TIMEOUT, f"out2={out2!r}")
    finally:
        S._FIRST_EVENT_TIMEOUT_SEC = real_timeout
        S.chat_stream = real_stream


# ================================================================ 第 3 组
def t_caller_wiring() -> None:
    """调用方必须把哨兵翻译成"念一句兜底话术"，而不是当成空回复。

    ⚠️ 静态判据一律**先剥注释**（MEMORY 第 3 条）：本仓踩过"防回归注释
    自己踩红"的假信号。
    """
    print("\n[3] _run_turn 把哨兵翻译成兜底话术")
    src = (SERVER / "app" / "voice" / "session.py").read_text(encoding="utf-8")
    stripped = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    stripped = re.sub(r"^\s*#.*$", "", stripped, flags=re.M)

    # 只取 _run_turn 那段（从 for round_no 到 else 分支）
    m = re.search(r"for round_no in range\(1, config\.VOICE_TOOL_MAX_ROUNDS \+ 1\):"
                  r"(.*?)\n            else:", stripped, flags=re.S)
    check("找得到工具轮循环", m is not None)
    body = m.group(1) if m else ""

    check("循环里判了哨兵（is _FIRST_EVENT_TIMEOUT）",
          "is _FIRST_EVENT_TIMEOUT" in body, body[:200])
    check("判哨兵时赋了兜底话术",
          "LLM_SLOW_FALLBACK" in body, body[:200])
    check("判哨兵时**真的推给 speaker**（只赋值不推 = 老人听到静音）",
          "speaker.push_text" in body, body[:200])
    check("判哨兵的分支在 `if result is not None` **之前**"
          "（否则哨兵会被当成 ChatResult 记进 results）",
          body.find("is _FIRST_EVENT_TIMEOUT") < body.find("if result is not None")
          if "if result is not None" in body else False,
          f"sentinel@{body.find('is _FIRST_EVENT_TIMEOUT')} "
          f"notnone@{body.find('if result is not None')}")

    # 首事件超时只包在第一个事件上（中途卡住不能掐，speaker 可能已经在念了）
    m2 = re.search(r"event = await asyncio\.wait_for\((.*?)\)\n", stripped, flags=re.S)
    check("next() 被 asyncio.wait_for 包住", m2 is not None)
    wrapped = m2.group(1) if m2 else ""
    check("超时阈值只在首个事件生效（None if got_first）",
          "got_first" in wrapped and "_FIRST_EVENT_TIMEOUT_SEC" in wrapped,
          wrapped[:200])


def main() -> int:
    print("=== LLM 首字超时（厂商卡住 ⇒ 别让老人干等）===")
    t_constants()
    t_timeout_behaviour()
    t_caller_wiring()

    print(f"\n=== {_g_pass} 通过 / {_g_fail} 失败 ===")
    if _failures:
        print("失败项：")
        for f in _failures:
            print(f"  - {f}")
    return 1 if _g_fail else 0


if __name__ == "__main__":
    sys.exit(main())
