#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
syntax_check_elder.py —— 对 elder 的 C++ 适配层做**交叉编译语法检查**。

为什么需要这个脚本
------------------
全量 `idf.py build` 要几分钟，而改一行 C++ 就得等几分钟才能知道有没有语法错。
本脚本从 `build/compile_commands.json` 里捞出**真实编译命令**（正确的头文件
路径、SDK 版本、编译选项全在里面），只把结尾的 `-o X.obj -c Y` 换成
`-fsyntax-only Y`，几秒钟出结论。CI 与本地提交前都用它兜底。

用法：
    python tools/syntax_check_elder.py

⚠️ 三个踩过的坑（每个都浪费过一整轮排查，写在这里免得再踩）
-----------------------------------------------------------
1. **Windows 命令行 32K 上限**：直接 subprocess 跑整条编译命令会得到
   `命令行太长`（GBK 报错「命令行太长」/RC=1，**一条编译错误都没有**）。
   假绿陷阱：看着像编译失败，其实是根本没跑起来。必须把参数写进 `@rsp`
   响应文件让编译器自己展开。
2. **rsp 按 ANSI(GBK) 解码，仓库路径含中文**：`G:/workbuddy/语音助手/...`
   里的中文用 UTF-8 写进 rsp，编译器会解成乱码，于是**所有 -I 路径集体失效**，
   报的却是 `fatal error: sdkconfig.h: No such file or directory` —— 一个和
   源码毫无关系的假象。必须 `c2.encode("gbk")`。
   同时 cwd 必须设为 `build/`：编译命令里有 105 条相对 -I，基准就是 build 目录。
3. **BOARD_TYPE 宏要手动补**：它是 CMake `target_compile_definitions` 传进去的，
   复用别的文件的编译命令时不会自动带上。缺了它，`ESP_LOGI(TAG, "…(板卡 %s)",
   BOARD_TYPE)` 会把 `lichuang-dev` 当成减法表达式，报出一堆
   `'lichuang' was not declared in this scope` —— 同样是与真实缺陷无关的噪音。
   elder_bridge.cc 引用它、elder_bind_guide.cc 不引用，正是区分两类错误的探针。
4. **不能整串 `replace("\\", "/")`**：命令里的反斜杠有两种身份 —— 路径分隔符
   （`-IC:/Users/...`，必须换成 `/`）**和引号转义**（`-DBOARD_NAME=\"lichuang-dev\"`，
   换成 `/` 就毁了）。整串替换会把后者变成 `-DBOARD_NAME=/"lichuang-dev/"`，
   宏展开成 `lichuang / dev`，于是报 `'lichuang' was not declared`、
   `#include expects "FILENAME"` —— 全是与源码无关的假红。
   以前的补丁只给 `BOARD_TYPE` 兜了底，`MBEDTLS_CONFIG_FILE` 一直漏网，
   所以 `mcp_server.cc` 永远红。现在按「反斜杠后跟双引号就保留」精确处理。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / "xiaozhi" / "build"
CCJSON = BUILD / "compile_commands.json"

#: 要检查的文件 → 源码路径（第一个必须存在，用来提取编译命令）
TARGETS = {
    "elder_bridge.cc": ROOT / "firmware" / "xiaozhi" / "components" / "elder" / "src" / "elder_bridge.cc",
    "elder_bind_guide.cc": ROOT / "firmware" / "components" / "elder" / "src" / "elder_bind_guide.cc",
    "config_report.c": ROOT / "firmware" / "components" / "elder" / "src" / "config_report.c",
    # 2026-11-01 加：状态灯开关改了 rgb.c（C）与 mcp_server.cc（C++），
    # 两者都是"改错就整块功能没有"的文件，必须在提交前几秒内验证。
    # 注意 rgb.c 用**注入副本**路径：compile_commands.json 里记的就是它
    # （main 编译的是 firmware/xiaozhi/components/elder，不是源目录）。
    "rgb.c": ROOT / "firmware" / "xiaozhi" / "components" / "elder" / "src" / "rgb.c",
    "mcp_server.cc": ROOT / "firmware" / "xiaozhi" / "main" / "mcp_server.cc",
    # 2026-11-01 加 sync.c：灯效下发解析（light_effect_from_str / apply_lights）
    # 全在这个文件里，"加一个 effect 字符串"是最容易漏改映射表的一类改动，
    # 而漏改的表现是**用户换了效果灯没变、两端日志都正常**。
    # 它之前一直不在检查清单里，等于这块改动从来没有过编译级门禁。
    "sync.c": ROOT / "firmware" / "xiaozhi" / "components" / "elder" / "src" / "sync.c",
    # 2026-11-03 加：预录语音片段把 ui.c 的钩子结构体加了一个字段
    # （elder_ui_hooks_t::play_clip，**只能追加在末尾**），并新增 elder_clip.c。
    # 这两处错的代价特别高：
    #   * 字段插到中间 ⇒ 所有初始化器错位，**编译不报错**，表现是"按 A 播 B"；
    #   * elder_clip.c 的 kClipMs 下标与 elder_clip_id_t 顺序对不上 ⇒ 同样静默。
    # 编译级只能挡住"括号/类型/头文件"这类硬错，所以先把它纳入门禁，
    # 顺序一致性另由 tools/verify_elder_clips.py 与人工核对保证。
    # ⚠️ elder_clip.c 是新增文件，build/compile_commands.json 里还没有它，
    #    base_command() 会回退到同后缀模板（rgb.c）—— 这是脚本设计好的行为。
    "elder_clip.c": ROOT / "firmware" / "components" / "elder" / "src" / "elder_clip.c",
    "ui.c": ROOT / "firmware" / "xiaozhi" / "components" / "elder" / "src" / "ui.c",
}

