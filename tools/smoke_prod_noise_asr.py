#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描器 404 日志降噪 + ASR 空结果留证（2026-11-03 深夜用户实报）。

两条用户原话
------------
1. 「生产容器出现大量 404 错误日志。」
2. 「唤醒之后我再说话设备完全没识别。并且设备一直在聆听状态，过了1分钟左右
   设备说"没听清"。」

1 的现场：45 分钟 209 条 404，其中 **207 条来自同一个 IP**，路径全是
``/.git/config``、``/.aws/credentials``、``/wp-config.php`` 这类凭据探测。
全部正确 404，**不是本站缺陷**，但把真正的 404 淹掉了。

2 的现场：三次 ``服务端静音兜底触发（累计语音 4680ms / 600ms / 1020ms）``
紧接 ``识别结果为空``，而 ``model_usage`` 里那三次 ASR 全是 ``ok=True``、
272~782ms —— ASR 服务本身是好的（同容器 TTS→ASR 往返能逐字识别回来），
问题在**送进去的音频**。可日志里只有一个"4680ms"，分不清是太小、全是噪声、
还是被切碎，三种原因的修法完全不同 ⇒ 补一行音频指纹 + 按需落 wav。

判据口径
--------
· 404 降噪用**真的** ``ScannerNoiseFilter`` 喂真 ``LogRecord``：判据是
  "路径前缀"而不是关键词黑名单（黑名单永远漏，扫描器天天换路径）。
  必须同时钉住"自己的 404 不许被吞" —— 否则以后前端打错路径就彻底看不见。
· 音频指纹用**真的** ``_pcm_stats`` / ``_dump_empty_asr``，喂合成 PCM：
  全零 → rms 0；满幅方波 → rms≈1；落盘要真产生文件，且超过 KEEP 个要淘汰。
· 落盘目录用 monkeypatch 指到临时目录，**绝不往生产库目录写测试文件**。
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from app import config  # noqa: E402
from app import main as app_main  # noqa: E402
from app.voice import session as S  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


# ================================================================ 1. 404 降噪
def t_scanner_filter() -> None:
    print("\n── 1. 扫描器 404 不许进访问日志，但自己的 404 必须留着 ──")

    flt = app_main.ScannerNoiseFilter()

    def rec(path: str, status: int) -> logging.LogRecord:
        # uvicorn.access 的参数形状：
        # (client_addr, method, full_path, http_version, status_code)
        return logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname=__file__,
            lineno=1, msg='%s - "%s %s HTTP/%s" %d',
            args=("45.138.12.179:0", "GET", path, "1.1", status),
            exc_info=None)

    # 现场那批：全部应被摘掉
    scanner_paths = [
        "/.git/config", "/.aws/credentials", "/wp-config.php",
        "/application_default_credentials.json", "/xml", "/soap",
        "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php",
        "/actuator/env", "/.env",
    ]
    for p in scanner_paths:
        check(flt.filter(rec(p, 404)) is False,
              f"扫描器探测 {p} 的 404 被摘掉",
              "留着就会把真正的 404 淹掉")

    # ⚠️ 反例：``/api/v1/auto_login`` 也常被扫，但它落在**我们自己的**前缀下，
    # 必须保留 —— 判据只能是"路径前缀"，不能因为"看起来像扫描器"就吞。
    # 这条同时钉住一个取舍：宁可在自己的命名空间里多留几条，也不能漏掉
    # "前端打错版本"这种真事故。
    check(flt.filter(rec("/api/v1/auto_login", 404)) is True,
          "/api/ 前缀下的 404 一律保留（哪怕看起来像扫描器）",
          "按关键词黑名单吞掉 ⇒ 自己打错路径时日志一片空白")

    # 自己的 404：必须留（这是排查"前端打错路径/设备用错版本"的唯一线索）
    own_paths = ["/api/voice/nope", "/static/missing.js", "/admin/x",
                 "/favicon.ico", "/index.html", "/"]
    for p in own_paths:
        check(flt.filter(rec(p, 404)) is True,
              f"本站路径 {p} 的 404 必须保留",
              "被吞掉 ⇒ 自己打错路径时日志一片空白")

    # 非 404 一律不动（200/401/500 都要留）
    for status in (200, 401, 403, 405, 500):
        check(flt.filter(rec("/.git/config", status)) is True,
              f"状态 {status} 不属于降噪范围",
              "只该摘 404；401/500 是真信号")

    # 参数形状不认识时不许崩、也不许吞
    weird = logging.LogRecord(name="uvicorn.access", level=logging.INFO,
                              pathname=__file__, lineno=1, msg="x",
                              args=None, exc_info=None)
    check(flt.filter(weird) is True, "参数形状不认识时放行（过滤器不能把日志打崩）")

    # 汇总行：每 50 条打一行，证明"有人一直在扫"这件事没被抹掉
    flt2 = app_main.ScannerNoiseFilter()
    for _ in range(49):
        flt2.filter(rec("/.env", 404))
    check(flt2.dropped == 49, "累计计数按条递增", f"dropped={flt2.dropped}")
    flt2.filter(rec("/.env", 404))
    check(flt2.dropped == 50 and flt2.dropped % 50 == 0,
          "第 50 条会打一行汇总（噪声消失但证据保留）")

    # 真的挂到了 uvicorn.access 上（只写类不注册 = 完全没生效，且不报错）
    installed = [f for f in logging.getLogger("uvicorn.access").filters
                 if isinstance(f, app_main.ScannerNoiseFilter)]
    check(len(installed) == 1, "过滤器已挂到 uvicorn.access（源码接线）",
          "只定义不注册 ⇒ 一条也不会被摘掉，且不报错")


