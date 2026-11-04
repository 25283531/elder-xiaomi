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
from app.voice import session as vs  # noqa: E402
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
# 强档 = **只能对机器说**的祈使句（「退下」「待机」「闭嘴」）—— 它们的字面
# 意思在正常陪聊里不成立，命中即直判待机是安全的。
STRONG_CASES = [
    "退下吧", "退下", "待机", "待命", "进入待机", "闭嘴", "静音", "别出声",
    "不出声", "我想静静",
]
# 注：「让我安静一会儿」**故意不列** —— 它同时命中强档的「让我安静」和弱档的
# 「安静」，是两档本就存在的重叠（弱档的「安静」是为了「安静点」这类抱怨）。
# 重叠时 `_dismiss_tier` 先强后弱 ⇒ 实际走强档直判，而这句话在陪聊里完全可能
# 只是"别吵我，我在想事"。真要收口，得先把弱档的「安静」改成更长的判据，
# 那是另一件事；这里只是不让断言假装它不存在。
for _t in STRONG_CASES:
    _tier = _dismiss_tier(_t)
    check(_tier == "strong", f"「{_t}」应判强档（直判待机）", f"实际 {_tier!r}")

# 弱档 = 在普通对话里**也会夹杂**的话，必须交大模型按上下文判意图。
#
# ⚠️ 2026-11-03 用户明确否决了「把结束语升到强档」这个改法，原话：
#    「弱档中待机词不能直接判定为强待机，这些词在普通对话中也会夹杂，
#      如果直接判定为待机会造成很严重的后果。这些词需要大模型进行判断
#      是否是进入待机的意图，确认是进入待机意图才能让设备待机。」
#    「今天太累了，我睡了」是在讲自己的状态；「昨天跟人聊到这儿就散了」
#    是在回忆 —— 直判待机会把正在进行的对话直接掐掉，而老人不会知道是
#    自己哪句话把设备关了。
# 所以这一组断言的作用是**防止有人再把它们挪回强档**，不是"顺手测一下"。
WEAK_CASES = [
    "不用了", "没事了", "停一下", "再见", "拜拜", "安静", "小点声",
    "休息吧", "我走了", "困了", "睡觉吧", "先这样吧",
    # ---- 结束语：必须交模型复核，不许直判 ----
    "先不聊了", "不聊了", "不聊了，我去做饭", "别聊了", "不用聊了",
    "聊到这儿吧", "聊到这吧", "不说了", "别说了", "不用说了",
    "不要再说了", "晚安", "晚安，我睡了", "睡吧", "我睡了", "我要睡了",
    "要睡了", "该睡了", "去睡了",
    # ---- 2026-11-03 用户实报「设备已经应答『有事随时叫我』，但是没有进入
    #      待机」：现场原句是「嗯，我不想说了。」—— 模型**已经**正确调了
    #      enter_standby，却被 _run_tool 的关键词闸门拒了，因为弱档里只有
    #      ``不说了``（字面三连），"不想说了"多一个"想"就匹配不上。
    #      这一组守住"这类说法必须落进弱档"，否则模型再判对也没用。
    #      ⚠️ 仍然是**弱**档：老人说"我不想说了"也可能只是"不想说这件事"，
    #      是否真待机由模型按上下文定（用户 2026-11-03 明确否决了升强档）。
    "嗯，我不想说了。", "我不想说了", "不想说了", "不想聊了", "不想讲了",
    "不想再聊", "不想再说了", "没什么好说的", "没啥好说的", "没话说",
    "说够了", "不问了", "先不聊",
]
for _t in WEAK_CASES:
    _tier = _dismiss_tier(_t)
    check(_tier == "weak", f"「{_t}」应判弱档（交模型复核，不许直判）",
          f"实际 {_tier!r}")

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


