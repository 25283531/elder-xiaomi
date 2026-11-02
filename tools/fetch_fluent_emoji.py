#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取 Microsoft Fluent Emoji 的 3D 素材，裁 alpha 外框、缩到 128x128，产物入库。

⚠️ 这个脚本**只在开发机跑一次**，产物（21 张 PNG + THIRD_PARTY.md）直接
提交进 git。CI 与构建流程**永远不跑它**，理由三条：

  1. CI 容器（espressif/idf:v5.5.1）里没有 Pillow，也没有外网；构建期依赖
     网络 = 构建会随 GitHub 抖动而红。
  2. assets.bin 的字节必须只由仓库内容决定。每次构建去拉 21 个文件，上游
     换一张图固件就变了，`build_default_assets.py` 里那段"同一个 commit 编
     出来的固件二进制不一致就没法用校验和判断固件换没换"的注释会失效。
  3. 裁剪+缩放是不可逆的，必须是人看过对比图之后才入库的一次性动作。

什么时候需要重新跑
------------------
  · 换一套表情（改下面的 EMOJI_MAP）；
  · 上游修了某张图的画法，且我们确认要跟进（THIRD_PARTY.md 里记了
    上游 commit SHA，换 SHA 要同时更新那一行）；
  · 屏上表情尺寸变了（改 OUT_SIZE / CONTENT_SIZE）—— 同时要改
    `firmware/xiaozhi/scripts/emoji_cbin.py` 头注释里的 49,180 这个数。
重跑之后**必须**重跑 `tools/prebuild_check.py`（C17 会逐张转 cbin 并与源
图 IHDR 对账），再重新出 `--contact-sheet` 的对比图人眼过一遍。

为什么源尺寸选 128 而不是 256
-----------------------------
屏上 `emoji_box_` 是 134x110（lcd_display.cc:885，CONTAIN ⇒ 实际画 110px）。
cbin 体积 = 28 + w*h*3：128x128 = 49,180 B/张 ×21 = 0.98 MB；
256x256 = 196,636 B/张 ×21 = 3.94 MB。画到 110px 时 128 已经是 1.16 倍
超采样，256 纯属白占 3MB 分区和 3MB OTA 流量。

为什么必须裁 alpha 外框
-----------------------
Fluent 3D 的 256 画布上，图形只占 87.5%（alpha bbox 16,16→239,239，其余
是投影留白）。原样缩到 128 再画到 110px，人脸只剩约 96px —— 老人 1-2 米
外看不清。裁掉透明边再填满内容区，人脸能到约 107px。

用法
----
    python tools/fetch_fluent_emoji.py                     # 抓取+处理+写 THIRD_PARTY.md
    python tools/fetch_fluent_emoji.py --contact-sheet build-out/fluent3d_contact.png
    python tools/fetch_fluent_emoji.py --report            # 只打印每张的处理参数