# ================================================================ 2. 音频指纹
def t_pcm_stats() -> None:
    print("\n── 2. 音频指纹：分得清「太小」「满幅」「全零」──")

    silent = b"\x00\x00" * 16000            # 1 秒全零
    rms, peak = S._pcm_stats(silent)
    check(rms == 0.0 and peak == 0.0, "全零音频 → rms=0 peak=0",
          f"实际 rms={rms} peak={peak}")

    loud = b"\xff\x7f" * 16000              # 1 秒满幅方波（+32767）
    rms, peak = S._pcm_stats(loud)
    check(rms > 0.9 and peak > 0.99, "满幅音频 → rms/peak 接近 1.0",
          f"实际 rms={rms:.3f} peak={peak:.3f}")

    quiet = b"\x10\x00" * 16000             # 1 秒 16/32768 ≈ 0.0005
    rms_q, _ = S._pcm_stats(quiet)
    check(0 < rms_q < 0.01, "极轻音频 → rms 落在 0.01 以下（能看出'太小'）",
          f"实际 {rms_q:.5f}")

    check(S._pcm_stats(b"") == (0.0, 0.0), "空字节串不抛异常")
    check(S._pcm_stats(b"\x01") == (0.0, 0.0), "半个样本（奇数长度）不抛异常")