#: 需要确保存在的宏（见坑 3）。
#:
#: ⚠️ 两个必须踩过才知道的细节：
#:   1. 真实编译命令里**已经带了**这个宏（compile_commands.json 记录的是
#:      CMake target_compile_definitions 展开后的完整命令行）。无条件追加会变成
#:      重复定义，编译器报 `warning: "BOARD_TYPE" redefined` 且 **rc=1** ——
#:      又是一个与源码无关的假象。必须先查后加。
#:   2. **不能用字面串比对**：命令行里的形态是 `-DBOARD_TYPE=/"lichuang-dev/"`
#:      （引号被 CMake 转义过），写成 `-DBOARD_TYPE="lichuang-dev"` 去 `in` 判断
#:      永远匹配不上，于是「查后加」退化成「每次都加」，也就是上面那个 bug。
#:      所以只匹配宏**名字**，不碰值。
BOARD_TYPE_NAME = "BOARD_TYPE"
BOARD_TYPE_FALLBACK = '-DBOARD_TYPE=\\"lichuang-dev\\"'


def ensure_def(base: str, name: str, fallback: str) -> str:
    """剔除命令里已有的 BOARD_TYPE token，再补一个干净定义。

    ⚠️ 为什么必须**剔除**而不是「已存在就跳过」：
    compile_commands.json 里记录的那条是 CMake 在 Windows 上生成的命令，
    其形态为 ``-DBOARD_TYPE=/"lichuang-dev/"`` —— 值两侧多了两个斜杠。
    那是命令行 shell 用的转义残留，**在 rsp 响应文件里会被当成除号**，
    于是宏展开成 ``lichuang / dev``，报出一堆 ``'lichuang' was not declared``。
    而 ``/I...`` 那类 include token 也正是靠斜杠才没被误判。

    ⇒ 只要在命令里看见 ``-DBOARD_TYPE`` 就先删掉，再统一追加 fallback。
    """
    base = re.sub(r"-D" + re.escape(name) + r"=\S+\s*", "", base)
    return base + " " + fallback


def _norm_paths(cmd: str) -> str:
    """把**路径分隔符**换成 `/`，但保留引号转义 `\\"`（见文件头坑 4）。

    只放行 `\\` 后跟 `"` 的情况：那是 CMake 给字符串宏值做的转义，
    在 rsp 响应文件里必须原样保留，否则宏值退化成 `lichuang / dev`。
    其余反斜杠都是 Windows 路径分隔符，换成 `/` 才不会在 rsp 里被当转义符。
    """
    return re.sub(r'\\(?!")', "/", cmd)


#: 编译命令模板来源：按**精确文件名**匹配，不能用后缀。
#:
#: ⚠️ 踩过的坑：用 `endswith(".cc")` 会匹配到上游 main 组件的 .cc
#:（audio_service.cc 等），那条命令的 `-I` 只含 main 组件的头文件路径，
#: 于是报 `nvs_flash.h / config_report.h: No such file or directory` ——
#: 三个文件全红，但全是工具用错命令造成的假红，不是源码问题。
#: 每个组件的 `-I` 集合不同，模板必须来自**同一组件**。
TEMPLATE_FILE = {".cc": "elder_bridge.cc", ".c": "rgb.c"}