# ------------------- 4. 重新武装**不许**清零（用户实报的那条 bug）
async def _rearm_keeps_streak() -> None:
    """重新武装监听必须**保留**连续空识别计数。

    2026-11-03 用户实报：「他已经说了先不聊了，还是频繁在说抱歉我没听清」。
    根因就是这里原本清零：realtime 模式下每一轮 TTS 播完设备都会重发
    listen start ⇒ 每轮都清零 ⇒ 计数永远到不了阈值 ⇒ 每次都按"第 1 次"
    念 EMPTY_UTTERANCE_REPLY，silent / standby 两个分支从来没被执行过。

    所以断言要写成**反向**的：清零 = 红。
    """
    rec = _Recorder()
    sess = _make_session(rec)
    sess._empty_asr_streak = _EMPTY_ASR_STANDBY_STREAK

    async def _noop_watchdog() -> None:
        return

    sess._silence_watchdog = _noop_watchdog   # type: ignore[method-assign]
    sess._rearm_listening()
    await asyncio.sleep(0)                    # 让 create_task 起的看门狗跑完
    check(sess._empty_asr_streak == _EMPTY_ASR_STANDBY_STREAK,
          "重新武装监听：连续空识别计数**保持不变**（不许清零）",
          f"streak={sess._empty_asr_streak}")
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


# ------------- 6. 「裁剪把话切没了」→ 拿未裁剪音频重试一次
def _retry_decision_table() -> None:
    """``_should_retry_asr_untrimmed`` 的判据表（纯函数，逐格断言）。

    这张表的每一格都对应一个**真实出现过的**情形：
      · None            —— 这一轮压根没裁剪，重试 = 再送同一段，纯浪费；
      · 比值 < 1.5      —— 只裁掉头尾几十毫秒，同上；
      · 比值 ≥ 1.5      —— 2026-11-04 12:43 现场（366→30、244→62、114→27）；
      · 裁剪后为 0      —— 整段被判成静音，最该重试；
      · 超过时长上限    —— 让老人等 60 秒才听到"没听清"比立刻听到更糟。
    """
    f = vs._should_retry_asr_untrimmed
    check(f(None, 32000) is False, "未裁剪音频为 None：不重试", repr(f(None, 32000)))
    check(f(b"", 32000) is False, "未裁剪音频为空：不重试", repr(f(b"", 32000)))
    check(f(b"\x00" * 100000, 100000) is False,
          "没裁掉东西（比值 1.0）：不重试", "同一段音频重送没有意义")
    check(f(b"\x00" * 149999, 100000) is False,
          "只裁掉一丁点（比值 1.49）：不重试")
    check(f(b"\x00" * 150000, 100000) is True,
          "比值刚好 1.5：重试（判据是 ≥）")
    check(f(b"\x00" * 960000, 96000) is True,
          "现场比值 10×（22.0s → 3.0s）：重试")
    check(f(b"\x00" * 32000, 0) is True,
          "裁剪后一帧不剩：必须重试（整段被判成静音）")
    check(f(b"\x00" * int(61 * 32000), 32000) is False,
          "未裁剪超过 60 秒：不重试（等太久比没听清更糟）")
    check(f(b"\x00" * int(59 * 32000), 32000) is True,
          "未裁剪 59 秒：仍在上限内，重试")