# ================================================================ 3. 留证落盘
def t_dump() -> None:
    print("\n── 3. ASR 空结果留证：只在'确实说了话'时落，且只留最近 N 个 ──")

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        orig_dir = S._asr_dump_dir
        orig_dump = config.ASR_DUMP
        orig_keep = config.ASR_DUMP_KEEP
        try:
            S._asr_dump_dir = lambda: folder      # type: ignore[assignment]
            config.ASR_DUMP = True
            config.ASR_DUMP_KEEP = 2

            pcm = b"\x00\x10" * 16000        # 1 秒

            # 语音量不够 ⇒ 不落盘（环境噪声碰响 VAD，不值得存）
            summary = S._dump_empty_asr(pcm, 300, "s" * 32)
            check(list(folder.glob("asr_empty_*.wav")) == [],
                  "语音量不足阈值时不落盘", "否则磁盘会被噪声塞满")
            check("rms=" in summary and "时长=" in summary,
                  "摘要里带 rms/时长（不落盘也要有指纹）", f"实际 {summary!r}")

            # 够阈值 ⇒ 落盘
            summary = S._dump_empty_asr(pcm, 2000, "abcdef1234567890")
            files = list(folder.glob("asr_empty_*.wav"))
            check(len(files) == 1, "语音量够就落一个 wav", f"实际 {len(files)} 个")
            check("已存" in summary, "摘要里给出文件名（能直接 docker cp 出来）",
                  f"实际 {summary!r}")

            if files:
                blob = files[0].read_bytes()
                check(blob[:4] == b"RIFF" and blob[8:12] == b"WAVE",
                      "落盘的是标准 wav（RIFF/WAVE 头）",
                      "直接丢裸 PCM 出来没法用播放器听")
                check(len(blob) == 44 + len(pcm),
                      "wav 头 44 字节 + 原始 PCM，长度对得上",
                      f"实际 {len(blob)} vs {44 + len(pcm)}")

            # 超过 KEEP 个 ⇒ 淘汰最旧的
            for i in range(4):
                S._dump_empty_asr(pcm, 2000, f"keep{i}abcdefghij")
            files = sorted(folder.glob("asr_empty_*.wav"))
            check(len(files) <= 2, "只保留 ASR_DUMP_KEEP 个（按时间淘汰）",
                  f"实际留下 {len(files)} 个")

            # 关掉开关 ⇒ 一个都不写（现场可以一键停）
            for f in folder.glob("asr_empty_*.wav"):
                f.unlink()
            config.ASR_DUMP = False
            summary = S._dump_empty_asr(pcm, 2000, "off" + "0" * 29)
            check(list(folder.glob("asr_empty_*.wav")) == [],
                  "ELDER_ASR_DUMP=0 时完全不写盘", "现场要有开关")
            check("rms=" in summary, "关掉落盘也照样打指纹")

            # ---- 3b. 窗口超时（VAD 判"一句没说"，老人却说他说了话）----
            # 这一种是 2026-11-03 深夜现场里最要命的：60 秒窗口累计语音 0ms，
            # 走的是"重新武装而不送 ASR"，_dump_empty_asr 的闸门是
            # "语音 ≥ 800ms"，在这里恒为 0 ⇒ **永远抓不到**。所以它单独有
            # 一套判据（看峰值），必须单独钉住。
            print("\n  -- 窗口超时留证：闸门是峰值，不是语音量 --")
            config.ASR_DUMP = True
            for f in folder.glob("asr_*.wav"):
                f.unlink()

            # 有声（peak 0.125）但语音量 0ms ⇒ 必须落，且 tag 是 timeout
            summary = S._dump_listen_timeout(pcm, 0, "tout1234abcd", 60000)
            files = list(folder.glob("asr_timeout_*.wav"))
            check(len(files) == 1,
                  "语音量 0ms 但有能量 ⇒ 照样落盘（不看 800ms 闸门）",
                  f"实际 {len(files)} 个；摘要 {summary!r}")
            check("窗口=60000ms" in summary, "摘要里带窗口长度",
                  f"实际 {summary!r}")
            check("已存" in summary, "摘要里给出文件名", f"实际 {summary!r}")
            if files:
                check(len(files[0].read_bytes()) == 44 + len(pcm),
                      "超时留证的 wav 长度也对（44 + PCM）")

            # 房间真安静（peak 0.0078 < 0.01）⇒ 不落，只打指纹。
            # 否则空房间里每分钟写一个 1.9MB 的文件。
            quiet = b"\x00\x01" * 16000
            _r, quiet_peak = S._pcm_stats(quiet)
            summary = S._dump_listen_timeout(quiet, 0, "quiet123abcd", 60000)
            check(quiet_peak < S._DUMP_TIMEOUT_PEAK,
                  "静音样本峰值确实低于闸门（测试前提成立）",
                  f"peak={quiet_peak}")
            check(len(list(folder.glob("asr_*.wav"))) == 1,
                  "峰值低于闸门时不落盘（空房间不能每分钟写 1.9MB）",
                  f"实际 {len(list(folder.glob('asr_*.wav')))} 个")
            check("峰值过低" in summary, "摘要里说明为什么没落盘",
                  f"实际 {summary!r}")

            # 两种 tag 共用同一个配额：asr_*.wav 一起轮换
            for i in range(3):
                S._dump_listen_timeout(pcm, 0, f"mix{i}abcd", 60000)
            total = sorted(folder.glob("asr_*.wav"))
            check(len(total) <= 2,
                  "empty/timeout 两种 tag 共用 ASR_DUMP_KEEP 配额",
                  f"实际留下 {len(total)} 个（配额 2）")

            # 关掉开关 ⇒ 超时分支也不写
            for f in folder.glob("asr_*.wav"):
                f.unlink()
            config.ASR_DUMP = False
            S._dump_listen_timeout(pcm, 0, "offtoutabcd", 60000)
            check(list(folder.glob("asr_*.wav")) == [],
                  "ELDER_ASR_DUMP=0 时超时分支也不写盘")
        finally:
            S._asr_dump_dir = orig_dir       # type: ignore[assignment]
            config.ASR_DUMP = orig_dump
            config.ASR_DUMP_KEEP = orig_keep


