"""提醒软删除 + 操作审计流水 冒烟。

用法：
    python tools/smoke_reminder_audit.py [--port 8137]

为什么要有这个脚本（守的是什么）
--------------------------------
用户需求原话：「提醒的增删都放到 Docker 容器的数据库中，这样即便提醒删除了，
用户也能从 APP 和网页端看到历史上的提醒操作记录。」

⇒ 删除改成**软删除**（行留在库里）+ 一张只追加的 ``reminder_audits`` 流水表。

软删除引入的风险不是"看不到历史"，而是**删了还在响**：
``reminders`` 的行还在，任何一处读查询漏了 ``deleted_at IS NULL``，那条提醒
就会继续被排期、继续随设备心跳下发、继续在老人家里播报 —— 服药提醒重复
播报 = 让老人重复服药。**这条路径在代码里不报错、在日志里不显形**，
只有断言能抓。所以本脚本的每一条都对应一个具体的漏法：

    A. create      流水落库（含 via / action_label）
    B. update      before/after 能看出改了什么
    C. toggle      暂停 / 恢复两个方向都记
    D. delete      列表默认不含、include_deleted=1 含且 is_deleted=true
    E. delete      审计仍能查到 create + delete，title 是**当时**的标题
    F. delete      PENDING 实例清零  ← 「删了还在响」的机器证据
    G. restore     列表回来、is_deleted=false、有 restore 流水、实例重新排出
    H. 已删除       GET / PATCH / toggle 全部 404（对外表现为不存在）
    I. 越权        别人的 device_pk 查 audits 被拒
    J. can_restore 已删除时 true、活着时 false
    L. 先暂停再删除 → 恢复后必须**还是暂停态**（enabled 要还原，不能一律 True）
    M. 一条提醒多味药 + 剂量：JSON 列往返、老数据派生、PATCH 不清空、心跳下发、
       4 味上限

另有三条专门守**设备侧副作用**的断言：

    D4. 软删除要同时把 ``enabled`` 置 0 —— **第二道防线**，不是"老固件兜底"
        （下发 payload 本来就看 enabled，置 0 与过滤 deleted_at 对设备是同一个
        效果；老固件在"数组为空"时仍会保留本地旧表，只能等 OTA）。它的价值
        是让 materialize_events 等按 enabled 过滤的查询与 deleted_at 互为备份，
        且不留"已删除但仍启用"的自相矛盾数据。
    H6. 已删除规则的回执要返回 200 + ``ignored``，不能 4xx（设备会死循环重试）。
    H7. 已删除规则**不能**因为回执补建事件行（否则历史里出现"删了还吃过药"）。

三条实现约定（与生产代码一致，不要改成"更省事"的写法）
----------------------------------------------------
1. **直连 SQLite 数行**（``sqlite3.connect``）：F/G 两条验的是"库里到底有没有
   那些行"，问接口等于让被测方自证。服务端是**子进程**，所以用独立连接读同一个
   库文件（WAL 模式下并发读是安全的）。
2. **子进程输出落文件**，绝不接 PIPE（4KB 管道写满会把服务端永久阻塞，
   smoke_mail 的真实事故，见 smoke_home.py 的 SERVER_LOG 注释）。
3. **端口走 smoke_ports.py**（唯一真源），不在这里写死。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_common import unlink_retry  # noqa: E402
from smoke_ports import (  # noqa: E402
    detect_addr_in_use,
    guard_or_die,
    guard_port,
    release_port,
    resolve_port,
)

# ⚠️ 本地请求绝不走代理（本机/CI 都可能注入 http_proxy，绕一跳之后表现为
# TimeoutError + 零断言失败，极易被误判成代码回归）。见 smoke_home.py 同款注释。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_DIR = Path(os.environ.get("SMOKE_DB_DIR", str(Path(tempfile.gettempdir()))))
DB_PATH = DB_DIR / f"elder_smoke_reminder_audit_{os.getpid()}.db"
DB_FILE = DB_PATH if DB_PATH.is_absolute() else (SERVER / DB_PATH)
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")

PORT = resolve_port("smoke_reminder_audit")
BASE = f"http://127.0.0.1:{PORT}"

#: 与 smoke_home / smoke_auth / smoke_mail / smoke_admin_presets / smoke_server
#: 的 LEGACY_PW 同名同义（六处保持一致）。
LEGACY_PW = "admin123"
#: 种子管理员口令在 bb5ca98 之后不再硬编码，空库必须显式注入，否则本地裸跑 401。
SEED_PW_ENV = "ELDER_SEED_ADMIN_PASSWORD"

DEV_MAC = "auditdev000001"
DEV_MAC_2 = "auditdev000002"
#: M 段（多味药）专用：独立设备，避免多出来的提醒把 K1 的流水顺序断言弄红。
DEV_MAC_3 = "auditdev000003"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def req(method: str, path: str, body: dict | None = None, token: str | None = None,
        headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with _LOCAL_OPENER.open(r, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001  4xx 的 body 可能是纯文本，断言只看状态码
            return e.code, raw


def wait_ready(timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            code, _ = req("GET", "/api/health")
            if code == 200:
                return True
        except Exception:  # noqa: BLE001, S110  未就绪时连接被拒是常态
            pass
        time.sleep(0.6)
    return False


def _read_log_tail(max_bytes: int = 8192) -> str:
    try:
        return SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


# ================================================================ 直连 SQLite
def db_count(sql: str, params: tuple = ()) -> int:
    """数行数（独立连接，读同一个库文件）。WAL 下并发读安全。"""
    con = sqlite3.connect(str(DB_FILE), timeout=10)
    try:
        return int(con.execute(sql, params).fetchone()[0])
    finally:
        con.close()


def db_scalar(sql: str, params: tuple = ()):
    """取一个标量（第一行第一列）；无行返回 None。"""
    con = sqlite3.connect(str(DB_FILE), timeout=10)
    try:
        row = con.execute(sql, params).fetchone()
        return None if row is None else row[0]
    finally:
        con.close()


def db_row(sql: str, params: tuple = ()):
    """取一行（tuple）；无行返回 None。"""
    con = sqlite3.connect(str(DB_FILE), timeout=10)
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


def pending_events(reminder_pk: int) -> int:
    """该提醒**尚未触发**的实例数（state=0 PENDING）。

    ⚠️ 这是"删了还在响"的直接证据：设备按 PENDING 实例播报，残留一条就等于
    还会再响一次。
    """
    return db_count(
        "SELECT COUNT(*) FROM reminder_events WHERE reminder_id = ? AND state = 0",
        (reminder_pk,))


def db_exec(sql: str, params: tuple = ()) -> None:
    """直接写一行（独立连接）。

    用途：M3 要造一条**老数据**（``medicines`` 为空、只有老字段 ``dose``）。
    走接口造不出来 —— 接口现在一定会把 ``medicines`` 写进去，而"老数据长什么样"
    正是这一条要验的东西，只能绕过被测方直接落库。
    """
    con = sqlite3.connect(str(DB_FILE), timeout=10)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


# ================================================================ 主流程
def main() -> int:
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    env["ELDER_JWT_SECRET"] = "smoke-reminder-audit-secret"
    # 不跑后台调度：本脚本要精确控制"有没有 PENDING 实例"，让调度线程
    # 每 60s 去补排期会让 F 段（删除后必须为 0）随机变红。
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"
    env[SEED_PW_ENV] = LEGACY_PW

    if DB_FILE.exists():
        unlink_retry(DB_FILE)

    print(f"[boot] starting server on :{PORT} (db: {DB_FILE})")
    # 三段式端口守卫（假绿修复），与 smoke_home.py 逐段一致：
    # ① guard_or_die 事前检查 ② 显式持有 guard_port 的 hold 到 spawn 前才释放
    # ③ 起来之后校验输出里没有 "address already in use"。
    guard_or_die(PORT, "smoke_reminder_audit.py")
    hold, hold_hint = guard_port(PORT)
    if hold is None:
        print("[boot] " + hold_hint.replace("\n", "\n[boot] "))
        return 1
    release_port(hold, PORT)

    log_fp = SERVER_LOG.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(SERVER), env=env,
        stdout=log_fp, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        if not wait_ready():
            print(f"[boot] server did not become ready. log tail ({SERVER_LOG}):")
            print(_read_log_tail())
            return 1
        if detect_addr_in_use(_read_log_tail()):
            print(f"[boot] ✗ 端口 {PORT} bind 失败（占位未生效）。结果不可信，退出。")
            print("[boot]   请换端口重跑：python tools/smoke_reminder_audit.py <其它端口>")
            return 1
        print("[boot] ready\n")

        # ==================================================== 账号与设备
        code, tk = req("POST", "/api/auth/login",
                       {"username": "admin", "password": LEGACY_PW})
        check("登录默认管理员", code == 200 and bool((tk or {}).get("access_token")),
              str(code))

        def reg(name: str) -> str:
            req("POST", "/api/auth/register",
                {"username": name, "password": "test123456", "nickname": name})
            _, t = req("POST", "/api/auth/login",
                       {"username": name, "password": "test123456"})
            return (t or {}).get("access_token", "")

        u1, u2 = reg("audit_u1"), reg("audit_u2")
        check("注册并登录两个普通用户", bool(u1 and u2), f"u1={bool(u1)} u2={bool(u2)}")

        # 设备走「建档（即归属）→ OTA 拿 device_token」这条与固件完全相同的路，
        # 不要用取码绑定：建档之后设备已经是已绑定态，取码会 409（D44 §3）。
        code, dev = req("POST", "/api/devices", {"device_id": DEV_MAC}, u1)
        dev_pk = int((dev or {}).get("id", 0) or 0)
        _, ota = req("GET", "/api/voice/ota", None, None, {"Device-Id": DEV_MAC})
        dev_token = ((ota or {}).get("websocket") or {}).get("token", "")
        check("u1 绑定设备并拿到 device_token", dev_pk > 0 and bool(dev_token),
              f"pk={dev_pk} token={bool(dev_token)}")

        # u2 也建一台设备：越权断言要用"别人的 device_pk"。
        req("POST", "/api/devices", {"device_id": DEV_MAC_2}, u2)

        # ============================================ L. 暂停 → 删除 → 恢复
        # 放在 A 之前：K1 断言"设备级流水 create 在最末、restore 在最前"，
        # 这里先跑一遍完整生命周期，正好成为最末那条 create。
        print("── L. 先暂停、再删除 → 恢复后必须还是暂停态 ──")
        code, r2 = req("POST", f"/api/reminders?device_pk={dev_pk}", {
            "title": "晚上泡脚",
            "rtype": 0,
            "schedule_kind": 1,
            "times_of_day": ["21:00"],
            "enabled": True,
        }, u1)
        r2_pk = int((r2 or {}).get("id", 0) or 0)
        check("L1 建第二条提醒", code == 201 and r2_pk > 0, f"code={code} pk={r2_pk}")

        code, off2 = req("POST", f"/api/reminders/{r2_pk}/toggle", None, u1)
        check("L2 先暂停（enabled=false）",
              code == 200 and (off2 or {}).get("enabled") is False,
              f"code={code} enabled={(off2 or {}).get('enabled')}")

        code, _ = req("DELETE", f"/api/reminders/{r2_pk}", None, u1)
        check("L3 删除 204", code == 204, str(code))
        check("★ L4 删除后库里 enabled=0（第二道防线：与 deleted_at 互为备份，"
              "且已删行不该是启用态）",
              db_scalar("SELECT enabled FROM reminders WHERE id = ?", (r2_pk,)) == 0,
              str(db_scalar("SELECT enabled FROM reminders WHERE id = ?", (r2_pk,))))

        code, back2 = req("POST", f"/api/reminders/{r2_pk}/restore", None, u1)
        check("★ L5 恢复后仍是暂停态（还原删除前的 enabled，别把「我暂停过」弄丢）",
              code == 200 and (back2 or {}).get("enabled") is False,
              f"code={code} enabled={(back2 or {}).get('enabled')}")


        # ==================================================== A. create
        print("── A. 新增提醒 → create 流水 ──")
        # X-Client: app —— 顺带验 via 的判定（网页/APP 打同一套 API，只能靠头区分）
        code, rem = req("POST", f"/api/reminders?device_pk={dev_pk}", {
            "title": "早上吃药",
            "rtype": 0,
            "schedule_kind": 1,
            "times_of_day": ["08:00", "20:00"],
            "dose": "1 片",
            "require_confirm": True,
            "enabled": True,
        }, u1, {"X-Client": "app"})
        rem_pk = int((rem or {}).get("id", 0) or 0)
        check("A1 创建提醒 201", code == 201 and rem_pk > 0, f"code={code} pk={rem_pk}")
        check("A2 ReminderOut 带软删字段且 is_deleted=false",
              (rem or {}).get("is_deleted") is False
              and (rem or {}).get("deleted_at") is None
              and (rem or {}).get("deleted_via") == "",
              str({k: (rem or {}).get(k) for k in
                   ("is_deleted", "deleted_at", "deleted_by", "deleted_via")}))

        code, audits = req("GET",
                           f"/api/reminders/audits?device_pk={dev_pk}&reminder_id={rem_pk}",
                           token=u1)
        acts = [a.get("action") for a in (audits or [])]
        check("A3 audits 有 create 流水", code == 200 and acts == ["create"], str(acts))
        a0 = (audits or [{}])[0]
        check("A4 create 流水字段齐全（title / action_label / via_label）",
              a0.get("title") == "早上吃药" and a0.get("action_label") == "新增"
              and a0.get("via") == "app" and a0.get("via_label") == "手机 APP"
              and a0.get("actor_name") == "audit_u1",
              str({k: a0.get(k) for k in
                   ("title", "action_label", "via", "via_label", "actor_name")}))
        check("A5 create 流水的 summary 是中文一句话",
              a0.get("summary") == "新增提醒「早上吃药」", str(a0.get("summary")))
        check("A6 created_at 带时区偏移（不能是裸 naive 串）",
              isinstance(a0.get("created_at"), str)
              and ("+00:00" in a0["created_at"] or a0["created_at"].endswith("Z")),
              str(a0.get("created_at")))
        check("A7 can_restore=false（提醒还活着）", a0.get("can_restore") is False,
              str(a0.get("can_restore")))

        # ==================================================== B. update
        print("── B. 改标题 → update 流水（before/after 可对比）──")
        code, upd = req("PATCH", f"/api/reminders/{rem_pk}", {
            "title": "早上吃降压药",
            "rtype": 0,
            "schedule_kind": 1,
            "times_of_day": ["08:00", "20:00"],
            "dose": "1 片",
            "require_confirm": True,
            "enabled": True,
        }, u1, {"X-Client": "web"})
        check("B1 改标题 200", code == 200 and (upd or {}).get("title") == "早上吃降压药",
              f"code={code} title={(upd or {}).get('title')}")
        code, audits = req("GET",
                           f"/api/reminders/audits?device_pk={dev_pk}&reminder_id={rem_pk}",
                           token=u1)
        upd_row = next((a for a in (audits or []) if a.get("action") == "update"), None)
        check("B2 有 update 流水", code == 200 and upd_row is not None, str(code))
        # before/after 是**库里存的原文**，刻意不从接口读：接口不回这两列
        # （一页 100 条 × 两份 4KB 快照 = 800KB，前端也用不到），而且验
        # "到底存了什么"本来就该绕过被测方。
        raw = db_row("SELECT before_json, after_json FROM reminder_audits "
                     "WHERE reminder_id = ? AND action = 'update' "
                     "ORDER BY id DESC LIMIT 1", (rem_pk,)) or ("", "")
        before = json.loads(raw[0] or "{}")
        after = json.loads(raw[1] or "{}")
        check("B3 before/after 能看出改了什么（title 前后不同）",
              before.get("title") == "早上吃药" and after.get("title") == "早上吃降压药",
              f"before={before.get('title')} after={after.get('title')}")
        check("B4 update 流水的 title 是**改后**的标题（列表显示当前值）",
              (upd_row or {}).get("title") == "早上吃降压药",
              str((upd_row or {}).get("title")))

        # ==================================================== C. toggle
        print("── C. 暂停 / 恢复 → toggle_off / toggle_on ──")
        code, off = req("POST", f"/api/reminders/{rem_pk}/toggle", None, u1)
        check("C1 暂停成功且 enabled=false", code == 200 and (off or {}).get("enabled") is False,
              f"code={code} enabled={(off or {}).get('enabled')}")
        code, on = req("POST", f"/api/reminders/{rem_pk}/toggle", None, u1)
        check("C2 恢复成功且 enabled=true", code == 200 and (on or {}).get("enabled") is True,
              f"code={code} enabled={(on or {}).get('enabled')}")
        code, audits = req("GET", f"/api/reminders/audits?device_pk={dev_pk}", token=u1)
        acts = [a.get("action") for a in (audits or [])]
        check("C3 两个方向都记了（toggle_off + toggle_on）",
              "toggle_off" in acts and "toggle_on" in acts, str(acts))
        labels = {a.get("action"): a.get("action_label") for a in (audits or [])}
        check("C4 action_label 中文正确（暂停 / 恢复）",
              labels.get("toggle_off") == "暂停" and labels.get("toggle_on") == "恢复",
              str(labels))

        # ==================================================== D/E/F. delete
        print("── D/E/F. 删除 → 软删除 + 列表隐藏 + PENDING 清零 ──")
        # 删除前先确认"有 PENDING 实例"，否则 F 段的 0 可能是"本来就没有"。
        n_before = pending_events(rem_pk)
        check("F0 删除前该提醒有 PENDING 实例（否则 F 段的 0 无意义）",
              n_before > 0, f"{n_before} 条")

        code, _ = req("DELETE", f"/api/reminders/{rem_pk}", None, u1,
                      {"X-Client": "app"})
        check("D1 删除返回 204", code == 204, str(code))

        code, alive = req("GET", f"/api/reminders?device_pk={dev_pk}", token=u1)
        check("★ D2 默认列表**不含**已删除的提醒（否则等于没删）",
              code == 200 and all(r.get("id") != rem_pk for r in (alive or [])),
              f"code={code} n={len(alive or [])}")

        code, allrows = req("GET",
                            f"/api/reminders?device_pk={dev_pk}&include_deleted=1",
                            token=u1)
        hit = next((r for r in (allrows or []) if r.get("id") == rem_pk), None)
        check("★ D3 include_deleted=1 含这条且 is_deleted=true",
              code == 200 and hit is not None and hit.get("is_deleted") is True
              and hit.get("deleted_via") == "app" and hit.get("deleted_at") is not None,
              str({k: (hit or {}).get(k) for k in
                   ("is_deleted", "deleted_via", "deleted_at")}))

        n_after = pending_events(rem_pk)
        check("★ F1 删除后 PENDING 实例清零（这是「删了还在响」的机器证据）",
              n_after == 0, f"删前 {n_before} → 删后 {n_after}")
        check("★ D4 删除后库里 enabled=0（第二道防线：enabled 与 deleted_at 互为备份）",
              db_scalar("SELECT enabled FROM reminders WHERE id = ?", (rem_pk,)) == 0,
              str(db_scalar("SELECT enabled FROM reminders WHERE id = ?", (rem_pk,))))
        n_sync = 0
        code, sync = req("POST", "/api/device/sync", None, dev_token)
        if code == 200 and isinstance(sync, dict):
            n_sync = sum(1 for r in (sync.get("reminders") or [])
                         if r.get("id") == rem_pk)
        check("★ F2 设备心跳下发的规则里也没有这条（删了就不会再被下发）",
              code == 200 and n_sync == 0, f"code={code} 命中={n_sync}")

        code, audits = req("GET", f"/api/reminders/audits?device_pk={dev_pk}", token=u1)
        acts = [a.get("action") for a in (audits or [])]
        check("★ E1 删除后 audits 仍能查到这条的历史（create + delete 都在）",
              code == 200 and "create" in acts and "delete" in acts, str(acts))
        del_row = next((a for a in (audits or []) if a.get("action") == "delete"), None)
        check("E2 delete 流水的 title 是删除时的标题",
              (del_row or {}).get("title") == "早上吃降压药",
              str((del_row or {}).get("title")))
        check("E3 delete 流水的 via 是 app / actor 是操作人",
              (del_row or {}).get("via") == "app"
              and (del_row or {}).get("actor_name") == "audit_u1",
              str({k: (del_row or {}).get(k) for k in ("via", "actor_name")}))
        check("★ J1 已删除时 can_restore=true", (del_row or {}).get("can_restore") is True,
              str((del_row or {}).get("can_restore")))

        # ==================================================== H. 已删除 = 不存在
        print("── H. 已删除的提醒对外表现为「不存在」──")
        code, _ = req("GET", f"/api/reminders/{rem_pk}", token=u1)
        check("H1 GET /{id} → 404", code == 404, str(code))
        code, _ = req("PATCH", f"/api/reminders/{rem_pk}",
                      {"title": "改名试试", "rtype": 0, "schedule_kind": 1,
                       "times_of_day": ["08:00"]}, u1)
        check("H2 PATCH /{id} → 404", code == 404, str(code))
        code, _ = req("POST", f"/api/reminders/{rem_pk}/toggle", None, u1)
        check("H3 toggle /{id} → 404", code == 404, str(code))
        code, _ = req("DELETE", f"/api/reminders/{rem_pk}", None, u1)
        check("H4 再删一次 → 404（幂等口径：已删的不再重复成功）", code == 404, str(code))
        code, _ = req("POST", f"/api/reminders/{rem_pk}/confirm", None, u1)
        check("H5 confirm /{id} → 404（不能给已删的提醒做确认）", code == 404, str(code))

        # 设备断网期间的回执：规则在这期间被软删了，联网后补发。
        # 若照旧补建事件行，家属会看到"删掉的提醒还显示吃过药"。
        due = "2026-01-01T08:00:00"
        code, ig = req("POST", "/api/device/events", {
            "reminder_id": rem_pk,
            "due_at": due,
            "state": 2,            # EventState.CONFIRMED
            "confirm_method": 2,   # ConfirmMethod.BUTTON
            "confirmed_at": due,
        }, dev_token)
        check("★ H6 已删规则的回执 → 200 且标记 ignored（不能 4xx，设备会死循环重试）",
              code == 200 and isinstance(ig, dict)
              and ig.get("ignored") == "reminder_deleted", f"{code} {ig}")
        n_ev = db_count("SELECT COUNT(*) FROM reminder_events "
                        "WHERE reminder_id = ? AND due_at LIKE '2026-01-01%'",
                        (rem_pk,))
        check("★ H7 已删规则不因回执补建事件行（否则历史里出现「删了还吃过药」）",
              n_ev == 0, f"{n_ev} 条")

        # ==================================================== I. 越权
        print("── I. 越权：别人的设备 ──")
        code, _ = req("GET", f"/api/reminders/audits?device_pk={dev_pk}", token=u2)
        check("I1 他人查我设备的 audits → 403", code == 403, str(code))
        code, _ = req("GET", "/api/reminders/audits", token=u1)
        check("I2 缺 device_pk → 422（必填参数，不能默认查全量）", code == 422, str(code))
        code, _ = req("GET", "/api/reminders/audits")
        check("I3 未登录 → 401", code == 401, str(code))

        # ==================================================== G. restore
        print("── G. 恢复 → 列表回来 + 重新排期 ──")
        code, back = req("POST", f"/api/reminders/{rem_pk}/restore", None, u1,
                         {"X-Client": "web"})
        check("G1 恢复 200、is_deleted=false、enabled 还原成删除前的 true",
              code == 200 and (back or {}).get("is_deleted") is False
              and (back or {}).get("deleted_at") is None
              and (back or {}).get("enabled") is True,
              f"code={code} is_deleted={(back or {}).get('is_deleted')} "
              f"enabled={(back or {}).get('enabled')}")

        code, alive = req("GET", f"/api/reminders?device_pk={dev_pk}", token=u1)
        check("G2 恢复后回到默认列表", code == 200
              and any(r.get("id") == rem_pk for r in (alive or [])),
              f"code={code} n={len(alive or [])}")

        code, audits = req("GET", f"/api/reminders/audits?device_pk={dev_pk}", token=u1)
        acts = [a.get("action") for a in (audits or [])]
        check("G3 有 restore 流水", "restore" in acts, str(acts))
        del_row = next((a for a in (audits or []) if a.get("action") == "delete"), None)
        check("★ J2 恢复之后 can_restore 变回 false（按钮要反映现状）",
              (del_row or {}).get("can_restore") is False,
              str((del_row or {}).get("can_restore")))

        n_restored = pending_events(rem_pk)
        check("★ G4 恢复后 PENDING 实例重新排出来（否则「恢复了但永远不响」）",
              n_restored > 0, f"{n_restored} 条")
        code, sync = req("POST", "/api/device/sync", None, dev_token)
        n_sync = 0
        if code == 200 and isinstance(sync, dict):
            n_sync = sum(1 for r in (sync.get("reminders") or [])
                         if r.get("id") == rem_pk)
        check("G5 恢复后设备心跳又能拿到这条规则", code == 200 and n_sync == 1,
              f"code={code} 命中={n_sync}")

        # ==================================================== M. 多味药
        # 单独一台设备：K1 断言"本设备最新一条流水是 restore、最末是 create"，
        # 在这台设备上多建几条提醒会把它弄红 —— 而那条断言本身是有价值的。
        print("── M. 一条提醒多味药 + 各自剂量 ──")
        _, dev3 = req("POST", "/api/devices", {"device_id": DEV_MAC_3}, u1)
        dev_pk3 = int((dev3 or {}).get("id", 0) or 0)
        check("M0 第三台设备就绪（多药断言用独立设备，别打乱 K1 的顺序）",
              dev_pk3 > 0, f"pk={dev_pk3}")

        MEDS = [{"name": "降压药", "dose": "1片"}, {"name": "二甲双胍", "dose": "0.5g"}]
        code, mr = req("POST", f"/api/reminders?device_pk={dev_pk3}", {
            "title": "早上吃药",
            "rtype": 0,
            "schedule_kind": 1,
            "times_of_day": ["08:00"],
            "medicines": MEDS,
            "require_confirm": True,
            "enabled": True,
        }, u1)
        mr_pk = int((mr or {}).get("id", 0) or 0)
        check("M1 建一条带 2 味药的提醒 201，回包 2 项且**顺序一致**",
              code == 201 and mr_pk > 0
              and [m.get("name") for m in (mr or {}).get("medicines") or []]
              == ["降压药", "二甲双胍"]
              and [(m or {}).get("dose") for m in (mr or {}).get("medicines") or []]
              == ["1片", "0.5g"],
              f"code={code} medicines={(mr or {}).get('medicines')}")

        code, got = req("GET", f"/api/reminders/{mr_pk}", token=u1)
        check("M2 GET 回来仍是 2 项、顺序不变（JSON 列没有被打散/排序）",
              code == 200
              and [m.get("name") for m in (got or {}).get("medicines") or []]
              == ["降压药", "二甲双胍"],
              f"code={code} medicines={(got or {}).get('medicines')}")

        # ---- 老数据兼容：直接往库里插一条只有老字段的提醒 ----
        # 不写回填脚本（没有 Alembic、没有回滚），靠 schemas.ReminderOut 派生。
        # 这条断言就是"派生"这件事唯一的机器证据。
        db_exec(
            "INSERT INTO reminders (device_id, creator_id, title, speak_text, rtype,"
            " schedule_kind, times_of_day, weekdays, month_days, interval_days,"
            " dose, medicines, require_confirm, repeat_interval_min, repeat_times,"
            " escalate_after_min, confirm_methods, enabled, tz, created_at,"
            " updated_at, deleted_via)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dev_pk3, None, "吃钙片", "", 0, 1, '["09:00"]', "[]", "[]", 1,
             "1片", "[]", 1, 10, 3, 30, '["voice"]', 1, "",
             "2026-01-01 08:00:00", "2026-01-01 08:00:00", ""))
        legacy_pk = int(db_scalar(
            "SELECT id FROM reminders WHERE device_id = ? AND title = '吃钙片'",
            (dev_pk3,)) or 0)
        code, legacy = req("GET", f"/api/reminders/{legacy_pk}", token=u1)
        check("★ M3 老数据（medicines 为空、dose='1片'）→ GET 派生出 1 项"
              "「吃钙片 1片」（不靠回填脚本）",
              code == 200 and legacy_pk > 0
              and (legacy or {}).get("medicines") == [{"name": "吃钙片", "dose": "1片"}],
              f"code={code} medicines={(legacy or {}).get('medicines')}")

        # ---- PATCH 只传 title：medicines 必须**原样保留** ----
        # 这条守的是 update_reminder 的 model_dump(exclude_unset=True)。
        # 老语义是全量覆盖：没发的字段拿默认值（空数组）写回库，于是"旧版 APK
        # 改了个标题，多味药就被静默清空"—— 不报错、界面也不提示。
        code, pt = req("PATCH", f"/api/reminders/{mr_pk}", {"title": "早上吃药（改）"}, u1)
        check("★ M4 只 PATCH title → medicines 不被清空（exclude_unset 回归）",
              code == 200
              and [m.get("name") for m in (pt or {}).get("medicines") or []]
              == ["降压药", "二甲双胍"]
              and (pt or {}).get("title") == "早上吃药（改）",
              f"code={code} medicines={(pt or {}).get('medicines')}")

        # ---- 改 medicines：库要落新值，审计 after_json 要看得见 ----
        # 后者守的是 reminder_audit._SNAPSHOT_FIELDS 登记了 medicines ——
        # 漏登记的话流水会显示"什么都没改"（before/after 两份一样）。
        # 注意 title 必须一起带：ReminderIn 里 title 是**必填**（PATCH 用的也是
        # 同一个入参模型），只发 medicines 会 422，那验不到快照。
        code, mt = req("PATCH", f"/api/reminders/{mr_pk}", {
            "title": "早上吃药（改）",
            "medicines": [{"name": "降压药", "dose": "2片"}],
        }, u1)
        raw_med = db_row("SELECT medicines FROM reminders WHERE id = ?", (mr_pk,))
        # ⚠️ 必须 json.loads 之后再比：快照里的中文是 \\uXXXX 转义的，
        # 直接 ``"二甲双胍" not in after_json`` 恒为真（假绿）。
        snap = db_row("SELECT before_json, after_json FROM reminder_audits "
                      "WHERE reminder_id = ? AND action = 'update' "
                      "ORDER BY id DESC LIMIT 1", (mr_pk,)) or ("", "")
        snap_before = json.loads(snap[0] or "{}")
        snap_after = json.loads(snap[1] or "{}")
        check("★ M5 PATCH medicines → 库落新值，且审计 before/after 都带 medicines"
              "（快照漏登记就只显示「什么都没改」）",
              code == 200
              and [m.get("name") for m in (mt or {}).get("medicines") or []] == ["降压药"]
              and "二甲双胍" not in json.dumps(raw_med[0] if raw_med else "", ensure_ascii=False)
              and snap_before.get("medicines") == MEDS
              and snap_after.get("medicines") == [{"name": "降压药", "dose": "2片"}],
              f"code={code} db={raw_med} before={snap_before.get('medicines')} "
              f"after={snap_after.get('medicines')}")

        # ---- 设备心跳：JSON 列天然跟着下发（这正是不做子表的理由）----
        _, ota3 = req("GET", "/api/voice/ota", None, None, {"Device-Id": DEV_MAC_3})
        dev3_token = ((ota3 or {}).get("websocket") or {}).get("token", "")
        code, sync3 = req("POST", "/api/device/sync", None, dev3_token)
        hit3 = next((r for r in (sync3 or {}).get("reminders") or []
                     if r.get("id") == mr_pk), None)
        check("★ M6 设备心跳下发的规则里带 medicines（不用额外查询，也不会 N+1）",
              code == 200 and bool(dev3_token)
              and [m.get("name") for m in (hit3 or {}).get("medicines") or []]
              == ["降压药"],
              f"code={code} medicines={(hit3 or {}).get('medicines')}")

        # ---- 上限 4 味 ----
        code, over = req("POST", f"/api/reminders?device_pk={dev_pk3}", {
            "title": "五种药",
            "rtype": 0,
            "schedule_kind": 1,
            "times_of_day": ["08:00"],
            "medicines": [{"name": f"药{i}", "dose": "1片"} for i in range(5)],
        }, u1)
        check("★ M7 超过 4 味药 → 422（上限必须在服务端，前端拦得住 API 拦不住）",
              code == 422, str(code))
        code, longname = req("POST", f"/api/reminders?device_pk={dev_pk3}", {
            "title": "长药名",
            "rtype": 0,
            "schedule_kind": 1,
            "times_of_day": ["08:00"],
            "medicines": [{"name": "药" * 33, "dose": "1片"}],
        }, u1)
        check("M8 药名超 32 字 → 422（列宽 32，不拦就会被截断或撑爆布局）",
              code == 422, str(code))

        # ---- 存量超长 dose：读**不能** 500 ----
        # 本次给 ReminderIn.dose 补了 max_length=128（堵"以后还能写进去"），
        # 但 SQLite 不校验 VARCHAR 长度、改之前 API 也没拦 ⇒ 生产库里可能已经
        # 躺着更长的串。ReminderOut 因此**刻意覆盖**成不带 max_length 的字段：
        # 否则一条存量坏行会让 GET /api/reminders 与设备心跳一起 500
        # （一条坏行 = 整台设备一条提醒都同步不到）。
        db_exec(
            "INSERT INTO reminders (device_id, creator_id, title, speak_text, rtype,"
            " schedule_kind, times_of_day, weekdays, month_days, interval_days,"
            " dose, medicines, require_confirm, repeat_interval_min, repeat_times,"
            " escalate_after_min, confirm_methods, enabled, tz, created_at,"
            " updated_at, deleted_via)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dev_pk3, None, "超长剂量", "", 0, 1, '["10:00"]', "[]", "[]", 1,
             "片" * 200, "[]", 1, 10, 3, 30, '["voice"]', 1, "",
             "2026-01-01 08:00:00", "2026-01-01 08:00:00", ""))
        long_pk = int(db_scalar(
            "SELECT id FROM reminders WHERE device_id = ? AND title = '超长剂量'",
            (dev_pk3,)) or 0)
        code, longr = req("GET", f"/api/reminders/{long_pk}", token=u1)
        check("★ M9 存量超长 dose 仍能读出（不 500），派生值截到 64",
              code == 200 and long_pk > 0
              and len(((longr or {}).get("medicines") or [{}])[0].get("dose") or "") == 64,
              f"code={code} dose_len="
              f"{len(((longr or {}).get('medicines') or [{}])[0].get('dose') or '')}")

        # ==================================================== 收尾统计
        code, audits = req("GET", f"/api/reminders/audits?device_pk={dev_pk}", token=u1)
        acts = [a.get("action") for a in (audits or [])]
        check("K1 全量流水顺序为新→旧（create 在最末）",
              bool(acts) and acts[0] == "restore" and acts[-1] == "create", str(acts))
        code, lim = req("GET", f"/api/reminders/audits?device_pk={dev_pk}&limit=2",
                        token=u1)
        check("K2 limit 生效（只回 2 条）", code == 200 and len(lim or []) == 2,
              f"code={code} n={len(lim or [])}")
        code, bad = req("GET", f"/api/reminders/audits?device_pk={dev_pk}&limit=999",
                        token=u1)
        check("K3 limit 超上限 → 422（Query le=500）", code == 422, str(code))
        code, one = req("GET",
                        f"/api/reminders/audits?device_pk={dev_pk}&reminder_id={rem_pk}",
                        token=u1)
        check("K4 reminder_id 过滤生效且全属于这条提醒",
              code == 200 and bool(one)
              and all(a.get("reminder_id") == rem_pk for a in one),
              f"code={code} n={len(one or [])}")

        passed = sum(1 for _, ok, _ in results if ok)
        total = len(results)
        print(f"\n==== {passed}/{total} passed ====")
        for name, ok, detail in results:
            if not ok:
                print(f"  FAILED: {name} {detail}")
        return 0 if passed == total else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001  收尾失败不影响判定
            proc.kill()
        try:
            log_fp.close()
        except Exception:  # noqa: BLE001
            pass
        if DB_FILE.exists():
            try:
                unlink_retry(DB_FILE)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
