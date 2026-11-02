"""逐字节校验固件内置 OGG 片段的 OpusHead / 帧长（生成之后必跑）。

为什么需要单独一个校验脚本
--------------------------
``AudioService::PlaySound()`` 从 **OpusHead** 里读采样率，再按**硬编码 60ms**
把每个 Opus 包当成一帧推给解码器。这两件事只要有一件不对，症状都不是报错：

* 采样率 ≠ 24kHz ⇒ 每次播放都走 ``SetDecodeSampleRate()`` **重建解码器**
  （``audio_service.cc`` 398-411），带来重建开销和爆音。``popup.ogg`` 就是
  16kHz，所以它是这套约束的反例 —— 不要照抄它。
* 包内帧长 ≠ 60ms ⇒ 解码器按 60ms 缓冲去解一个 20ms 的包，多出来的部分
  是上一帧的残留 ⇒ 听起来像"卡带/回声"，而日志一切正常。

编码阶段**不会**报任何错，生成脚本的 self-check 也只是看字节数。所以必须
在拿到文件之后，直接从字节里把 OpusHead 和每个包的 TOC 读出来核对。

用法::

    python tools/verify_elder_clips.py                     # 校验默认目录下全部 elder_*.ogg
    python tools/verify_elder_clips.py --dir <目录>         # 指定目录
    python tools/verify_elder_clips.py --file x.ogg ...     # 指定文件
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "firmware" / "xiaozhi" / "main" / "assets" / "common"

#: 硬约束：设备解码链路的采样率（boards/lichuang-dev/config.h + es8311 编解码器）
EXPECT_SAMPLE_RATE = 24000
EXPECT_CHANNELS = 1
EXPECT_FRAME_MS = 60.0

#: Opus TOC 的 config(0-31) → 单帧时长（毫秒）。表见 RFC 6716 附录 A。
_CONFIG_FRAME_MS = (
    (10, 20, 40, 60),   # 0-3   SILK NB
    (10, 20, 40, 60),   # 4-7   SILK MB
    (10, 20, 40, 60),   # 8-11  SILK WB
    (10, 20),           # 12-13 Hybrid SWB
    (10, 20),           # 14-15 Hybrid FB
    (2.5, 5, 10, 20),   # 16-19 CELT NB
    (2.5, 5, 10, 20),   # 20-23 CELT WB
    (2.5, 5, 10, 20),   # 24-27 CELT SWB
    (2.5, 5, 10, 20),   # 28-31 CELT FB
)

#: config → 可读模式名，只用于打印（便于一眼看出 24kHz 落在哪个带）。
_MODE_NAME = {}
for _c in range(32):
    if _c < 4:
        _MODE_NAME[_c] = "SILK-NB"
    elif _c < 8:
        _MODE_NAME[_c] = "SILK-MB"
    elif _c < 12:
        _MODE_NAME[_c] = "SILK-WB"
    elif _c < 14:
        _MODE_NAME[_c] = "HYBRID-SWB"
    elif _c < 16:
        _MODE_NAME[_c] = "HYBRID-FB"
    elif _c < 20:
        _MODE_NAME[_c] = "CELT-NB"
    elif _c < 24:
        _MODE_NAME[_c] = "CELT-WB"
    elif _c < 28:
        _MODE_NAME[_c] = "CELT-SWB"
    else:
        _MODE_NAME[_c] = "CELT-FB"


def _crc32_ogg(data: bytes) -> int:
    """Ogg CRC32（多项式 0x04C11DB7，非反射）。与 zlib.crc32 不同。"""
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 \
                else (crc << 1) & 0xFFFFFFFF
    return crc


def parse_pages(data: bytes) -> list[dict]:
    """拆出全部 Ogg 页（不依赖外部库：本机没有 libogg）。"""
    pages = []
    off = 0
    while True:
        idx = data.find(b"OggS", off)
        if idx < 0:
            break
        if idx + 27 > len(data):
            raise ValueError(f"页头截断 @ {idx}")
        segs = data[idx + 26]
        seg_off = idx + 27
        if seg_off + segs > len(data):
            raise ValueError(f"段表截断 @ {idx}")
        body_size = sum(data[seg_off + i] for i in range(segs))
        body_off = seg_off + segs
        if body_off + body_size > len(data):
            raise ValueError(f"页体截断 @ {idx}")
        page = data[idx:body_off + body_size]
        crc_stored = struct.unpack("<I", page[22:26])[0]
        crc_calc = _crc32_ogg(page[:22] + b"\x00\x00\x00\x00" + page[26:])
        pages.append({
            "offset": idx,
            "header_type": page[5],
            "granule": struct.unpack("<q", page[6:14])[0],
            "serial": struct.unpack("<I", page[14:18])[0],
            "seq": struct.unpack("<I", page[18:22])[0],
            "crc_ok": crc_stored == crc_calc,
            "segments": [data[seg_off + i] for i in range(segs)],
            "body": data[body_off:body_off + body_size],
        })
        off = body_off + body_size
    return pages


def iter_packets(pages: list[dict]) -> list[bytes]:
    """按 lacing 还原包（255 表示"续"，末段 <255 收尾）。"""
    packets: list[bytes] = []
    pending = bytearray()
    for page in pages:
        pos = 0
        for seg in page["segments"]:
            pending += page["body"][pos:pos + seg]
            pos += seg
            if seg < 255:
                packets.append(bytes(pending))
                pending = bytearray()
    return packets


def packet_frame_ms(pkt: bytes) -> tuple[float, int]:
    """(单帧毫秒, 帧数)。

    TOC 首字节 = ``config(5) | s(1) | c(2)``（RFC 6716 §3.1）。

    ⚠️ **c == 3 是常态，不是异常**。libopus 在 24kHz 下编 60ms 帧时，
    SILK 到不了 60ms，于是它改用 CELT 的 20ms 帧并把**多个帧塞进同一个包**，
    ``c`` 就取 3（"任意帧数"），真实帧数写在第 2 个字节的低 6 位。
    本工程既有的 ``reminder_chime.ogg`` 正是这样（toc=0xdb → config 27
    = CELT SWB 20ms，帧数 3 ⇒ 60ms）。第一版校验脚本把 c==3 当错误，
    结果 7 个文件全红 —— 是脚本错了，不是音频错了。
    """
    if len(pkt) < 2:
        raise ValueError("包太短，读不出 TOC/帧数")
    toc = pkt[0]
    config = toc >> 3
    table = _CONFIG_FRAME_MS[config // 4]
    per = table[config % 4]
    code = toc & 0x03
    if code == 0:
        n = 1
    elif code in (1, 2):
        n = 2          # 1 = 两帧等长，2 = 两帧不等长（本工程不会产出 2）
    else:
        n = pkt[1] & 0x3F      # byte1 = vbr(1) | padding(1) | 帧数(6)
        if n == 0:
            raise ValueError("帧数为 0")
    return per, n


def check_file(path: Path) -> tuple[bool, str]:
    data = path.read_bytes()
    lines = [f"--- {path.name}（{len(data)} 字节）---"]
    ok = True

    pages = parse_pages(data)
    bad_crc = [p["seq"] for p in pages if not p["crc_ok"]]
    if bad_crc:
        ok = False
        lines.append(f"  ✗ CRC 错的页：{bad_crc[:5]}（共 {len(bad_crc)} 个）")

    packets = iter_packets(pages)
    if len(packets) < 3:
        return False, "\n".join(lines + [f"  ✗ 包太少（{len(packets)}），不是合法 OGG Opus"])

    head = packets[0]
    if head[:8] != b"OpusHead":
        return False, "\n".join(lines + [f"  ✗ 首包不是 OpusHead：{head[:8]!r}"])
    version = head[8]
    channels = head[9]
    pre_skip = struct.unpack("<H", head[10:12])[0]
    rate = struct.unpack("<I", head[12:16])[0]
    gain = struct.unpack("<h", head[16:18])[0]
    lines.append(f"  OpusHead: version={version} channels={channels} "
                 f"pre_skip={pre_skip} sample_rate={rate} output_gain={gain}")

    if rate != EXPECT_SAMPLE_RATE:
        ok = False
        lines.append(f"  ✗ 采样率 {rate} ≠ {EXPECT_SAMPLE_RATE}"
                     f"（PlaySound 会因此重建解码器）")
    if channels != EXPECT_CHANNELS:
        ok = False
        lines.append(f"  ✗ 声道 {channels} ≠ {EXPECT_CHANNELS}")

    if packets[1][:8] != b"OpusTags":
        ok = False
        lines.append(f"  ✗ 第二包不是 OpusTags：{packets[1][:8]!r}")

    audio = packets[2:]
    durations: dict[float, int] = {}
    modes: dict[str, int] = {}
    bad: list[int] = []
    for i, pkt in enumerate(audio):
        try:
            per, n = packet_frame_ms(pkt)
        except ValueError as exc:
            bad.append(i)
            lines.append(f"  ✗ 第 {i} 个音频包：{exc}")
            continue
        total = per * n
        durations[total] = durations.get(total, 0) + 1
        mode = _MODE_NAME.get(pkt[0] >> 3, f"config{pkt[0] >> 3}")
        modes[f"{mode}/{per:g}ms x{n}"] = modes.get(f"{mode}/{per:g}ms x{n}", 0) + 1
        if abs(total - EXPECT_FRAME_MS) > 0.01:
            bad.append(i)

    lines.append(f"  音频包 {len(audio)} 个；包时长分布 "
                 f"{ {k: v for k, v in sorted(durations.items())} }")
    lines.append(f"  编码形态 {modes}")
    if bad:
        ok = False
        lines.append(f"  ✗ {len(bad)} 个包不是 {EXPECT_FRAME_MS:g}ms：下标 {bad[:8]}")

    # granule 是 48kHz 口径（Opus 规范固定），总时长 = 末页 granule / 48
    total_ms = pages[-1]["granule"] / 48.0
    expect_ms = len(audio) * EXPECT_FRAME_MS
    lines.append(f"  granule 总时长 {total_ms:.0f}ms / 帧数折算 {expect_ms:.0f}ms"
                 f" / 页数 {len(pages)}")
    if abs(total_ms - expect_ms) > 1.0:
        ok = False
        lines.append("  ✗ granule 与帧数不一致（seek/时长会错，但解码不受影响）")

    lines.append("  ✓ 全部判据通过" if ok else "  ✗ 有判据未通过")
    return ok, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验固件内置 OGG 的 OpusHead 与帧长")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--file", nargs="*", default=None)
    ap.add_argument("--all", action="store_true",
                    help="校验目录下全部 .ogg（含既有音效，便于对照 16k 的 popup）")
    args = ap.parse_args(argv)

    if args.file:
        files = [Path(f) for f in args.file]
    else:
        pattern = "*.ogg" if args.all else "elder_*.ogg"
        files = sorted(Path(args.dir).glob(pattern))
    if not files:
        print("没有找到要校验的 ogg", file=sys.stderr)
        return 1

    failed = 0
    for path in files:
        ok, text = check_file(path)
        print(text)
        if not ok:
            failed += 1

    print(f"\n共 {len(files)} 个文件，失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
