"""语气画像的**唯一**渲染器：把画像数值写成一句（段）人话。

为什么单独一个模块（2026-11-01）
-------------------------------
原先这句话有**两份实现**：

  * ``memory/persona.py`` 的 ``describe_tone()`` —— 给家属读的画像描述、
    偏好示例段（``build_persona_prompt``）、以及语音链路的 ``memory_ctx``；
  * ``persona/compiler.py`` 的 ``_section_persona()`` —— 给 system prompt 的
    「这位老人习惯的说话方式」段。

两份实现很快就分叉了，而且分叉**不报错**：
``_section_persona`` 漏渲染 ``manual.note``（家属手填的画像备注），
对空值的处理也不同（``avg_reply_len == 0`` 时一个输出"还没有足够数据"、
一个什么都不输出）。结果生产 prompt 里同一段画像**出现两次且措辞不同** ——
模型收到两句互相矛盾的自我描述，而日志里一切正常。

修法不是"让两份实现保持一致"（那种承诺在实践中必然失守），而是**只留一份**。

为什么放在 ``app/persona/`` 而不是 ``app/memory/``
------------------------------------------------
``app/persona/`` 有一条硬约束（见该包 ``__init__`` 的 docstring）：
**不 import app.models / app.db**，好在"没有 engine、没有库"的纯函数环境里跑
（eval 套件靠这一点在 CI 里不起服务就能断言医疗红线齐全）。
本模块只依赖 ``typing``，所以放进 persona 包完全合规；
反过来放进 ``app/memory/`` 会让 ``compiler`` 去 import 一个带 models 的模块，
直接踩穿那条约束。

⚠️ 本模块**不做清洗**：``note`` 是家属自由文本，属于不可信输入。
调用方必须自己过 ``sanitize_for_prompt`` 再用 —— 两个调用方都是这么做的
（``build_persona_prompt`` 清洗 description，``_section_persona`` 走 ``_clean``）。
把清洗放进来会让"同一句话"在两个调用点得到不同结果，去重就失效了。
"""
from __future__ import annotations

from typing import Any

__all__ = ["describe_tone"]


def describe_tone(stats: dict[str, Any], effective: dict[str, Any]) -> str:
    """把画像写成一句（段）人话：既注入 prompt，也直接展示给家属。

    :param stats: 统计侧取值，本函数只用到 ``catchphrases``
        （口头禅是**统计**出来的，不是配置项）。
    :param effective: 已合并的生效值（``avg_reply_len`` / ``address`` /
        ``humor`` / ``speed`` / ``note``）。优先级链由调用方保证。
    """
    parts: list[str] = []
    avg = float(effective.get("avg_reply_len") or 0.0)
    if avg <= 0:
        parts.append("回复长度：还没有足够数据，先按简短、清楚来说")
    elif avg <= 18:
        parts.append(f"回复要短，平均每句 {avg:.0f} 字左右，一次只说一件事")
    elif avg <= 45:
        parts.append(f"回复中等长度，平均每句 {avg:.0f} 字左右")
    else:
        parts.append(f"可以稍微多说一点，平均每句 {avg:.0f} 字左右")

    addr = str(effective.get("address") or "")
    if addr:
        parts.append(f"称呼对方用「{addr}」")

    parts.append("可以适度开开玩笑" if effective.get("humor") else "少开玩笑，说正事为主")

    speed_map = {"slow": "语速放慢、句子之间多停顿", "normal": "语速正常",
                 "fast": "语速可以稍快"}
    parts.append(speed_map.get(str(effective.get("speed") or "normal"), "语速正常"))

    phrases = [p for p in (stats.get("catchphrases") or []) if len(p) >= 3][:3]
    if phrases:
        parts.append("常用口头禅：" + "、".join(phrases))

    note = str(effective.get("note") or "").strip()
    if note:
        parts.append("家属特别交代：" + note)
    return "；".join(parts) + "。"
