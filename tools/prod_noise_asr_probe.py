"""生产容器探针：核对「扫描器 404 降噪」与「ASR 空结果留证」是否真的生效。

用法（脚本从 stdin 进容器，容器内没有源码只有镜像）：

    plink ... "docker exec -i elder-server python -" < tools/prod_noise_asr_probe.py

为什么要探针而不是看 CI 绿：这两处都是**运行期接线**（日志过滤器挂没挂上、
落盘目录在容器里是不是可写），CI 里绿只证明代码逻辑对，不证明生产容器
里的那个镜像带着它、且 `_asr_dump_dir()` 解析出来的 `/data` 真能写。

⚠️ 只读 + 写一个临时文件立刻删掉，不碰任何业务数据。
"""
from __future__ import annotations

import logging
import sys

sys.path.insert(0, "/app")

import app.config as config  # noqa: E402
import app.main as main_mod  # noqa: E402
from app.voice import session as S  # noqa: E402

fails: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {msg}")
    if not ok:
        fails.append(msg)


def main() -> int:
    print("== 1. 扫描器 404 降噪 ==")
    filters = logging.getLogger("uvicorn.access").filters
    names = [getattr(f, "name", type(f).__name__) for f in filters]
    print(f"  uvicorn.access 上挂了 {len(filters)} 个过滤器：{names}")
    check(any(isinstance(f, main_mod.ScannerNoiseFilter) for f in filters),
          "ScannerNoiseFilter 已注册在 uvicorn.access 上")

    flt = next((f for f in filters
                if isinstance(f, main_mod.ScannerNoiseFilter)), None)
    if flt is not None:
        def feed(path: str, status: int) -> bool:
            rec = logging.LogRecord(
                "uvicorn.access", logging.INFO, __file__, 0,
                '%s - "%s %s HTTP/%s" %d',
                ("1.2.3.4:0", "GET", path, "1.1", status), None)
            return flt.filter(rec)

        # 现场真实出现过的探测路径
        for p in ("/.git/config", "/wp-config.php", "/.aws/credentials",
                  "/xml", "/soap"):
            check(feed(p, 404) is False, f"丢弃扫描器 404：{p}")
        # 自家的路径一个都不许吞 —— 这是这条过滤器唯一的风险
        for p in ("/", "/index.html", "/api/v1/auto_login",
                  "/static/user.html", "/admin"):
            check(feed(p, 404) is True, f"保留自家 404：{p}")
        check(feed("/.git/config", 200) is True, "非 404 一律不动")

    print("== 2. 诊断开关 ==")
    print(f"  ASR_DUMP={config.ASR_DUMP} "
          f"MIN_SPEECH_MS={config.ASR_DUMP_MIN_SPEECH_MS} "
          f"KEEP={config.ASR_DUMP_KEEP}")
    check(config.ASR_DUMP is True, "ELDER_ASR_DUMP 默认开")
    check(config.ASR_DUMP_MIN_SPEECH_MS == 800, "最小语音量 800ms")
    check(config.ASR_DUMP_KEEP >= 1, "保留份数 ≥ 1")

    print("== 3. 音频指纹（真函数、合成波形）==")
    import array
    import math

    # 0.3 幅度的 1 秒 440Hz 正弦：rms 应约 0.3/√2≈0.212，峰值约 0.3
    amp = 0.3
    pcm = array.array("h", (
        int(amp * 32767 * math.sin(2 * math.pi * 440 * i / 16000))
        for i in range(16000))).tobytes()
    rms, peak = S._pcm_stats(pcm)
    print(f"  正弦(幅度 0.3)：rms={rms:.4f} peak={peak:.3f}")
    check(abs(rms - 0.212) < 0.02, "rms 落在 0.212 附近（不是 0、不是 1）")
    check(abs(peak - 0.300) < 0.01, "峰值落在 0.300 附近")
    check(S._pcm_stats(b"\x00" * 3200) == (0.0, 0.0), "全零 → (0.0, 0.0)")
    check(S._pcm_stats(b"") == (0.0, 0.0), "空串不炸")

    print("== 4. 落盘目录 ==")
    folder = S._asr_dump_dir()
    print(f"  _asr_dump_dir() = {folder}")
    check(folder is not None, "能解析出落盘目录（非 sqlite 或目录不存在会返回 None）")
    if folder is not None:
        probe = folder / ".probe_noise_asr"
        try:
            probe.write_bytes(b"x")
            writable = probe.read_bytes() == b"x"
            probe.unlink()
        except OSError as exc:
            writable = False
            print(f"  写入失败：{type(exc).__name__}: {exc}")
        check(writable, f"{folder} 可写（否则真出事时落不下证据）")
        existing = sorted(folder.glob("asr_empty_*.wav"))
        print(f"  现有 asr_empty_*.wav：{len(existing)} 个"
              + (f"，最新 {existing[-1].name}" if existing else ""))

    print("== 5. 空结果分支真的调用了留证 ==")
    import inspect

    src = inspect.getsource(S.VoiceSession._run_turn)
    check("_dump_empty_asr(" in src, "_run_turn 里调用了 _dump_empty_asr")
    check("音频指纹" in src, "日志带『音频指纹』标记（便于 grep 现场）")

    print()
    if fails:
        print(f"结论：**红** —— {len(fails)} 项失败")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("结论：**绿**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