`--contact-sheet` 不联网：它只读已经入库的 128 PNG，按设备实际绘制尺寸
110x110 排格子。**别按 256 出图**——放大到 256 看，屏上糊不糊根本看不出来。
"""

import argparse
import io
import os
import struct
import subprocess
import sys
import urllib.parse
import urllib.request
import zlib

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

REPO = "microsoft/fluentui-emoji"
BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/assets"

#: 入库目录（仓库内、git 跟踪）。绝不能放 managed_components —— 那里被
#: .gitignore 忽略，CI 是浅克隆，构建时取不到。
OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "firmware", "xiaozhi", "main", "assets", "emoji", "fluent3d_128")

#: 输出画布边长。与屏上绘制尺寸（110）留 1.16 倍余量，兼顾 LVGL 运行时缩放质量。
OUT_SIZE = 128

#: 内容区边长：128 - 2*4 = 120，四边各留 4px 透明边。
#: 留边不是为了好看，是给 LVGL 缩放时的双线性采样留缓冲 —— 图形顶到画布
#: 边缘时，110px 绘制会出现"脸被切了一圈"的观感。
CONTENT_SIZE = 120

#: 允许的最大放大倍率。超过就不放大、按原始尺寸居中放置（放大 = 把 256 的
#: 采样噪点一起放大）。当前 21 张全是缩小（0.50~0.54 倍），这条是防御性的，
#: 将来换 64px 源图或改 CONTENT_SIZE 时才会真触发。
MAX_UPSCALE = 1.15

#: 单张源图的硬性要求，任何一条不过就整体失败退出（不许静默跳过）
SRC_SIZE = 256

#: 21 个语义名 → (Fluent 官方 CLDR 名, 3D 目录下的文件名)
#: 官方名必须逐字匹配仓库里的目录名（大小写敏感，空格转 %20）；
#: 文件名是官方名小写、空格换下划线，**连字符保留**
#: （"Smiling face with heart-eyes" → smiling_face_with_heart-eyes_3d.png，
#:  写成 heart_eyes 是 404，2026-11-03 实测）。
EMOJI_MAP = [
    ("neutral",     "Neutral face",                    "neutral_face_3d.png"),
    ("happy",       "Slightly smiling face",           "slightly_smiling_face_3d.png"),
    ("laughing",    "Grinning squinting face",         "grinning_squinting_face_3d.png"),
    ("funny",       "Face with tears of joy",          "face_with_tears_of_joy_3d.png"),
    ("sad",         "Pensive face",                    "pensive_face_3d.png"),
    ("angry",       "Angry face",                      "angry_face_3d.png"),
    # ⚠️ crying 用 "Crying face"(1F622)，**不要**改回 "Loudly crying face"(1F62D)。
    # 后者把眼泪画成两条从眼眶直落到底的蓝色竖柱，110px 下看起来像"屏幕坏了/
    # 故障图案"，老人会以为设备出问题（2026-11-03 对比图人工判定，最坏的一种
    # 误读）。Crying face 是一滴挂在脸颊上的泪 + 深皱眉，语义清楚，且与
    # sad(Pensive face，无泪) 靠"有没有泪"拉开。
    ("crying",      "Crying face",                     "crying_face_3d.png"),
    ("loving",      "Smiling face with heart-eyes",    "smiling_face_with_heart-eyes_3d.png"),
    ("embarrassed", "Flushed face",                    "flushed_face_3d.png"),
    ("surprised",   "Hushed face",                     "hushed_face_3d.png"),
    ("shocked",     "Face screaming in fear",          "face_screaming_in_fear_3d.png"),
    ("thinking",    "Thinking face",                   "thinking_face_3d.png"),
    ("winking",     "Winking face",                    "winking_face_3d.png"),
    ("cool",        "Smiling face with sunglasses",    "smiling_face_with_sunglasses_3d.png"),
    ("relaxed",     "Relieved face",                   "relieved_face_3d.png"),
    ("delicious",   "Face savoring food",              "face_savoring_food_3d.png"),
    ("kissy",       "Face blowing a kiss",             "face_blowing_a_kiss_3d.png"),
    ("confident",   "Smirking face",                   "smirking_face_3d.png"),
    ("sleepy",      "Sleeping face",                   "sleeping_face_3d.png"),
    ("silly",       "Winking face with tongue",        "winking_face_with_tongue_3d.png"),
    ("confused",    "Face with rolling eyes",          "face_with_rolling_eyes_3d.png"),
]

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _pillow():
    """Pillow 可选：有就用（质量最好），没有就退回纯标准库实现。"""
    try:
        from PIL import Image  # noqa: PLC0415 - 故意延迟导入
        return Image
    except Exception:           # noqa: BLE001 - 任何导入失败都走退化路径
        return None


# ---------------------------------------------------------------------------
# 下载 + 校验
# ---------------------------------------------------------------------------

def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "xiaozhi-emoji-fetch"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return resp.read()


def verify_source(name, url, blob):
    """四道校验，任何一道不过都抛异常（不静默跳过）。

    为什么这么严：少一张 = 固件少一个表情，但构建照样全绿，屏上只是那个
    情绪永远回落到 FontAwesome 图标。这类"少一半还编译通过"的坑本仓踩过
    （见 build_default_assets.py:251 关于 .gif 的注释）。
    """
    if blob[:8] != _PNG_SIG:
        raise RuntimeError(f"{name}: 不是 PNG（前 8 字节 {blob[:8]!r}）")
    if len(blob) < 33:
        raise RuntimeError(f"{name}: 文件只有 {len(blob)} 字节")
    w, h, depth, ctype = struct.unpack(">IIBB", blob[16:26])
    if (w, h) != (SRC_SIZE, SRC_SIZE):
        raise RuntimeError(f"{name}: 源图 {w}x{h}，期望 {SRC_SIZE}x{SRC_SIZE}")
    if ctype not in (4, 6):
        raise RuntimeError(f"{name}: colortype={ctype} 没有 alpha 通道")
    if depth != 8:
        raise RuntimeError(f"{name}: 位深 {depth}，只支持 8")
    return w, h


def upstream_sha():
    """取上游 commit SHA。优先 git ls-remote（不吃 GitHub API 限流）。

    2026-11-03 实测 api.github.com 的 /commits 对未认证请求返回 403
    rate limit，所以 git 是主路径，API 只做兜底，都失败就记"未获取"。
    """
    try:
        out = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{REPO}", f"refs/heads/{BRANCH}"],
            capture_output=True, text=True, timeout=45, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.split()[0], "git ls-remote"
    except Exception:           # noqa: BLE001
        pass
    try:
        import json
        blob = fetch(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}")
        return json.loads(blob)["sha"], "GitHub API"
    except Exception:           # noqa: BLE001
        return "未获取（离线或限流）", "none"


# ---------------------------------------------------------------------------
# 裁剪 + 缩放 + 居中
# ---------------------------------------------------------------------------

def process_pillow(Image, blob):
    """Pillow 路径。返回 (128x128 RGBA Image, 报告 dict)。"""
    im = Image.open(io.BytesIO(blob))
    im.load()
    im = im.convert("RGBA")
    bbox = im.getchannel("A").getbbox()          # alpha > 0 的紧包围盒
    if bbox is None:
        raise RuntimeError("整张图 alpha 全 0")
    cw, ch = bbox[2] - bbox[0], bbox[3] - bbox[1]
    content = im.crop(bbox)

    scale = CONTENT_SIZE / max(cw, ch)
    upscaled = scale > MAX_UPSCALE
    if upscaled:
        # 不放大：按原始像素尺寸居中放置（会小于内容区，但绝不糊）
        target_w, target_h = cw, ch
    else:
        target_w = max(1, round(cw * scale))
        target_h = max(1, round(ch * scale))

    # ⚠️ 必须先转 RGBa（预乘 alpha）再缩放：直接缩 RGBA 时，全透明区域的
    # RGB 是 (0,0,0)，插值会把黑边渗到图形边缘，屏上一圈脏边。
    content = content.convert("RGBa").resize((target_w, target_h), Image.LANCZOS)
    content = content.convert("RGBA")

    canvas = Image.new("RGBA", (OUT_SIZE, OUT_SIZE), (0, 0, 0, 0))
    canvas.alpha_composite(content, ((OUT_SIZE - target_w) // 2,
                                     (OUT_SIZE - target_h) // 2))
    return canvas, {
        "src_box": (cw, ch), "bbox": bbox, "scale": scale,
        "upscaled": upscaled, "placed": (target_w, target_h),
    }


def _load_stdlib(path_hint, blob):
    """纯标准库解码：复用 emoji_cbin.py 的 PNG 解码器（同一份代码，不另写）。"""
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "firmware", "xiaozhi", "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from emoji_cbin import decode_png      # noqa: PLC0415
    tmp = path_hint + ".src.png"
    with open(tmp, "wb") as f:
        f.write(blob)
    try:
        w, h, bpp, px = decode_png(tmp)
    finally:
        os.remove(tmp)
    if bpp != 4:                            # colortype 2 补 alpha=255
        out = bytearray(w * h * 4)
        for i in range(w * h):
            out[i * 4:i * 4 + 3] = px[i * 3:i * 3 + 3]
            out[i * 4 + 3] = 255
        px = bytes(out)
    return w, h, px


def _resize_box(w, h, rgba, dw, dh):
    """面积平均缩放（预乘 alpha）。纯标准库兜底路径用。"""
    out = bytearray(dw * dh * 4)
    for dy in range(dh):
        sy0 = dy * h / dh
        sy1 = (dy + 1) * h / dh
        y0, y1 = int(sy0), min(h, max(int(sy0) + 1, int(sy1 + 0.9999)))
        for dx in range(dw):
            sx0 = dx * w / dw
            sx1 = (dx + 1) * w / dw
            x0, x1 = int(sx0), min(w, max(int(sx0) + 1, int(sx1 + 0.9999)))
            sa = sr = sg = sb = 0
            n = 0
            for y in range(y0, y1):
                row = y * w * 4
                for x in range(x0, x1):
                    o = row + x * 4
                    a = rgba[o + 3]
                    sa += a
                    sr += rgba[o] * a          # 预乘：避免透明区黑边渗色
                    sg += rgba[o + 1] * a
                    sb += rgba[o + 2] * a
                    n += 1
            o = (dy * dw + dx) * 4
            if sa > 0:
                out[o] = min(255, sr // sa)
                out[o + 1] = min(255, sg // sa)
                out[o + 2] = min(255, sb // sa)
            out[o + 3] = min(255, sa // n) if n else 0
    return bytes(out)


def process_stdlib(blob, path_hint):
    w, h, px = _load_stdlib(path_hint, blob)
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            if px[row + x * 4 + 3] > 0:
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
    if maxx < 0:
        raise RuntimeError("整张图 alpha 全 0")
    cw, ch = maxx - minx + 1, maxy - miny + 1
    crop = bytearray(cw * ch * 4)
    for y in range(ch):
        src = ((miny + y) * w + minx) * 4
        crop[y * cw * 4:(y + 1) * cw * 4] = px[src:src + cw * 4]

    scale = CONTENT_SIZE / max(cw, ch)
    upscaled = scale > MAX_UPSCALE
    tw, th = (cw, ch) if upscaled else (max(1, round(cw * scale)),
                                        max(1, round(ch * scale)))
    small = _resize_box(cw, ch, bytes(crop), tw, th)

    canvas = bytearray(OUT_SIZE * OUT_SIZE * 4)
    ox, oy = (OUT_SIZE - tw) // 2, (OUT_SIZE - th) // 2
    for y in range(th):
        d = ((oy + y) * OUT_SIZE + ox) * 4
        canvas[d:d + tw * 4] = small[y * tw * 4:(y + 1) * tw * 4]
    return bytes(canvas), {
        "src_box": (cw, ch), "bbox": (minx, miny, maxx + 1, maxy + 1),
        "scale": scale, "upscaled": upscaled, "placed": (tw, th),
    }


def encode_png_stdlib(w, h, rgba):
    """写 RGBA PNG（filter 0）。纯标准库兜底路径用。"""
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (_PNG_SIG + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
            chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# THIRD_PARTY.md
# ---------------------------------------------------------------------------

MIT_TEXT = """MIT License

