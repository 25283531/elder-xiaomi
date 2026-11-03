"""人格预设实报回归：数值档位区间匹配 / 保存真生效 / 音色链路。

用法：
    python tools/smoke_role_traits_voice.py [--port 8139]

## 这个脚本守的是 2026-11-03 的三条用户实报（原话）

1. 「用户 web 页面中的人格预设页面，爱开玩笑这一项的 3 个档次，每次打开页面
   都提示『没有当前值 0.1 不在任何档位里，请选一个档位』，保存根本没有生效。」
2. 「另外人格预设的音色也从来没有生效。」
3. 「保存人格预设后，重新打开页面看到的还是旧值（保存不生效）。」

## 三条各自的根因

**A. 档位是区间，前端做了精确相等匹配**
``catalog.TraitSpec.to_dict()`` 给数值型 trait 的 ``options`` 是
``{"value": 上界}`` —— 0.3 / 0.6 / 1.0，不是"可选值"。0.1 属于第一档
（0.0~0.3），而 ``bandEffect`` / ``isBandOn`` 用 ``String(o.value) ===
String(value)`` 精确比较 ⇒ 既报「不在任何档位」，三个按钮也全不亮。
本脚本读 ``user.html`` 源码钉住「区间归属」，并确认枚举/多选仍是精确匹配。

**B. 0.1 不是脏数据，是画像修正值冒充了用户设置**
``compiler.effective_traits()`` 把画像里的布尔 humor 映射成 **0.1 / 0.7**
覆盖声明值；而 ``RoleOut.traits_effective`` 曾经直接返回它。用户端编辑弹窗
把这个字段当"当前值"显示 ⇒ 永远显示 0.1（0.1 不是任何档位的上界）；
保存 0.6 后再打开，画像又覆盖回 0.1 ⇒ 用户看到「保存没生效」，库里其实一直是 0.6。
本脚本走 HTTP：PATCH 数值 → GET 必须**逐字**回同一个数（含非档位值 0.55），
存储层不许量化，``traits_effective`` 也不许被画像改写。

**C. 音色：合成路径里配置行压过了角色**
``asr_tts.tts()`` 原写法 ``_row_voice(row) or voice`` —— 只要管理员在
「TTS 配置」里选过音色，角色音色就永远被忽略，而音色是角色的属性
（``device_api.sync`` 只下发 ``role.tts_voice``）。本脚本守「保存 → 重新读」
链路与解析式的**操作数顺序**（角色在前、配置行兜底）；
合成行为断言在 ``smoke_tencent_voice`` 的 K7/K8（那里有现成的厂商打桩）。

## 为什么不测真调厂商 / 不跑前端

真打 TTS 要密钥、要花钱，失败原因与本仓代码无关。前端没有构建产物可执行，
本仓既有做法就是「读 static/*.html 源码 + 字符串断言」（见 smoke_lights /
smoke_voice_catalog），这里沿用：档位算法的语义用后端的 prompt 渲染交叉验证
（同一份 bands，后端 ``to_prompt_sentence`` 就是区间匹配）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
USER_HTML = SERVER / "app" / "static" / "user.html"
ASR_TTS = SERVER / "app" / "voice" / "asr_tts.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_common import cleanup_db, seed_admin_env, unlink_retry  # noqa: E402
from smoke_ports import guard_or_die, resolve_port  # noqa: E402

# ⚠️ 本地请求绝不走系统代理（同其余冒烟脚本）：本机设了 http_proxy 时，
# 「访问自己刚起的 127.0.0.1」会被绕到代理，表现为超时且零断言失败 —— 极像回归。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DB_PATH = Path(os.environ.get(
    "SMOKE_ROLE_TRAITS_VOICE_DB",
    str(Path(tempfile.gettempdir()) / f"elder_smoke_rtv_{os.getpid()}.db")))
if DB_PATH.exists():
    unlink_retry(DB_PATH)

PORT = resolve_port("smoke_role_traits_voice")
BASE = f"http://127.0.0.1:{PORT}"

# 子进程输出**落文件**，绝不接无人消费的 PIPE（Windows 4KB 管道缓冲会锁死服务端）。
SERVER_LOG = DB_PATH.with_name(DB_PATH.stem + "_server.log")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def req(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with _LOCAL_OPENER.open(r, timeout=40) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def wait_ready(proc, timeout: float = 120.0) -> bool:
    """等服务就绪，**同时看自己起的进程是否还活着**（端口被占会探到别人的服务）。"""
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            if req("GET", "/api/health")[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.6)
    return False


def read_log_tail(max_bytes: int = 8192) -> str:
    try:
        return SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-max_bytes:]
    except OSError:
        return ""


# ================================================================ 源码工具
def method_body(src: str, name: str) -> str:
    """截出 ``name(...) { ... }`` 的方法体（按大括号配平）。

    为什么按大括号配平而不是「正则截到第一个 ``},``」：方法体里有对象字面量与
    嵌套 if，非贪婪正则会截短，断言就会在**不完整的片段**上做假判断 ——
    那种断言在修复前也可能绿，等于没守。
    """
    m = re.search(rf"\n\s+(?:async\s+)?{re.escape(name)}\s*\([^)]*\)\s*\{{", src)
    if not m:
        return ""
    i = m.end() - 1
    depth = 0
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[m.end():i]
        i += 1
    return ""


def num_eq(a, b) -> bool:
    """浮点等值比较：JSON 往返不保证 0.55 的二进制表示逐位一致。"""
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def role_in_list(rows: list, role_id: int) -> dict:
    for r in rows or []:
        if isinstance(r, dict) and int(r.get("id") or 0) == int(role_id):
            return r
    return {}


# ================================================================ A. 档位区间匹配
def t_band_matching() -> None:
    """A. 数值型档位必须按**区间**归属（读 user.html 源码）。

    这组断言在修复前全红：旧代码在 bandEffect / isBandOn 里都是精确相等。
    """
    print("\n── A. 数值档位按区间归属（user.html）──")
    src = USER_HTML.read_text(encoding="utf-8")
    idx = method_body(src, "bandIndex")
    on = method_body(src, "isBandOn")
    eff = method_body(src, "bandEffect")

    check("A1 bandIndex 存在（区间归属只有一份实现，两个调用点共用）", bool(idx))
    check("A2 bandIndex 用『当前值 <= 该档上界』找档位（不是精确相等）",
          "v <= Number(opts[i].value)" in idx, idx[:160].replace("\n", " "))
    check("A3 bandIndex 都不满足时取最后一档（超出上界不静默返回 -1）",
          "return opts.length - 1;" in idx, idx[-120:].replace("\n", " "))
    check("A4 isBandOn 的数值分支走 bandIndex（否则 0.1 时三个按钮全不亮）",
          "spec.kind === 'number'" in on and "this.bandIndex(spec, cur)" in on,
          on[:200].replace("\n", " "))
    check("A5 bandEffect 的数值分支走 bandIndex（否则报『不在任何档位』）",
          "spec.kind === 'number'" in eff and "this.bandIndex(spec, value)" in eff,
          eff[:200].replace("\n", " "))

    # 数值分支里不许再出现「不在任何档位」那句话 —— 它是 bug 1 的用户可见症状。
    num_branch = eff.split("spec.kind === 'number'", 1)[-1]
    num_branch = num_branch.split("const hit =", 1)[0]
    check("A6 数值分支存在且不再输出『不在任何档位里』（那句话只属于枚举的非法值）",
          "spec.kind === 'number'" in eff and "不在任何档位里" not in num_branch,
          num_branch[-160:].replace("\n", " "))

    # 枚举 / 多选保持精确匹配 —— 区间匹配只该用在 number 上。
    check("A7 枚举仍是精确匹配（没有被区间逻辑顺手改坏）",
          "String(cur) === String(value)" in on, on[-160:].replace("\n", " "))
    check("A8 多选仍按『当前数组里有没有这个值』判断",
          "Array.isArray(cur) && cur.map(String).indexOf(String(value)) >= 0" in on)
    check("A9 多选档位的效果文案仍逐项拼接（未被数值分支吞掉）",
          "arr.map(v =>" in eff, eff[:120].replace("\n", " "))


# ================================================================ B. 保存真生效
def t_save_traits(token: str) -> int:
    """B. PATCH 数值型 trait → GET 必须逐字回同一个数（存储层不许量化）。"""
    print("\n── B. 保存 traits 真生效（HTTP 往返）──")
    code, role = req("POST", "/api/roles", {"name": "数值档位回归"}, token)
    role = role if isinstance(role, dict) else {}
    rid = int(role.get("id") or 0)
    check("B0 建原创角色", code == 201 and rid > 0, f"code={code}")

    code, patched = req("PATCH", f"/api/roles/{rid}",
                        {"name": "数值档位回归", "traits": {"humor": 0.6,
                                                           "verbosity": "short"}}, token)
    check("B1 PATCH 档位值 0.6 -> 200", code == 200, f"code={code}")

    _, got = req("GET", f"/api/roles/{rid}", token=token)
    got = got if isinstance(got, dict) else {}
    check("B2 重新读：traits.humor 就是 0.6（不是默认值 / 不是旧值）",
          num_eq((got.get("traits") or {}).get("humor"), 0.6),
          str(got.get("traits")))
    # ⚠️ 这条是 bug 3 的核心：旧实现里 traits_effective 被画像改写成 0.1/0.7，
    # 页面拿它当"当前值"显示 ⇒ 保存后重新打开看到的永远是 0.1。
    check("B3 traits_effective 不被画像改写（0.6 不会被换成 0.1/0.7）",
          num_eq((got.get("traits_effective") or {}).get("humor"), 0.6),
          str(got.get("traits_effective")))
    check("B4 枚举型同样原样回读（verbosity=short）",
          (got.get("traits_effective") or {}).get("verbosity") == "short",
          str(got.get("traits_effective")))

    # 「重新打开页面」走的是列表接口 —— 必须和详情一致，否则用户看到的就是旧值。
    _, listing = req("GET", "/api/roles?scope=mine", token=token)
    row = role_in_list((listing or {}).get("mine") if isinstance(listing, dict) else [], rid)
    check("B5 列表接口（页面重新打开的数据源）也返回 0.6",
          num_eq((row.get("traits") or {}).get("humor"), 0.6)
          and num_eq((row.get("traits_effective") or {}).get("humor"), 0.6),
          f"traits={row.get('traits')} eff={row.get('traits_effective')}")

    # 非档位值：区间匹配是**显示层**的事，存储层不许量化成 0.6 / 0.3。
    code, _ = req("PATCH", f"/api/roles/{rid}",
                  {"name": "数值档位回归", "traits": {"humor": 0.55}}, token)
    _, got2 = req("GET", f"/api/roles/{rid}", token=token)
    got2 = got2 if isinstance(got2, dict) else {}
    check("B6 PATCH 非档位值 0.55 -> 200", code == 200, f"code={code}")
    check("B7 0.55 原样存回（存储层不量化；区间匹配只发生在显示层）",
          num_eq((got2.get("traits") or {}).get("humor"), 0.55),
          str(got2.get("traits")))
    check("B8 traits_effective 也保持 0.55", 
          num_eq((got2.get("traits_effective") or {}).get("humor"), 0.55),
          str(got2.get("traits_effective")))

    # 显示层的区间匹配与后端 prompt 渲染必须一致：0.55 落在 0.6 那一档。
    _, pr = req("GET", f"/api/roles/{rid}/prompt", token=token)
    prompt = (pr or {}).get("prompt") if isinstance(pr, dict) else ""
    check("B9 prompt 按区间渲染 0.55 -> 0.6 档文案（前后端同一份判据）",
          "偶尔可以开个小玩笑" in (prompt or ""), (prompt or "")[-200:].replace("\n", " "))

    # 0.1 是历史脏值（旧版页面把画像值存进来了）：必须合法、必须落在第一档。
    code, _ = req("PATCH", f"/api/roles/{rid}",
                  {"name": "数值档位回归", "traits": {"humor": 0.1}}, token)
    _, got3 = req("GET", f"/api/roles/{rid}", token=token)
    _, pr3 = req("GET", f"/api/roles/{rid}/prompt", token=token)
    check("B10 历史值 0.1 仍可保存（不写数据迁移，靠区间匹配兜住）",
          code == 200 and num_eq(((got3 or {}).get("traits") or {}).get("humor"), 0.1),
          f"code={code} traits={((got3 or {}).get('traits'))}")
    check("B11 0.1 渲染成第一档文案（少开玩笑）",
          "少开玩笑" in ((pr3 or {}).get("prompt") or ""),
          ((pr3 or {}).get("prompt") or "")[-200:].replace("\n", " "))

    # 校验不能被放松：超出 0~1 仍然 400。
    code_bad, _ = req("PATCH", f"/api/roles/{rid}",
                      {"name": "数值档位回归", "traits": {"humor": 1.5}}, token)
    check("B12 超出范围的 1.5 仍然 400（区间匹配不是放松校验）", code_bad == 400,
          f"code={code_bad}")

    # 派生角色：overlay 只存改过的项，其余继承母版。
    code_p, presets = req("GET", "/api/roles/presets", token=token)
    pid = int((presets or [{}])[0].get("id") or 0) if isinstance(presets, list) else 0
    if not pid:
        check("B13 从预设派生角色", False, f"拿不到预设：code={code_p}")
        return rid
    code_d, dv = req("POST", f"/api/roles/{pid}/derive", {"name": "数值派生", "inherit": False},
                     token)
    did = int((dv or {}).get("id") or 0)
    check("B13 从预设派生角色", code_d == 201 and did > 0, f"code={code_d}")
    code_dp, dpatched = req("PATCH", f"/api/roles/{did}",
                            {"name": "数值派生", "traits": {"humor": 0.9}}, token)
    _, dgot = req("GET", f"/api/roles/{did}", token=token)
    dgot = dgot if isinstance(dgot, dict) else {}
    check("B14 派生角色 overlay 只存改过的项（humor 一项，不是整份声明）",
          code_dp == 200 and (dgot.get("traits") or {}) == {"humor": 0.9},
          str(dgot.get("traits")))
    check("B15 派生角色 traits_effective = 母版 + 覆盖（humor 0.9）",
          num_eq((dgot.get("traits_effective") or {}).get("humor"), 0.9),
          str(dgot.get("traits_effective")))
    check("B16 派生角色未改的维度仍继承母版（verbosity 存在且不是空）",
          bool((dgot.get("traits_effective") or {}).get("verbosity")),
          str(dgot.get("traits_effective")))

    # 前端只提交改动项（否则派生角色的 overlay 会被一次保存抄成整份声明）。
    save_src = method_body(USER_HTML.read_text(encoding="utf-8"), "saveRole")
    check("B17 前端保存时对派生角色只提交改动过的项（保住 overlay 语义）",
          "this.rEdit.parent_id" in save_src and "rEditOverlay" in save_src,
          save_src[:200].replace("\n", " "))
    return rid


# ================================================================ C. 音色
def t_voice(token: str) -> None:
    """C. 音色：保存链路 + 解析式顺序（角色在前、配置行兜底）。"""
    print("\n── C. 音色：保存链路 + 合成路径取值顺序 ──")
    code, opts = req("GET", "/api/providers/voice-options", token=token)
    opts = opts if isinstance(opts, dict) else {}
    voices = [str(v.get("id") or "") for v in (opts.get("voices") or [])]
    check("C1 voice-options 200 且普通用户可访问", code == 200, f"code={code}")
    check("C2 前端下拉清单来自服务端（不是前端硬编码）",
          "/api/providers/voice-options" in USER_HTML.read_text(encoding="utf-8"))

    if opts.get("allow_custom"):
        voice = "cosyvoice-zh-female"
        check("C3 自建厂商：清单为空但允许手填（不是给一个空下拉卡死人）",
              not voices and bool(opts.get("custom_hint")), str(opts)[:160])
    else:
        voice = voices[0] if voices else ""
        check("C3 认得出的厂商：清单非空（否则用户没得选）", bool(voices),
              f"vendor={opts.get('vendor')!r}")

    code, r = req("POST", "/api/roles", {"name": "音色回归", "tts_voice": voice}, token)
    r = r if isinstance(r, dict) else {}
    rid = int(r.get("id") or 0)
    check("C4 建角色时带音色 -> 原样落库（写路径不吞音色）",
          code == 201 and r.get("tts_voice") == voice,
          f"code={code} got={r.get('tts_voice')!r} 期望={voice!r}")

    _, got = req("GET", f"/api/roles/{rid}", token=token)
    check("C5 重新读详情：音色还是那个（保存链路没丢）",
          (got or {}).get("tts_voice") == voice, str((got or {}).get("tts_voice")))

    _, listing = req("GET", "/api/roles?scope=mine", token=token)
    row = role_in_list((listing or {}).get("mine") if isinstance(listing, dict) else [], rid)
    check("C6 列表接口（页面重新打开）也显示保存的音色",
          row.get("tts_voice") == voice, str(row.get("tts_voice")))

    code, p = req("PATCH", f"/api/roles/{rid}", {"name": "音色回归", "tts_voice": voice},
                  token)
    check("C7 PATCH 音色 -> 200 且原样返回", code == 200 and (p or {}).get("tts_voice") == voice,
          f"code={code} got={(p or {}).get('tts_voice')!r}")

    # 合成路径：解析式的操作数顺序必须是「角色在前、配置行兜底」。
    # 只断言"出现过 _row_voice"不够 —— 旧代码就出现过，但顺序是反的。
    src = ASR_TTS.read_text(encoding="utf-8")
    role_first = "_shape_ok_voice(style, voice) or _shape_ok_voice(style, _row_voice(row))"
    role_first_stream = "_shape_ok_voice(_style, voice) or _shape_ok_voice(_style, _row_voice(row))"
    check("C8 tts() 里角色音色在前、配置行兜底（音色归角色）",
          src.count(role_first) == 1, f"命中 {src.count(role_first)} 次")
    check("C9 tts_stream() 的流式分支用同一条判据（两条路不许分家）",
          src.count(role_first_stream) == 1, f"命中 {src.count(role_first_stream)} 次")
    check("C10 前端没有硬编码音色 ID（清单必须来自服务端）",
          "zh-CN-" not in USER_HTML.read_text(encoding="utf-8"), "")


def main() -> int:
    env = seed_admin_env(dict(os.environ))
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
    env["ELDER_JWT_SECRET"] = "smoke-role-traits-voice-secret"
    env["ELDER_SCHEDULER_INTERVAL"] = "3600"
    env["PYTHONIOENCODING"] = "utf-8"

    print(f"[boot] starting server on :{PORT} (db: {DB_PATH})")
    guard_or_die(PORT, "smoke_role_traits_voice.py")

    log_fp = open(SERVER_LOG, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(SERVER), env=env, stdout=log_fp, stderr=subprocess.STDOUT)

    try:
        if not wait_ready(proc):
            print("[fatal] 服务未就绪，日志尾部：\n" + read_log_tail())
            return 1

        req("POST", "/api/auth/register",
            {"username": "rtv", "password": "test123456", "nickname": "rtv"})
        code, t = req("POST", "/api/auth/login",
                      {"username": "rtv", "password": "test123456"})
        token = t.get("access_token", "") if isinstance(t, dict) else ""
        if not token:
            print(f"[fatal] 普通用户登录失败：code={code} {t}")
            return 1

        t_band_matching()
        t_save_traits(token)
        t_voice(token)
    finally:
        cleanup_db(DB_PATH, procs=(proc,), handles=(log_fp,))
        if any(not ok for _, ok, _ in results):
            print("\n[diag] ---- server log tail ----\n" + read_log_tail())

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
