#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把表情 PNG 转成 LVGL 的 cbin 图片（28 字节头 + 裸 RGB565A8）。

为什么必须转（2026-11-02 核实，别退回 PNG）
------------------------------------------
`sdkconfig.h` 里 ``CONFIG_LV_CACHE_DEF_SIZE`` 是 **0** ⇒ LVGL 图片缓存是关的。
走 PNG 时 LVGL 每画一次就调一次 lodepng 解一次（`lv_lodepng.c` 的
``lodepng_decode32``），64x64 解成 RGBA8888 峰值 16KB。静态表情勉强能忍，
但待机跑马灯是逐帧重绘，每帧重解四五张 PNG 会直接把 LVGL 任务拖死。
cbin 让 LVGL 拿到的是现成的 ``lv_img_dsc_t``，一个字节都不用解。

文件格式（逐字节，32 位小端）
----------------------------
头就是 ``lv_img_dsc_t`` 的**内存镜像**，``cbin_img_dsc_create()`` 用
``malloc_cpy`` 盲拷 ``sizeof(lv_img_dsc_t)`` 字节，再把 ``data`` 字段
当**偏移**加上文件基址：

    偏移 类型  字段        值
    0    u8    magic       0x19   (LV_IMAGE_HEADER_MAGIC)
    1    u8    cf          0x14   (LV_COLOR_FORMAT_RGB565A8)
    2    u16   flags       0
    4    u16   w           图像宽
    6    u16   h           图像高
    8    u16   stride      w*2    (RGB565 平面的行字节数)
    10   u16   reserved_2  0
    12   u32   data_size   stride*h + w*h
    16   u32   data        28     (相对文件起始的偏移，不是指针)
    20   u32   reserved    0
    24   u32   reserved_2  0

⚠️ 头是 **28 字节**，不是 24：``lv_image_dsc_t`` = 12 字节
``lv_image_header_t``（三个 u32 位域单元：magic/cf/flags、w/h、stride/
reserved_2）+ data_size + data + reserved + reserved_2 = 12+16 = 28。
``lv_image_header_t`` 的字段顺序按**位域**落，不是按 4 字节对齐，写错会整体错位。

这条 28 是实测过的，不是推算：同一个 ``cbin_font_create()`` 盲拷的是
``sizeof(lv_font_t)``，而 `78/xiaozhi-fonts` 里 cbin 字库的 ``dsc`` 偏移
恰好是 36 —— 与按 `lv_font.h` 算出来的 ``sizeof(lv_font_t)`` 一致
（3 个函数指针 + 2 个 int32 + 4 个 u8 + 1 字节对齐 + 3 个指针 = 36，
dsc@24）。同一套算法用在 ``lv_img_dsc_t`` 上就是 28。若哪天 LVGL 被抬到
9.6（``lv_font_t`` 44 字节、字段重排），这里的头长**必须**跟着改，
否则症状是整屏花屏/白屏 —— 见 main/idf_component.yml 里 override_path 的长注释。

像素区从偏移 28 开始，**RGB565 平面在前、A8 平面在后**（lv_color.h:156
"Color array followed by Alpha array"）：

    [0, stride*h)            w*h 个 u16，小端 RGB565
    [stride*h, stride*h+w*h) w*h 个 u8，alpha

alpha 平面按 **w 字节一行**（不是 stride）：LVGL 在
`lv_draw_sw_img.c:263` 直接写死 ``mask_stride = img_stride / 2``。

为什么自己解 PNG 而不用 Pillow
-----------------------------
本文件在**编译期**跑（main/CMakeLists.txt 的 build_default_assets_bin），
而 CI 是在 ``espressif/idf:v5.5.1`` 容器里 ``python3`` 直接跑，容器里没有
Pillow、流程里也没有 pip install。门禁 tools/prebuild_check.py 同样要复算
一遍转换产物，它也只允许用标准库。所以这里只依赖 ``zlib``。