def base_command(name: str, suffix: str) -> str:
    """取一份真实编译命令。

    **优先用该文件自己那条**，找不到才回退到同后缀的模板。

    为什么优先自己的（2026-11-01 加）：每个组件的 ``-I`` 集合不同 ——
    ``mcp_server.cc`` 属于 main 组件，它需要 mbedtls 的
    ``MBEDTLS_CONFIG_FILE`` 与 main 的头文件；而 ``elder_bridge.cc`` 属于
    elder 组件，两者不通用。拿 elder 的命令去检查 mcp_server.cc，报出来的是
    ``mbedtls/build_info.h: #include expects "FILENAME"`` 和
    ``'lichuang' was not declared`` —— **与源码毫无关系的假红**。
    假红比不检查更坏：它教人忽略红灯。

    回退仍然保留：``compile_commands.json`` 是 ``idf.py build`` 的产物，
    新加的文件在 build 之前不可能出现在里面。
    """
    data = json.loads(CCJSON.read_text(encoding="utf-8"))
    for e in data:
        f = e["file"].replace("\\", "/")
        if f.endswith("/" + name):
            return _norm_paths(e.get("command") or " ".join(e["arguments"]))
    want = TEMPLATE_FILE[suffix]
    for e in data:
        f = e["file"].replace("\\", "/")
        if f.endswith("/" + want):
            return _norm_paths(e.get("command") or " ".join(e["arguments"]))
    raise SystemExit(f"[fatal] compile_commands.json 里找不到 {name} 或 {want}，"
                     f"先跑一次 idf.py build")


def check(name: str, src: Path, base: str) -> bool:
    real = src.as_posix()
    # 坑 3：先查后加 BOARD_TYPE，避免 redefine 让 rc 假红
    cmd = ensure_def(base, BOARD_TYPE_NAME, BOARD_TYPE_FALLBACK)
    cmd = re.sub(r"-o\s+\S+\.obj\s+-c\s+\S+", "-fsyntax-only " + real, cmd, count=1)
    rsp = BUILD / f"{name}.rsp"
    rsp.write_bytes(cmd.encode("gbk"))          # 坑 2：必须 GBK
    try:
        exe = cmd.split()[0]
        p = subprocess.run([exe, f"@{rsp.name}"], capture_output=True,
                           cwd=str(BUILD))        # 坑 2：cwd 必须是 build/
    finally:
        rsp.unlink(missing_ok=True)

    out = (p.stdout + p.stderr).decode("gbk", errors="replace")
    # ⚠️ 必须先剥 ANSI 色码再数错误行：GCC 的 `-fdiagnostics-color` 会把
    #    `error`/`warning` 包在 `\x1b[01;35m ... \x1b[m` 里，不剥的话
    #    `" error" in line` 永远匹配不上 ⇒ 明明报了错却显示 errors=0，
    #    只能靠 rc 兜底，诊断信息全丢。
    out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out)
    errs = [l for l in out.splitlines() if re.search(r"\berror\b", l)]
    warns = [l for l in out.splitlines() if re.search(r"\bwarning\b", l)]
    ok = p.returncode == 0 and not errs
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  rc={p.returncode} "
          f"errors={len(errs)} warnings={len(warns)}")
    for l in errs[:12]:
        print(f"         -> {l[:200]}")
    # 兜底：rc!=0 却零 error 行 = 编译器没真正跑起来（命令行/编码类问题），
    # 这种「假绿」必须当场暴露，不能当成编译错误混过去。
    if p.returncode != 0 and not errs:
        print(f"         -> ⚠️ rc!=0 但无 error 行：疑似 rsp/编码/命令行问题，"
              f"非源码错误。原始输出前 200 字：{out[:200]!r}")
    return ok


def main() -> int:
    if not CCJSON.is_file():
        print(f"[fatal] 缺 {CCJSON}；请先在 firmware/xiaozhi 跑一次 idf.py build")
        return 1
    print("=" * 64)
    print("elder 语法检查（交叉编译，秒级）")
    print("=" * 64)
    results = []
    for suffix in (".cc", ".c"):
        targets = [(n, s) for n, s in TARGETS.items() if s.is_file() and n.endswith(suffix)]
        for n, s in targets:
            # 每个文件各取各的编译命令（见 base_command 的说明）
            results.append((n, check(n, s, base_command(n, suffix))))
    failed = [n for n, ok in results if not ok]
    print("\n" + "=" * 64)
    print(f"共 {len(results)} 个文件：通过 {len(results) - len(failed)}，失败 {len(failed)}")
    for n in failed:
        print(f"  ✗ {n}")
    print("=" * 64)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())