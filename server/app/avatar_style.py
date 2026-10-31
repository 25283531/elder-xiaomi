"""形象「外观资产」的唯一真源：clips（动画片段映射）+ 情绪灯效。

为什么单独一个模块而不是塞在 ``routers/avatars.py`` 里
------------------------------------------------------
这三样东西被**三方**同时需要：
  1. ``routers/avatars.py`` —— 增删改查时归一；
  2. ``schemas.py`` —— ``AvatarOut`` 序列化时归一（老库里只有 rgb/effect，
     不补全的话前端滑杆显示空值）；
  3. ``main.py`` —— 首次部署 seed 默认形象。

放在 router 里会让 ``schemas`` 反向依赖 router（循环导入）；靠函数内
延迟 import 绕过去则等于把"这两个模块互相依赖"藏起来 —— 下次谁在
模块级加一行 import 就又炸。所以给它一个中立的位置。

⚠️ 本模块**只描述形状与默认值，不碰数据库**。设备侧到底消费不消费，
是另一件事（见 ``docs`` 与 ``routers/device_api.py``）。
"""
from __future__ import annotations

# ================================================================ clips
# 端侧约定的语义片段名，作为默认映射，便于管理页一键填充
DEFAULT_CLIPS: dict[str, str] = {
    "idle": "idle",
    "listening": "listening",
    "thinking": "thinking",
    "mouth_open": "mouth_open",
    "mouth_half": "mouth_half",
    "smile": "smile",
    "blink": "blink",
    "sleepy": "sleepy",
    "alert": "alert",
}

# clips 每一项的含义。前端据此渲染"状态 → 片段名"的表单，**不硬编码中文**。
CLIP_STATES: tuple[tuple[str, str], ...] = (
    ("idle", "待机"),
    ("listening", "聆听中"),
    ("thinking", "思考中"),
    ("mouth_open", "说话·张嘴"),
    ("mouth_half", "说话·半张"),
    ("smile", "微笑"),
    ("blink", "眨眼"),
    ("sleepy", "困倦"),
    ("alert", "警示（未服药）"),
)

# ================================================================ 灯效
# 状态键即 ``emotion_lights`` 的 key。顺序 = 前端展示顺序。
LIGHT_STATES: tuple[tuple[str, str, str], ...] = (
    ("neutral", "待机", "没有对话时的常态"),
    ("listening", "聆听中", "正在听用户说话"),
    ("speaking", "说话中", "正在播报回复"),
    ("happy", "开心", "识别到情绪开心"),
    ("sad", "难过", "识别到情绪低落"),
    ("angry", "生气", "识别到情绪生气"),
    ("alert", "提醒警示", "待办 / 用药提醒响铃时"),
)

# 支持的动效。``starlight``（星光）与 ``breath`` / ``blink`` 一样吃
# interval_ms；``solid`` 与 ``off`` 不吃（常亮没有"间隔"这回事）。
LIGHT_EFFECTS: tuple[tuple[str, str, bool], ...] = (
    ("off", "关闭", False),
    ("solid", "常亮", False),
    ("breath", "呼吸", True),
    ("blink", "闪烁", True),
    ("starlight", "星光", True),
)

# 预置灯效。**唯一真源在这里**，前端下拉直接拉接口，不自己抄一份 ——
# 抄一份的下场是加了个新灯效前端看不到，或者两边 rgb 对不上。
LIGHT_PRESETS: tuple[dict, ...] = (
    {"id": "off", "name": "关闭", "effect": "off", "rgb": "#000000"},
    {"id": "breath_blue", "name": "呼吸 · 蓝", "effect": "breath", "rgb": "#4A90D9"},
    {"id": "breath_cyan", "name": "呼吸 · 天青", "effect": "breath", "rgb": "#7FD3FF"},
    {"id": "breath_green", "name": "呼吸 · 绿", "effect": "breath", "rgb": "#3AD07A"},
    {"id": "breath_amber", "name": "呼吸 · 暖黄", "effect": "breath", "rgb": "#FFD24A"},
    {"id": "breath_purple", "name": "呼吸 · 紫", "effect": "breath", "rgb": "#9B7FE8"},
    {"id": "solid_blue", "name": "常亮 · 蓝", "effect": "solid", "rgb": "#4A90D9"},
    {"id": "solid_green", "name": "常亮 · 绿", "effect": "solid", "rgb": "#3AD07A"},
    {"id": "solid_gray", "name": "常亮 · 灰蓝", "effect": "solid", "rgb": "#5B6B8C"},
    {"id": "solid_red", "name": "常亮 · 红", "effect": "solid", "rgb": "#FF2020"},
    {"id": "blink_red", "name": "闪烁 · 红", "effect": "blink", "rgb": "#FF2020"},
    {"id": "blink_amber", "name": "闪烁 · 琥珀", "effect": "blink", "rgb": "#FFB020"},
    {"id": "starlight_white", "name": "星光 · 白", "effect": "starlight", "rgb": "#FFFFFF"},
    {"id": "starlight_purple", "name": "星光 · 紫", "effect": "starlight", "rgb": "#B98CFF"},
    {"id": "starlight_cyan", "name": "星光 · 青", "effect": "starlight", "rgb": "#7FD3FF"},
)