Copyright (c) Microsoft Corporation.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def write_third_party(sha, sha_src, rows, tool):
    path = os.path.join(OUT_DIR, "THIRD_PARTY.md")
    lines = [
        "# 第三方素材：Microsoft Fluent Emoji（3D）",
        "",
        "本目录下的 21 张 PNG 是 Microsoft Fluent Emoji 3D 素材的**派生作品**：",
        "裁掉透明外框、等比缩放、居中对齐到 128x128 透明画布，并重命名为本固件",
        "使用的语义名。原图未做任何调色或重绘。",
        "",
        "## 来源",
        "",
        f"- 仓库：https://github.com/{REPO}",
        f"- 分支：`{BRANCH}`",
        f"- 上游 commit：`{sha}`（取自 {sha_src}）",
        f"- 路径规则：`assets/<官方表情名>/3D/<官方名小写下划线>_3d.png`",
        f"- 原图规格：{SRC_SIZE}x{SRC_SIZE} PNG / 8 bit RGBA",
        "",
        "## 我们做的修改",
        "",
        "1. 按 alpha>0 的包围盒裁出内容区（原图画布上图形只占 87.5%，其余是投影留白）；",
        f"2. 等比缩放到 {CONTENT_SIZE}x{CONTENT_SIZE} 内容区（四边各留 "
        f"{(OUT_SIZE - CONTENT_SIZE) // 2}px 透明边），预乘 alpha 后重采样以避免透明区黑边渗色；",
        f"3. 居中贴到 {OUT_SIZE}x{OUT_SIZE} 透明画布；",
        "4. 文件重命名为固件语义名（`neutral.png` 等），与 `emoji_collection` 词表一一对应。",
        "",
        f"生成工具：`tools/fetch_fluent_emoji.py`（{tool}）。该脚本只在开发机跑一次，",
        "产物入库，CI 不执行。",
        "",
        "## 文件对照",
        "",
        "| 本目录文件 | 上游官方名 | 上游文件名 |",
        "|---|---|---|",
    ]
    for name, official, fname, *_ in rows:
        lines.append(f"| `{name}.png` | {official} | `{fname}` |")
    lines += [
        "",
        "## 许可证（上游 LICENSE 全文）",
        "",
        "```",
        MIT_TEXT.rstrip(),
        "```",
        "",
        "MIT 允许商用、修改、闭源再分发；唯一义务是**保留上述版权声明和许可声明**",
        "（本文件即履行该义务，随固件源码一起分发）。",
        "",
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return path


# ---------------------------------------------------------------------------
# 对比图
# ---------------------------------------------------------------------------

def contact_sheet(out_path, draw_size=110, cols=7, bg="#D3DBFC"):
    """按设备实际绘制尺寸（110x110）拼对比图，每格下面标语义名。

    特意**不放大**：110px 是屏上的真实尺寸，放大到 256 看会掩盖掉
    "110px 下五官糊成一团"的问题。
    """
    Image = _pillow()
    if Image is None:
        raise RuntimeError("--contact-sheet 需要 Pillow")
    from PIL import ImageDraw, ImageFont

    rows = (len(EMOJI_MAP) + cols - 1) // cols
    pad, label_h, gap = 8, 18, 6
    cell_w = draw_size + gap * 2
    cell_h = draw_size + label_h + gap * 2
    W = cols * cell_w + pad * 2
    H = rows * cell_h + pad * 2
    sheet = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 14)
    except Exception:           # noqa: BLE001
        font = ImageFont.load_default()

    for i, (name, _official, _fname) in enumerate(EMOJI_MAP):
        src = os.path.join(OUT_DIR, name + ".png")
        if not os.path.isfile(src):
            raise RuntimeError(f"缺 {src}，先跑一次不带参数的抓取")
        im = Image.open(src).convert("RGBA").resize((draw_size, draw_size),
                                                    Image.LANCZOS)
        cx = pad + (i % cols) * cell_w + gap
        cy = pad + (i // cols) * cell_h + gap
        sheet.paste(im, (cx, cy), im)     # 用 alpha 贴到浅蓝底上，模拟屏上观感
        tw = draw.textlength(name, font=font)
        draw.text((cx + (draw_size - tw) / 2, cy + draw_size + 2), name,
                  fill=(20, 20, 20), font=font)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    sheet.save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="抓取 Fluent Emoji 3D 并生成 128x128 入库素材")
    ap.add_argument("--contact-sheet", metavar="PATH",
                    help="只出对比图（不联网，读已入库的 128 PNG）")
    ap.add_argument("--report", action="store_true", help="只打印每张的处理参数")
    args = ap.parse_args()

    if args.contact_sheet:
        print("对比图：", contact_sheet(args.contact_sheet))
        return 0

    Image = _pillow()
    tool = "Pillow " + getattr(Image, "__version__", "?") if Image else "纯标准库（面积平均）"
    print(f"图像后端：{tool}")
    os.makedirs(OUT_DIR, exist_ok=True)

    if not args.report:
        sha, sha_src = upstream_sha()
        print(f"上游 {REPO}@{BRANCH} = {sha}（{sha_src}）")

    rows, total_cbin = [], 0
    for name, official, fname in EMOJI_MAP:
        url = f"{RAW_BASE}/{urllib.parse.quote(official)}/3D/{fname}"
        if args.report:
            blob = b""
        else:
            try:
                blob = fetch(url)
            except Exception as exc:      # noqa: BLE001
                raise SystemExit(f"✗ {name}: 下载失败 {url}\n  {exc}") from exc
            verify_source(name, url, blob)

        if Image is not None:
            img, info = process_pillow(Image, blob)
            dst = os.path.join(OUT_DIR, name + ".png")
            img.save(dst, optimize=True)
        else:
            rgba, info = process_stdlib(blob, os.path.join(OUT_DIR, name))
            dst = os.path.join(OUT_DIR, name + ".png")
            with open(dst, "wb") as f:
                f.write(encode_png_stdlib(OUT_SIZE, OUT_SIZE, rgba))

        src_bytes = len(blob)
        out_bytes = os.path.getsize(dst)
        cbin = 28 + OUT_SIZE * OUT_SIZE * 3
        total_cbin += cbin
        cw, ch = info["src_box"]
        rows.append((name, official, fname, src_bytes, (cw, ch),
                     info["scale"], info["upscaled"], info["placed"], out_bytes))
        print(f"  {name:12s} {official:32s} 源 {src_bytes:6d}B "
              f"框 {cw}x{ch} 缩放 {info['scale']:.3f}"
              f"{' ⚠️放大' if info['upscaled'] else ''} "
              f"→ 放置 {info['placed'][0]}x{info['placed'][1]} "
              f"入库 {out_bytes:6d}B cbin {cbin}B")

    if args.report:
        return 0

    tp = write_third_party(sha, sha_src, rows, tool)
    print(f"\n写入 {len(rows)} 张 → {OUT_DIR}")
    print(f"THIRD_PARTY.md → {tp}")
    print(f"cbin 合计 {total_cbin} B（{total_cbin / 1024 / 1024:.2f} MB）")
    print("\n下一步：重跑 tools/prebuild_check.py（C17 会逐张转 cbin 对账），"
          "再出 --contact-sheet 人眼过一遍。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
