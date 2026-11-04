"""构建后校验：这份 esp-sr 预编译库里**到底有没有**降噪实现、AGC 是不是真有实现。

为什么要单独一个脚本、而且必须跑在 `idf.py build` **之后**：

1. 2026-11-04 两次「唤醒即重启」都是同一个前提没被验证过造成的：
   我以为 `AFE_NS_MODE_WEBRTC` 是"算法降噪"，其实这份预编译库里
   **没有任何算法降噪实现**。而 `managed_components/` 是 .gitignore 的，
   静态门禁（`smoke_afe_agc_ns.py`，跑在 build 之前）拿不到 .a，
   只能靠注释说明 —— 注释不是判据。

2. 所以判据落在这里：直接读 `libnsnet.a` / `libesp_audio_front_end.a` 的
   **符号表**。事实是：
     * `libnsnet.a` 只有 `esp_nsnet1_quantized` / `esp_nsnet2_quantized`
       两个**模型**，`esp_nsn_models` 表里没有 "WEBRTC"；
     * `esp_ns.h` 里声明的 `ns_create` / `ns_pro_create` / `ns_process`
       在任何一个 .a 里**都没有符号** ⇒ 真调用只会链接失败；
     * AGC 反过来是真有实现：`esp_agc` / `enable_agc` / `disable_agc`，
       管线串 `|AGC(WebRTC)|` 也在库里。

   ⇒ 结论：本机降噪只能关（`ns_init=false`），能落地的是 AGC。
   这个脚本就是让**产物自己证明**这句话，而不是让注释替它作证。

3. 哪天 esp-sr 升级后真的加了算法降噪，这个脚本会**红**，那正是提醒我们
   "可以重新评估降噪了"的信号 —— 而不是让人靠记忆去猜。

用法（CI 里在 `idf.py build` 之后）：
    python tools/smoke_esp_sr_ns_libs.py
本地没 build 过时会明确报"库不存在"，不会静默 SKIP（MEMORY 第 1/4 条）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIBDIR = (ROOT / "firmware" / "xiaozhi" / "managed_components"
          / "espressif__esp-sr" / "lib" / "esp32s3")

NS_LIB = LIBDIR / "libnsnet.a"
AFE_LIB = LIBDIR / "libesp_audio_front_end.a"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"   {detail}" if not ok and detail else ""))


def symbols(path: Path) -> set[str]:
    """粗略提取 .a 里出现的标识符集合。

    不解析 ar/ELF 结构：.a 的符号名是明文，正则取标识符足够回答
    "某个函数名在不在这个库里"。要的是"存在性"，不是地址。
    """
    data = path.read_bytes()
    return {m.group().decode("ascii") for m in re.finditer(rb"[A-Za-z_][A-Za-z0-9_]{2,63}", data)}


def has_literal(path: Path, needle: bytes) -> bool:
    """管线串里有 `|` `(` 这类非标识符字符，标识符集合查不到，得按原字节找。"""
    return needle in path.read_bytes()


def main() -> int:
    print("=" * 64)
    print("esp-sr 预编译库能力校验（构建后）")
    print("=" * 64)
    print(f"库目录：{LIBDIR}")

    if not NS_LIB.exists() or not AFE_LIB.exists():
        print("\n  [FAIL] 找不到预编译库 —— 本脚本必须跑在 `idf.py build` 之后")
        print(f"         期望存在：{NS_LIB.name} / {AFE_LIB.name}")
        print("结论：**红**")
        return 1

    ns = symbols(NS_LIB)
    afe = symbols(AFE_LIB)
    print(f"  {NS_LIB.name}: {len(ns)} 个标识符   {AFE_LIB.name}: {len(afe)} 个标识符")

    print("\n── 1. 降噪：库里只有 NSNet2 模型，没有算法降噪 ──")
    check("esp_nsnet2_quantized" in ns or "esp_nsnet1_quantized" in ns,
          "libnsnet.a 里是 NSNet 模型（esp_nsnet{1,2}_quantized）",
          "连模型都没有 ⇒ 前提变了，重新评估整条降噪结论")
    check("esp_nsn_models" in ns,
          "libnsnet.a 里有模型表 esp_nsn_models（按名字查表）",
          "没有表说明实现方式变了")
    # 算法降噪的接口（esp_ns.h 声明了，但预编译库里没有实现）
    for fn in ("ns_create", "ns_pro_create", "ns_process", "ns_destroy"):
        check(fn not in ns and fn not in afe,
              f"没有任何库定义 {fn}()（算法降噪不可用）",
              f"居然有 {fn} 了 ⇒ esp-sr 加了算法降噪，可以重新评估 ns_init")
    # WebRTC 降噪的内部实现符号（存在就说明算法降噪真的编进来了）
    for sym in ("WebRtcNs_Create", "WebRtcNs_Process", "WebRtcNsx_Create", "WebRtcNsx_Process"):
        check(sym not in ns and sym not in afe,
              f"没有 {sym}（没有 WebRTC 降噪实现）",
              "有它 ⇒ 算法降噪可用，ns_init 可以重新评估")

    print("\n── 2. AGC：库里真有实现，所以它是本机能落地的修法 ──")
    check("esp_agc" in afe, "libesp_audio_front_end.a 里有 esp_agc",
          "没有 ⇒ AGC 也只是个空配置，白高兴一场")
    check("enable_agc" in afe and "disable_agc" in afe,
          "有 enable_agc / disable_agc（运行时可开关）",
          "没有 ⇒ 配置项可能被忽略")
    check(has_literal(AFE_LIB, b"|AGC(WebRTC)|"),
          "库里有管线串 |AGC(WebRTC)|（会出现在第二条 AFE 管线里）",
          "没有这个串 ⇒ 开机日志里永远看不到 AGC，说明实现不是这条")

    passed = sum(1 for ok, _, _ in results if ok)
    failed = len(results) - passed
    print("=" * 64)
    print(f"通过 {passed} 项，失败 {failed} 项")
    if failed:
        print("结论：**红**")
        return 1
    print("结论：**绿**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
