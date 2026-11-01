"""在生产库上走**语音链路真实的**提示词装配路径，把设备实际会发给大模型的
system prompt 打出来并做断言式核对。

为什么不用 ``build_persona_prompt`` 单独测：那条只覆盖画像 + 示例两段。
``build_system_prompt`` 才是 ``session.py`` 真正调用的入口 —— 身份段替换、
说话要求、记忆召回、示例注入的顺序与去重都在里面。分开测会漏掉
「画像段被注入两次」这类问题（2026-11-01 修的就是它）。

只读：不改任何行。用法（容器内，脚本从 stdin 进）：

    docker exec -i elder-server python - < tools/prod_prompt_probe.py

⚠️ ``examples_prompt`` 传空串是**对的**，不是偷懒：语音链路
（``session.py``）本身就传空串 —— 偏好示例已经由 ``recall_for_reply``
并进 ``memory_ctx``。传空串恰好复现真实路径，也正好把
``dedupe_tone(compiled, memory_ctx)`` 这段去重逻辑跑到。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from app.db import SessionLocal  # noqa: E402
from app.memory import bridge as memory_bridge  # noqa: E402
from app.voice.prompts import build_system_prompt  # noqa: E402
from app.voice.tools import TOOL_DEFS  # noqa: E402

DEVICE_PK = 1
USER_ID = 1          # 设备归属人（devices.owner_id），不是设备主键
AGENT_ID = 8         # ai_providers 里 record_type='role' 的那行「辉仔」

UTTERANCES = [
    "我今天想去买包子，你说买啥好",
    "我早上吃药了没有",
]

#: 生产库里那条 204 字 ASR 流水账的**特征片段**。不能拿整句的尾巴去比：
#: 打印时会被截断，尾巴根本不在输出里 ⇒ 假阴性（踩过一次）。
POLLUTION = ("水煎包儿有肉的话买肉的", "胡辣汤都说白借", "一一臭氧")

SECTIONS = ("【你是谁】", "【怎么说话】", "【这位老人习惯的说话方式】",
            "【家属的额外要求】", "【家属对你的特别交代】",
            "【他喜欢这样的表达，照着学】", "【下面这些说法他明确不喜欢")

TONE_HEAD = "【这位老人习惯的说话方式】"

NEW_TOOLS = ("get_wifi_info", "get_headlines", "set_status_light")


def main() -> int:
    fails: list[str] = []

    names = [d["function"]["name"] for d in TOOL_DEFS]
    print("== 工具表（%d 个）==" % len(names))
    print("  " + ", ".join(names))
    for t in NEW_TOOLS:
        ok = t in names
        print(f"  {'OK ' if ok else 'FAIL'} {t} 已注册")
        if not ok:
            fails.append(f"{t} 未注册")

    with SessionLocal() as db:
        for u in UTTERANCES:
            try:
                # 签名是 (db, user_id, agent_id, utterance, *, session_id, ...)
                # —— 四个位置参数。多写一个 8 会报
                # "takes 4 positional arguments but 5 were given"，
                # 然后 memory_ctx 静默变 0 字，看着像"记忆没召回"。
                ctx = memory_bridge.recall_for_reply(
                    db, USER_ID, AGENT_ID, u, session_id="probe-1")
            except Exception as exc:  # noqa: BLE001
                ctx = ""
                print("召回失败: %s: %s" % (type(exc).__name__, exc))
            prompt = build_system_prompt(
                db, DEVICE_PK, AGENT_ID, memory_ctx=ctx, examples_prompt="")

            print("\n" + "=" * 74)
            print("老人说：%s" % u)
            print("memory_ctx=%d 字  system=%d 字" % (len(ctx), len(prompt)))

            hit = [p for p in POLLUTION if p in prompt]
            print("  204 字污染片段命中：%s" % (hit or "无"))
            if hit:
                fails.append("prompt 里仍有 ASR 流水账污染")

            for key in SECTIONS:
                print("  %-24s %s" % (key, key in prompt))

            # 画像段**只能出现一次**。出现两次就是两个渲染器又分叉了：
            # 同一段画像在 prompt 里说两遍，措辞还不一样。
            n_tone = prompt.count(TONE_HEAD)
            print("  画像段出现次数：%d %s" % (n_tone, "OK" if n_tone == 1 else "FAIL"))
            if n_tone != 1:
                fails.append(f"画像段出现 {n_tone} 次（应为 1）")

            # 基础口令 1852 字是「角色不属于该用户」时的回退值。
            # 真拼上画像后必然明显更长。
            if len(prompt) < 2500:
                fails.append(f"system prompt 只有 {len(prompt)} 字，疑似回退了基础口令")

    print("\n" + "=" * 74)
    if fails:
        print("结论：**红**")
        for f in fails:
            print("  - " + f)
        return 1
    print("结论：**绿**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
