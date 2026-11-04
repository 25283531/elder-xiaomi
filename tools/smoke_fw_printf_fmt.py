"""固件静态门禁：`ESP_LOGx` 的格式串里不许出现 `%ll*`。

## 为什么需要它（2026-11-04 真机第三次崩溃）

本工程开了 `CONFIG_NEWLIB_NANO_FORMAT=y`（`sdkconfig.defaults:40`），
**nano-printf 不支持 `%lld` / `%llu` / `%llx`**。传了 `%lld` 之后
va_arg 的步进就和实参对不上，后面任何一个 `%s`/`%d` 都会读到错位的值 ——
把整数当指针去解引用，就是 `LoadProhibited`（EXCVADDR=0）。

真实现场（09:22:46，用户复现「按键不能确认」时）：

```
Guru Meditation Error: Core 1 panic'ed (LoadProhibited)
Backtrace: 0x4218e4aa  _printf_i
           0x42195c02  _vfprintf_r
           0x4218f9a5  vprintf
           0x4038c066  esp_log
           0x4201aada  Application::FlushSubtitles(bool) +0xda
           0x4201a939  Application::MainEventLoop +0x305
```

崩在「一句 TTS 字幕上屏」的瞬间，跟音频、降噪、按键都无关。

## 这个坑项目里其实早就踩过

`ota.cc:251` 有一整段注释：「⚠️ `%ld` 而不是 `%lld`：本工程开了
CONFIG_LIBC_NEWLIB_NANO_FORMAT=y，nano-printf 不支持 %lld，会原样打出
"(ld)" 字面量。」—— 但字幕代码是后来加的，又写回 `%lld`。
**写在注释里的教训不是判据**，所以这条规则必须由脚本执行。

## 判据

只扫**字符串字面量**（先剥注释），出现 `%ll` 就红。
`%ld` / `%d` / `%u` 都合法；确实需要 64 位时用 `(long)` + `%ld`
（秒级时间戳到 2038 年前都在 32 位范围内，够用）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW_MAIN = ROOT / "firmware" / "xiaozhi" / "main"
SDK = ROOT / "firmware" / "xiaozhi" / "sdkconfig.defaults"

# 本工程实际用到的扩展名。.h 里也可能有内联 log，一并扫。
EXTS = (".c", ".cc", ".cpp", ".h", ".hpp")

# 只看字符串字面量里有没有 %ll —— 先把注释剥掉，避免"解释这个坑"的注释
# 自己把门禁踩红（本仓踩过好几次）。
_COMMENT_RE = re.compile(
    r'//[^\n]*'            # 行注释
    r'|/\*.*?\*/',         # 块注释
    re.S)
# 字符串字面量（含转义），单双引号都收：printf 的格式串都是双引号，
# 但把单引号也算进来无害，且能覆盖 'x' 这类字符常量不误报。
_STR_RE = re.compile(r'"(?:\\.|[^"\\])*"', re.S)
_BAD_RE = re.compile(r'%ll[duxXeEfgGsScp]')

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"   {detail}" if not ok and detail else ""))


def strip_comments(src: str) -> str:
    """把注释换成等长空白，保留行号。"""
    def _blank(m: re.Match[str]) -> str:
        return re.sub(r'[^\n]', ' ', m.group())
    return _COMMENT_RE.sub(_blank, src)


def scan(path: Path) -> list[tuple[int, str]]:
    """返回 [(行号, 命中的格式串片段)]。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    code = strip_comments(raw)
    hits: list[tuple[int, str]] = []
    for m in _STR_RE.finditer(code):
        lit = m.group()
        if _BAD_RE.search(lit):
            line = code.count("\n", 0, m.start()) + 1
            hits.append((line, lit[:90]))
    return hits


def main() -> int:
    print("=" * 64)
    print("固件 printf 格式串门禁（nano-printf 不支持 %ll*）")
    print("=" * 64)

    sdk = SDK.read_text(encoding="utf-8")
    nano = "CONFIG_NEWLIB_NANO_FORMAT=y" in sdk
    check(nano,
          "sdkconfig.defaults 里 NEWLIB_NANO_FORMAT=y（本门禁成立的前提）",
          "前提不成立了 ⇒ 这条规则可以放宽，但**先确认**再改，别默默删门禁")
    if not nano:
        print("  前提不成立，跳过扫描（不是绿，是规则不适用）")
        print("结论：**红**（前提变了，需要人工确认）")
        return 1

    files = [p for p in FW_MAIN.rglob("*") if p.suffix in EXTS]
    print(f"  扫描 {len(files)} 个源文件（{FW_MAIN}）")
    bad_total = 0
    for p in sorted(files):
        hits = scan(p)
        for line, lit in hits:
            bad_total += 1
            rel = p.relative_to(ROOT).as_posix()
            print(f"  [FAIL] {rel}:{line}  {lit}")
    check(bad_total == 0,
          f"没有任何格式串用 %ll*（发现 {bad_total} 处）",
          "nano-printf 不支持 %ll*，va_arg 会错位 ⇒ 把整数当指针 ⇒ "
          "LoadProhibited（2026-11-04 崩在 FlushSubtitles）；改用 (long)+%ld 或 (int)+%d")

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
