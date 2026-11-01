"""生产数据基线快照（只读）。

用途：部署前后各跑一次，逐项对账 —— 部署必须"除了预期变化，其余一字不差"。

用法（容器内，脚本从 stdin 进，不必落盘）：
    docker exec -i elder-server python - < tools/prod_baseline.py

⚠️ 两个踩过的坑（都写在这里，免得下次再撞）：
1. **"角色"不是 ``roles`` 表**：本库没有 roles 表，角色是
   ``ai_providers`` 里 ``record_type='role'`` 的行（同一张表还混着
   ``config`` / ``preset`` / ``llm`` 三种记录）。``devices`` 用 ``agent_id``
   指向它，``owner_id`` 才是归属人。
2. **``devices`` 没有 ``role_id``**：写 ``select role_id from devices``
   会得到 ``no such column``，然后整个脚本死在第一段 —— 只读快照脚本
   报错会让人误以为"库坏了"。
"""
from __future__ import annotations

import json
import sqlite3

DB = "/data/elder.db"

TABLES = [
    "users", "devices", "ai_providers", "avatars", "agent_memories",
    "agent_memory_entities", "agent_preference_examples", "agent_persona_audits",
    "agent_persona_profiles", "chat_messages", "interactions", "reminders",
    "reminder_events", "model_usage",
]

#: 多选型 trait：值应为 list。旧格式是 str，新代码会抛 TraitValueError。
MULTI_TRAITS = ("topic_scope", "interests", "topics", "habits")


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    print("== 表行数 ==")
    for t in TABLES:
        try:
            n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error as exc:
            n = f"ERR({exc})"
        print(f"  {t:26} {n}")

    print("\n== 设备 → 角色归属（必须 owner 一致，否则回退 1852 字基础口令）==")
    for r in cur.execute(
            "SELECT d.id, d.device_id, d.name, d.owner_id AS dev_owner,"
            " d.agent_id, d.online, d.last_seen_at,"
            " a.name AS agent_name, a.owner_id AS agent_owner,"
            " a.record_type, a.deleted_at"
            " FROM devices d LEFT JOIN ai_providers a ON a.id = d.agent_id"):
        print(f"  device {r['id']} {r['device_id']} owner={r['dev_owner']} "
              f"online={r['online']} last_seen={r['last_seen_at']}")
        print(f"    → agent {r['agent_id']} {r['agent_name']!r} "
              f"type={r['record_type']} owner={r['agent_owner']} "
              f"deleted={r['deleted_at']}")

    print("\n== 记忆 / few-shot 有效性 ==")
    # ⚠️ ``agent_memories`` 没有 ``status`` 列（写 ``group by status`` 会报
    # ``no such column``）。"有效 / 被压制"是用 ``superseded_at`` 是否为 NULL
    # 表达的 —— 与 few-shot 的 ``status`` 是两套口径，别混。
    try:
        r = cur.execute(
            "SELECT COUNT(*) total,"
            " SUM(superseded_at IS NULL) alive,"
            " SUM(superseded_at IS NOT NULL) superseded FROM agent_memories"
        ).fetchone()
        print(f"  agent_memories: total={r['total']} 有效={r['alive']} "
              f"被压制={r['superseded']}")
    except sqlite3.Error as exc:
        print(f"  agent_memories: ERR({exc})")
    try:
        rows = list(cur.execute(
            "SELECT status, COUNT(*) n FROM agent_preference_examples "
            "GROUP BY status"))
        print("  agent_preference_examples: "
              + ", ".join(f"{r['status']}={r['n']}" for r in rows))
    except sqlite3.Error as exc:
        print(f"  agent_preference_examples: ERR({exc})")

    print("\n== 角色的多选 trait 值类型（部署后应全是 list）==")
    for r in cur.execute(
            "SELECT id, name, traits FROM ai_providers "
            "WHERE record_type = 'role' AND deleted_at IS NULL"):
        try:
            traits = json.loads(r["traits"] or "{}")
        except Exception:  # noqa: BLE001
            print(f"  agent {r['id']} {r['name']!r} traits 解析失败")
            continue
        got = {k: type(traits.get(k)).__name__
               for k in MULTI_TRAITS if k in traits}
        bad = {k: v for k, v in got.items() if v != "list"}
        print(f"  agent {r['id']} {r['name']!r} 多选字段={got} "
              f"{'← 需迁移' if bad else ''}")

    print("\n== 提醒 ==")
    try:
        r = cur.execute(
            "SELECT COUNT(*) total, SUM(enabled) en,"
            " SUM(deleted_at IS NOT NULL) del FROM reminders").fetchone()
        print(f"  total={r['total']} enabled={r['en']} deleted={r['del']}")
    except sqlite3.Error as exc:
        print(f"  ERR({exc})")

    con.close()
    print("\nOK")


if __name__ == "__main__":
    main()