支持范围：8 bit、非隔行、colortype 6（RGBA）/ 2（RGB）。超出范围直接抛
异常让构建失败 —— 静默出一张花图比构建报错难查得多。
"""

import struct
import zlib

#: `LV_IMAGE_HEADER_MAGIC`（lv_image_dsc.h:25）
LV_IMAGE_HEADER_MAGIC = 0x19
#: `LV_COLOR_FORMAT_RGB565A8`（lv_color.h:156）
LV_COLOR_FORMAT_RGB565A8 = 0x14
#: `sizeof(lv_img_dsc_t)`，32 位；像素数据就从这里开始。推导见模块头注释。
CBIN_IMG_HEADER_SIZE = 28

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png(path):
    """解出 PNG 像素。返回 ``(w, h, bpp, pixels)``，pixels 是行优先原始字节。

    只支持 8bit / 非隔行 / colortype 6(RGBA) 或 2(RGB) —— 表情素材全是这种。
    """
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:8] != _PNG_SIG:
        raise ValueError(f"{path} 不是 PNG")

    pos = 8
    idat = bytearray()
    w = h = depth = ctype = interlace = None
    while pos < len(blob):
        (length,) = struct.unpack_from(">I", blob, pos)
        ctype_name = blob[pos + 4:pos + 8]
        body = blob[pos + 8:pos + 8 + length]
        pos += 12 + length          # 4 长度 + 4 类型 + 数据 + 4 CRC
        if ctype_name == b"IHDR":
            w, h, depth, ctype, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", body)
        elif ctype_name == b"IDAT":
            idat += body
        elif ctype_name == b"IEND":
            break

    if w is None:
        raise ValueError(f"{path} 缺少 IHDR")
    if depth != 8:
        raise ValueError(f"{path} 位深 {depth} 不支持（只支持 8bit）")
    if interlace != 0:
        raise ValueError(f"{path} 是隔行 PNG，不支持")
    if ctype not in (2, 6):
        raise ValueError(f"{path} colortype {ctype} 不支持（只支持 2=RGB / 6=RGBA）")

    bpp = 4 if ctype == 6 else 3
    stride = w * bpp
    raw = zlib.decompress(bytes(idat))
    if len(raw) != (stride + 1) * h:
        raise ValueError(f"{path} 解压后长度 {len(raw)} 与 {w}x{h} 不符")

    out = bytearray(stride * h)
    prev = bytearray(stride)
    src = 0
    for y in range(h):
        ft = raw[src]
        src += 1
        line = bytearray(raw[src:src + stride])
        src += stride
        if ft == 0:
            pass
        elif ft == 1:                                   # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:                                   # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:                                   # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:                                   # Paeth
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        else:
            raise ValueError(f"{path} 第 {y} 行过滤器 {ft} 不支持")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, bpp, out


def rgba_to_rgb565a8(w, h, bpp, pixels):
    """RGBA/RGB 原始像素 -> (RGB565 平面, A8 平面)。

    RGB565 是 5/6/5 量化（低位直接截断，与 LVGL 自己的 RGB888->RGB565 一致），
    量化误差由 build-out 的往返校验证明为 0。**不要**在这里对全透明像素做
    RGB 清零：那会让「转换产物 == 源图量化值」这条不变量失效，而 LVGL 混合
    时 alpha=0 的像素本来就只取底色，清不清零对成像没有区别。
    """
    stride = w * 2
    color = bytearray(stride * h)
    alpha = bytearray(w * h)
    o = 0
    ai = 0
    for i in range(0, len(pixels), bpp):
        r = pixels[i]
        g = pixels[i + 1]
        b = pixels[i + 2]
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        color[o] = v & 0xFF
        color[o + 1] = (v >> 8) & 0xFF
        o += 2
        alpha[ai] = pixels[i + 3] if bpp == 4 else 0xFF
        ai += 1
    return bytes(color), bytes(alpha)


def build_cbin(w, h, color, alpha):
    """拼出完整 cbin 文件（28 字节头 + 像素）。color/alpha 是裸平面。"""
    stride = w * 2
    data_size = stride * h + w * h
    header = struct.pack(
        "<BBHHHHHIIII",
        LV_IMAGE_HEADER_MAGIC,
        LV_COLOR_FORMAT_RGB565A8,
        0,                          # flags
        w,
        h,
        stride,
        0,                          # reserved_2
        data_size,
        CBIN_IMG_HEADER_SIZE,       # data：相对文件起始的偏移
        0,                          # reserved
        0,                          # reserved_2
    )
    assert len(header) == CBIN_IMG_HEADER_SIZE, len(header)
    assert len(color) + len(alpha) == data_size
    return header + color + alpha


def png_to_cbin(path):
    """PNG 文件 -> cbin 字节串。"""
    w, h, bpp, pixels = decode_png(path)
    color, alpha = rgba_to_rgb565a8(w, h, bpp, pixels)
    return build_cbin(w, h, color, alpha)
