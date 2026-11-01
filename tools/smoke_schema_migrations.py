"""验证本轮新增的两条 schema 迁移在**真实老库数据**上的行为。

覆盖的三件事（都是「不看代码、只看行为」的断言）：

  1. ``backfill_token_epoch``：部署前就已解绑（owner_id IS NULL 且
     token_epoch = 0）的设备被抬到 1；**已绑定设备不动**；**已经抬过的
     不重复抬**（幂等 + 标记位）。
  2. ``normalize_device_macs``：带冒号 / 大写 / 带连字符的存量 MAC 统一成
     ``norm_mac`` 口径；``activation_codes.device_id`` 一起归一化；
     **重复执行不再改任何行**（patch_meta 生效）。
  3. **冲突不自动合并**：库里同时存在 ``aa:bb:cc:dd:ee:ff`` 与
     ``aabbccddeeff`` 两行时，函数必须**报错并零改动**，且**不登记**
     （下次启动重试，等人工处理）。这条是最容易写错的：直接 UPDATE 会撞
     ``device_id`` 唯一约束、在启动路径上抛异常 ⇒ 升级后服务起不来。

为什么单独写这个脚本而不是并进 smoke_activation_ttl
--------------------------------------------------
后者要起 uvicorn、跑完整 HTTP 流程（30s+）；这两个迁移是**纯数据变换**，
直接对着 SQLite 跑更快、失败信息也更准（能直接断言行内容）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"

_fail = 0
_pass = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _fail, _pass
    if ok:
        _pass += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="elder_mig_")
    db_path = Path(tmpdir) / "old.db"
    os.environ["ELDER_DB"] = f"sqlite:///{db_path.as_posix()}"
    os.environ.setdefault("ELDER_SEED_ADMIN_PASSWORD", "admin123456")
    sys.path.insert(0, str(SERVER))

    from sqlalchemy import text

    from app import models  # noqa: F401  建表用
    from app.db import Base, SessionLocal, engine
    from app import schema_patch

    Base.metadata.create_all(engine)

    # ---------------------------------------------------------------- 造老数据
    # 注意：这里**绕过 ORM 直接写 SQL**，因为要模拟「归一化之前入库的脏数据」，
    # 而 ORM 现在的写入路径已经会 norm_mac 了。
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO devices (id, device_id, name, owner_id, token_epoch, "
            "online, fw_version, hardware_model, settings, created_at, ip) VALUES "
            "(1, 'aabbccddee01', '已绑定设备', 1, 0, 0, '', '', '{}', "
            " '2026-01-01 00:00:00', ''),"
            "(2, 'aa:bb:cc:dd:ee:02', '部署前已解绑', NULL, 0, 0, '', '', '{}', "
            " '2026-01-01 00:00:00', ''),"
            "(3, 'AA-BB-CC-DD-EE-03', '从没绑定过', NULL, 0, 0, '', '', '{}', "
            " '2026-01-01 00:00:00', ''),"
            "(4, 'aabbccddee04', '解绑后又绑回来过', 2, 3, 0, '', '', '{}', "
            " '2026-01-01 00:00:00', '')"))
        conn.execute(text(
            "INSERT INTO activation_codes (code, device_id, used, attempts, "
            "max_attempts, expires_at, created_at) VALUES "
            "('1234567', 'aa:bb:cc:dd:ee:02', 0, 0, 5, "
            " '2030-01-01 00:00:00', '2026-01-01 00:00:00'),"
            "('7654321', 'AABBCCDDEE03', 0, 0, 5, "
            " '2030-01-01 00:00:00', '2026-01-01 00:00:00')"))

    # ------------------------------------------------- 1) 代次回填
    print("\n[1] backfill_token_epoch")
    n = schema_patch.backfill_token_epoch()
    check("首次执行改动 2 行（id=2、id=3，两台都未绑定且 epoch=0）", n == 2, f"got {n}")

    with SessionLocal() as db:
        epochs = dict(db.execute(text(
            "SELECT id, token_epoch FROM devices ORDER BY id")).all())
    check("id=2 已解绑设备 epoch 抬到 1", epochs[2] == 1, f"got {epochs[2]}")
    check("id=3 从没绑定过也抬到 1（无害）", epochs[3] == 1, f"got {epochs[3]}")
    check("id=1 已绑定设备**不动**（epoch 仍 0）", epochs[1] == 0, f"got {epochs[1]}")
    check("id=4 已绑定且 epoch=3 **不动**", epochs[4] == 3, f"got {epochs[4]}")

    n2 = schema_patch.backfill_token_epoch()
    check("第二次执行 0 行（patch_meta 标记生效）", n2 == 0, f"got {n2}")

    # ------------------------------------------------- 2) MAC 归一化
    print("\n[2] normalize_device_macs")
    n = schema_patch.normalize_device_macs()
    check("首次执行改动 2 行（id=2 带冒号、id=3 带连字符+大写）", n == 2, f"got {n}")

    with SessionLocal() as db:
        macs = dict(db.execute(text(
            "SELECT id, device_id FROM devices ORDER BY id")).all())
        codes = [r[0] for r in db.execute(text(
            "SELECT device_id FROM activation_codes ORDER BY id")).all()]

    check("id=2 归一化为无分隔符小写", macs[2] == "aabbccddee02", macs[2])
    check("id=3 归一化为无分隔符小写", macs[3] == "aabbccddee03", macs[3])
    check("id=1 原本就规范、保持不变", macs[1] == "aabbccddee01", macs[1])
    check("activation_codes 带冒号行已归一化", codes[0] == "aabbccddee02", codes[0])
    check("activation_codes 大写行已归一化", codes[1] == "aabbccddee03", codes[1])

    n2 = schema_patch.normalize_device_macs()
    check("第二次执行 0 行（已登记，不再扫改）", n2 == 0, f"got {n2}")

    # ------------------------------------------------- 3) 冲突不自动合并
    print("\n[3] normalize_device_macs 遇到唯一约束冲突")
    # 新建一个库：同一个归一化键对应两行（这正是 macs.py 描述的"两种写法都进过库"）
    db2 = Path(tmpdir) / "dupe.db"
    engine.dispose()
    os.environ["ELDER_DB"] = f"sqlite:///{db2.as_posix()}"
    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    # ⚠️ 必须重新 import models：Base.metadata 是**空**的，表定义是 import
    #    models 时注册上去的。漏这一句 create_all 什么也不建，后面 INSERT
    #    会报 "no such table"（本脚本第一版就踩了）。
    from app import models as _models2  # noqa: F401
    from app.db import Base as Base2, engine as engine2
    from app import schema_patch as sp2

    Base2.metadata.create_all(engine2)
    with engine2.begin() as conn:
        conn.execute(text(
            "INSERT INTO devices (id, device_id, name, owner_id, token_epoch, "
            "online, fw_version, hardware_model, settings, created_at, ip) VALUES "
            "(1, 'aa:bb:cc:dd:ee:ff', 'A', NULL, 0, 0, '', '', '{}', "
            " '2026-01-01 00:00:00', ''),"
            "(2, 'aabbccddeeff', 'B', NULL, 0, 0, '', '', '{}', "
            " '2026-01-01 00:00:00', '')"))

    n = sp2.normalize_device_macs()
    check("冲突时返回 0（零改动）", n == 0, f"got {n}")
    with engine2.begin() as conn:
        rows = dict(conn.execute(text(
            "SELECT id, device_id FROM devices ORDER BY id")).all())
        applied = conn.execute(text(
            "SELECT patch_id FROM patch_meta WHERE patch_id = 'device_mac_norm_v1'"
        )).first()
    check("冲突时两行**原样保留**（没被悄悄合并/删除）",
          rows[1] == "aa:bb:cc:dd:ee:ff" and rows[2] == "aabbccddeeff", str(rows))
    check("冲突时**不登记** patch_id（下次启动会重试）", applied is None, str(applied))

    # ------------------------------------------------- 4) norm_mac 行为契约
    #
    # ⚠️ 这里的期望值是**硬编码**的，不是用 norm_mac 自己算出来再比。
    # 理由：`norm_mac` 是「存量能否收敛」的唯一依赖 —— 它一旦被改成更严格的
    # 实现（比如「只允许 :/- 并校验 12 位十六进制」），迁移的兜底能力会**同时**
    # 失效，而这种退化不会有任何报错。判据必须与被测实现**不同源**，否则实现
    # 写错时两边一起错、判据照样绿（本轮在 collation 和 SQL 判据上各栽过一次）。
    print("\n[4] norm_mac 行为契约（硬编码期望值）")
    from app.macs import norm_mac, mac_variants

    cases = [
        ("aabbccddeeff", "aabbccddeeff"),
        ("AA:BB:CC:DD:EE:FF", "aabbccddeeff"),
        ("aa-bb-cc-dd-ee-ff", "aabbccddeeff"),
        ("Aa:Bb:Cc:Dd:Ee:Ff", "aabbccddeeff"),   # 混合大小写
        ("  aabbccddeeff  ", "aabbccddeeff"),    # 空白（isalnum 副作用，已固化为契约）
        ("aa:bb-cc:dd-ee-ff", "aabbccddeeff"),   # 混合分隔符
        ("", ""),
    ]
    for raw, want in cases:
        got = norm_mac(raw)
        check(f"norm_mac({raw!r}) == {want!r}", got == want, f"got {got!r}")

    # mac_variants 必须**总是**包含 norm 本身（迁移跑过之后只剩它一个）
    for raw, want in cases:
        check(f"mac_variants({raw!r}) 含规范化形式",
              want in mac_variants(raw) if want else True, str(mac_variants(raw)))

    # 6 种形态：{无分隔, 冒号, 连字符} × {大写, 小写}
    vs = mac_variants("aabbccddeeff")
    check("mac_variants 返回 6 种形态且封顶", len(vs) == 6, f"{len(vs)}: {vs}")

    # ------------------------------------------------- 4) 多选 trait 旧格式迁移
    #
    # ``topic_scope`` 从单选改成多选后，老库里是字符串 'daily'，而新代码要求
    # list[str]：``is_valid('daily')`` 为假 ⇒ ``spec.validate()`` 抛 ⇒
    # ``load_agent_persona`` 整体回退到基础口令。**生产上角色 id=8 就是
    # 'daily'** —— 不迁的话部署新版之后它照样没有人格，只是日志从
    # 「不属于该用户」变成「trait 取值非法」，同一句话两种死法。
    print("\n[4] traits 多选格式（str -> list）")
    # ⚠️ 必须**另起一个干净库**：上一节把 ELDER_DB 切到了 dupe.db 并清空了
    # sys.modules 里的 app.*，所以这里不重建就会对着 dupe.db 跑 —— 造的行
    # 在 old.db、迁移跑在 dupe.db，表现是"迁移一行都没改"，极难看出是环境问题
    # （第一版就是这么写的，调试了半天）。
    db3 = Path(tmpdir) / "traits.db"
    engine2.dispose()
    os.environ["ELDER_DB"] = f"sqlite:///{db3.as_posix()}"
    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    from app import models as M3          # noqa: F401  建表靠 import 注册
    from app.db import Base as Base3, SessionLocal as SL3, engine as engine3
    from app import main as app_main
    from app.persona.spec import from_row as _from_row

    Base3.metadata.create_all(engine3)

    with SL3() as db:
        rows = [
            ("老角色-str", {"verbosity": "normal", "topic_scope": "daily"}),
            ("老角色-非法值", {"topic_scope": "not_a_topic"}),
            ("已经是新格式", {"topic_scope": ["daily", "health"]}),
        ]
        made = []
        for name, traits in rows:
            r = M3.AIProvider(
                name=name, description="", system_prompt="", tts_voice="",
                kind="", record_type=M3.RECORD_TYPE_ROLE, owner_id=1,
                parent_id=None, traits=traits, guardrails={}, enabled=True,
            )
            db.add(r)
            db.flush()
            made.append(r.id)
        db.commit()

    app_main._seed_migration()

    with SL3() as db:
        got = {r.id: (r.name, r.traits.get("topic_scope"))
               for r in db.query(M3.AIProvider)
               .filter(M3.AIProvider.id.in_(made)).all()}

    check("str 'daily' 迁成 ['daily']",
          got[made[0]][1] == ["daily"], repr(got[made[0]][1]))
    check("迁后是 list（不是仍留着 str）",
          isinstance(got[made[0]][1], list), type(got[made[0]][1]).__name__)
    check("认不出的旧值回落到默认 ['daily']（不是原样留着、也不是空列表）",
          got[made[1]][1] == ["daily"], repr(got[made[1]][1]))
    check("已经是 list 的原样不动（幂等，不重排也不丢）",
          got[made[2]][1] == ["daily", "health"], repr(got[made[2]][1]))

    # 迁移的**目的**是让这些行能通过校验，而不是"字段看起来变了"。
    # 只断言形状不断言后果，等于没测（下一版 catalog 再改一次就又断了）。
    ok_all, bad = True, []
    with SL3() as db:
        for rid in made:
            try:
                _from_row(db.get(M3.AIProvider, rid), None).validate()
            except Exception as exc:  # noqa: BLE001
                ok_all = False
                bad.append(f"id={rid}:{type(exc).__name__}")
    check("迁移后三行都能过 spec.validate（这才是迁移的目的）", ok_all, ",".join(bad))

    app_main._seed_migration()
    with SL3() as db:
        again = {r.id: r.traits.get("topic_scope")
                 for r in db.query(M3.AIProvider)
                 .filter(M3.AIProvider.id.in_(made)).all()}
    check("再跑一次迁移不改任何值（幂等）",
          again == {made[0]: ["daily"], made[1]: ["daily"],
                    made[2]: ["daily", "health"]}, repr(again))

    print(f"\n{'=' * 60}\n结果：{_pass} 通过 / {_fail} 失败")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