LIGHT_PRESET_BY_ID: dict[str, dict] = {p["id"]: p for p in LIGHT_PRESETS}

# brightness 的合法区间。0 等于关闭，所以下限给 1 —— 允许存 0 会出现
# "设成 0 但 effect 是 breath"这种自相矛盾的组合，固件只能二选一。
BRIGHTNESS_MIN, BRIGHTNESS_MAX = 1, 100
# 间隔区间：200ms 以下人眼分辨不出闪烁而是常亮，5000ms 以上像卡死。
INTERVAL_MIN, INTERVAL_MAX = 200, 5000

_EFFECT_INTERVAL = {eff: has_iv for eff, _lab, has_iv in LIGHT_EFFECTS}


def _light(preset_id: str, brightness: int = 100, interval_ms: int = 800) -> dict:
    """按预置 id 造一条状态灯效。

    ``preset`` 是回显用的锚点，``rgb``/``effect`` 是**实际生效值** ——
    两个都存是刻意的：老代码与设备只认 rgb/effect，只存 preset 会让它们
    读到空；反过来只存 rgb/effect 则下拉框回显不出"当前选的是哪个预置"，
    用户会以为没保存成功。
    """
    p = LIGHT_PRESET_BY_ID[preset_id]
    return {
        "preset": preset_id,
        "rgb": p["rgb"],
        "effect": p["effect"],
        "brightness": brightness,
        "interval_ms": interval_ms,
    }


# 每个状态的出厂灯效
DEFAULT_LIGHTS: dict[str, dict] = {
    "neutral": _light("breath_blue"),
    "listening": _light("breath_amber"),
    "speaking": _light("breath_cyan", brightness=80, interval_ms=600),
    "happy": _light("breath_green"),
    "sad": _light("solid_gray", brightness=70),
    "angry": _light("blink_red", interval_ms=400),
    "alert": _light("solid_red"),
}


def normalize_lights(raw: object) -> dict[str, dict]:
    """把 ``emotion_lights`` 归一成 7 个状态 × 固定字段的形状。

    为什么要归一而不是"存什么读什么"：这个字段在界面上是一个
    "每状态一行"的表单，缺行 = 该状态没有灯效 = 设备行为未定义。
    前端漏传一个状态时，这里补成默认值，设备侧就不会因为查不到 key 而灭灯。
    """
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, dict] = {}
    for key, _label, _desc in LIGHT_STATES:
        base = DEFAULT_LIGHTS[key]
        item = src.get(key)
        item = item if isinstance(item, dict) else {}
        rgb = str(item.get("rgb") or base["rgb"])
        effect = str(item.get("effect") or base["effect"])
        if effect not in _EFFECT_INTERVAL:
            effect = base["effect"]
        try:
            brightness = int(item.get("brightness", base["brightness"]))
        except (TypeError, ValueError):
            brightness = base["brightness"]
        brightness = max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, brightness))
        try:
            interval = int(item.get("interval_ms", base["interval_ms"]))
        except (TypeError, ValueError):
            interval = base["interval_ms"]
        interval = max(INTERVAL_MIN, min(INTERVAL_MAX, interval))
        preset = str(item.get("preset") or "")
        # 整项缺失（新建形象 / 老数据里没这一行）时，rgb/effect/brightness
        # 全部取自 DEFAULT_LIGHTS —— 那 preset 也必须取默认表里的那个 id，
        # 否则新建形象的 7 个状态在界面上全显示「自定义」，而它实际就是
        # "呼吸·蓝"这些出厂预置。注意这**不是**反查：没有从 rgb+effect 猜 id。
        if not item:
            preset = base["preset"]
        # preset 与 rgb/effect 对不上时**以实际值为准**、preset 置空：
        # 反过来（以 preset 为准）会把用户手改的颜色悄悄改回去。
        # 注意必须校验"一致"而不只是"id 存在" —— 存了 preset=solid_red
        # 但 rgb 被改成别的颜色时，若只查 id 存在，下拉会一直显示"常亮·红"
        # 而实际颜色是别的，用户改了颜色却看到"没改"。
        p = LIGHT_PRESET_BY_ID.get(preset)
        if p is None or p["rgb"].lower() != rgb.lower() or p["effect"] != effect:
            preset = ""
        out[key] = {
            "preset": preset, "rgb": rgb, "effect": effect,
            "brightness": brightness, "interval_ms": interval,
        }
    return out


def normalize_clips(raw: object) -> dict[str, str]:
    """clips 归一成 9 个状态全在的映射；缺的补默认，多的丢掉。

    丢多的而不是原样保留：固件侧按键名查表，多出来的键永远查不到，
    留着只会让"我明明配了 12 个片段"变成一句查不出所以然的抱怨。
    """
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    for key, _label in CLIP_STATES:
        val = src.get(key)
        out[key] = (str(val).strip()
                    if isinstance(val, str) and val.strip() else DEFAULT_CLIPS[key])
    return out