async def _retry_paths() -> None:
    """``_resolve_empty_asr`` 三条路：重试成功 / 重试也空 / 根本没得重试。

    断言口径是**退避有没有被执行**（``_empty_asr_streak`` 与
    ``_speak_reply`` 的调用），而不是"返回值等于什么" —— 后者能靠
    return 一句话糊过去，前者才证明老人到底听到了什么。
    """
    # ---- 6a. 重试成功：不许进退避（否则老人还是听到「我没听清」）----
    rec = _Recorder()
    sess = _make_session(rec)
    calls: list[bytes] = []

    def _fake_asr(audio: bytes) -> str:
        calls.append(audio)
        return "我吃过药了"

    sess._asr_with_usage = _fake_asr        # type: ignore[method-assign]
    trimmed = b"\x01" * 64000
    raw = b"\x02" * 640000
    got = await sess._resolve_empty_asr(trimmed, raw, 0.0)
    check(got == "我吃过药了", "重试成功：返回重试识别出的文本", repr(got))
    check(calls == [raw], "重试成功：送给 ASR 的是**未裁剪**那一段",
          f"送进去 {[len(c) for c in calls]} 字节")
    check(not rec.spoken, "重试成功：不念「我没听清」", f"spoken={rec.spoken}")
    check(sess._empty_asr_streak == 0, "重试成功：连续空识别计数不增加",
          f"streak={sess._empty_asr_streak}")

    # ---- 6b. 重试也空：退避照旧执行（第 1 次念固定回复）----
    rec2 = _Recorder()
    sess2 = _make_session(rec2)
    sess2._asr_with_usage = lambda audio: ""   # type: ignore[method-assign]
    got2 = await sess2._resolve_empty_asr(trimmed, raw, 0.0)
    check(got2 == "", "重试也空：返回空串", repr(got2))
    check(len(rec2.spoken) == 1 and rec2.spoken[0] == EMPTY_UTTERANCE_REPLY,
          "重试也空：照旧念一次 EMPTY_UTTERANCE_REPLY", f"spoken={rec2.spoken}")
    check(sess2._empty_asr_streak == 1, "重试也空：计数 = 1（只算一次）",
          f"streak={sess2._empty_asr_streak}")

    # ---- 6c. 没裁剪过（untrimmed is None）：一次多余调用都不许发 ----
    rec3 = _Recorder()
    sess3 = _make_session(rec3)
    sent: list[bytes] = []

    def _never(audio: bytes) -> str:  # pragma: no cover - 不该被调用
        sent.append(audio)
        return "不该出现"

    sess3._asr_with_usage = _never       # type: ignore[method-assign]
    got3 = await sess3._resolve_empty_asr(trimmed, None, 0.0)
    check(got3 == "", "没裁剪过：直接走退避", repr(got3))
    check(sent == [], "没裁剪过：**不发**第二次 ASR 调用（不白花配额）",
          f"发了 {len(sent)} 次")
    check(sess3._empty_asr_streak == 1, "没裁剪过：计数 = 1",
          f"streak={sess3._empty_asr_streak}")

    # ---- 6d. 重试自己抛异常：不许把这一轮打崩，仍要走退避 ----
    rec4 = _Recorder()
    sess4 = _make_session(rec4)

    def _boom(audio: bytes) -> str:
        raise RuntimeError("厂商 502")

    sess4._asr_with_usage = _boom         # type: ignore[method-assign]
    got4 = await sess4._resolve_empty_asr(trimmed, raw, 0.0)
    check(got4 == "", "重试抛异常：按「还是没听清」处理，不外溢", repr(got4))
    check(len(rec4.spoken) == 1, "重试抛异常：退避仍然执行（老人听到回应）",
          f"spoken={rec4.spoken}")


def _retry_wired() -> None:
    """接线断言：空识别分支必须**经过**重试，不许直接掉进退避。

    这段没有可直调的纯函数（它在一轮对话的中段，前置依赖一整个会话与
    数据库），所以按源码相邻性断言，宁可变脆也不放任 —— 一旦有人把
    ``_handle_empty_asr(started)`` 直接写回空识别分支，重试就被绕过，
    而所有单元断言仍然全绿（这正是 2026-11-04 那三次空识别能连红的原因：
    "写了不跑"比"没写"更坏）。
    """
    import inspect

    src = inspect.getsource(VoiceSession._run_turn)
    check("untrimmed = self._pcm_untrimmed" in src,
          "_run_turn 取用未裁剪音频（接线）")
    check("self._pcm_untrimmed = None" in src,
          "_run_turn 取走后清空（不许残留到下一轮）")
    idx = src.find("ASR 返回空，音频指纹")
    check(idx > 0, "_run_turn 仍有空识别分支")
    window = src[idx: idx + 900] if idx > 0 else ""
    check("self._resolve_empty_asr(" in window,
          "空识别分支调用 _resolve_empty_asr（重试入口）")
    check("self._handle_empty_asr(" not in window,
          "空识别分支**不直接**调 _handle_empty_asr（否则绕过重试）")

    fin = inspect.getsource(VoiceSession._finalize)
    check("self._pcm_untrimmed = (" in fin,
          "_finalize 在裁剪处留一份未裁剪音频（接线）")
    check("_should_retry_asr_untrimmed" not in fin,
          "_finalize 只负责留素材，判据留在 _resolve_empty_asr（单一真源）")


def main() -> int:
    asyncio.run(_three_empties())
    asyncio.run(_quiet_while_dismissed())
    asyncio.run(_rearm_keeps_streak())
    _reset_on_real_content_wired()
    _retry_decision_table()
    asyncio.run(_retry_paths())
    _retry_wired()

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