# ================================================================ 4. 接线
def t_wiring() -> None:
    print("\n── 4. 源码接线：空结果分支必须真的调用留证 ──")
    src = (ROOT / "server" / "app" / "voice" / "session.py").read_text(
        encoding="utf-8")
    check("_dump_empty_asr(pcm_all, self._speech_ms, self.session_id)" in src,
          "空识别分支调用 _dump_empty_asr（源码接线）",
          "只写函数不调用 = 永远拿不到证据，且不报错")
    check("ASR 返回空，音频指纹" in src, "日志里带可 grep 的固定标记")

    # 看门狗那条"60 秒一句没说"的分支：**必须先取指纹再重新武装**，
    # _rearm_listening() 会清 _pcm，顺序反了就永远落出 0 字节的 wav。
    # 这一条只能按"代码位"判顺序，不能整文件子串匹配（MEMORY 第 3 条）。
    head, sep, tail = src.partition(
        "self._listen_timeout_fingerprint(elapsed_ms)")
    check(bool(sep), "看门狗超时分支调用 _listen_timeout_fingerprint（源码接线）",
          "只给 _dump_empty_asr 装埋点 ⇒ '一句没说'那种现场永远抓不到")
    if sep:
        after = tail.split("_rearm_listening()", 1)
        check(len(after) == 2 and after[0].strip(),
              "调用在 _rearm_listening() 之前（后者会清 _pcm）",
              "顺序反了 ⇒ 落出 0 字节 wav，比不落更误导")
        check(after[0].count("\n") < 8,
              "指纹调用紧挨着重新武装（不在别的分支里）")

    # 包装层必须自己兜异常：它跑在看门狗循环里，抛出去会被外层
    # except 吞掉 ⇒ 看门狗退出 ⇒ 设备永远卡在聆听（比没证据严重得多）。
    body_head, body_sep, body_tail = src.partition(
        "def _listen_timeout_fingerprint(")
    check(bool(body_sep), "_listen_timeout_fingerprint 方法存在")
    if body_sep:
        body = body_tail.split("async def _silence_watchdog", 1)[0]
        check("try:" in body and "except Exception" in body,
              "包装层自己 try/except（不许把异常抛给看门狗）")
        check("_dump_listen_timeout(list(self._pcm)" in body,
              "包装层真的去取 _pcm 并调用底层留证")

    cfg = (ROOT / "server" / "app" / "config.py").read_text(encoding="utf-8")
    check('"ELDER_ASR_DUMP", "1"' in cfg,
          "ASR_DUMP 默认开启（查清后可设 0 关）",
          "默认关 ⇒ 下次再出问题又只能猜")
    check("ASR_DUMP_KEEP" in cfg and "ASR_DUMP_MIN_SPEECH_MS" in cfg,
          "保留个数与最小语音量都可配")

    wf = (ROOT / ".github" / "workflows" / "server-ci.yml").read_text(
        encoding="utf-8")
    check("tools/smoke_prod_noise_asr.py" in wf,
          "server-ci.yml 里跑了本脚本",
          "写了不跑 = 没门禁，而且不报错（MEMORY 第 1 条）")


def main() -> int:
    t_scanner_filter()
    t_pcm_stats()
    t_dump()
    t_wiring()

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
