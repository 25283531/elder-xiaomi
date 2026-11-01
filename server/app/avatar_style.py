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

import re

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

# 支持的动效 = 界面「灯效」下拉的全部内容。**唯一真源在这里**，
# 前端直接拉 ``/api/avatars/light-presets`` 渲染，不自己抄一份。
#
# ⚠️ 这张表**只能**放固件真认识的字符串。``sync.c`` 的
# ``light_effect_from_str()`` 认不出就 ``LIGHT_EFFECT_KEEP``（静默保留默认），
# 于是"选了个新效果、灯没变"而且两端都不报错。加一项之前先去
# ``firmware/components/elder/src/sync.c`` 把映射补上。
#
# 2026-11-01 用户定稿的 5 种：关闭 / 呼吸 / 常亮 / 星光 / 流水（跑马灯）。
# 原来那张 15 条的"预置灯效"表已删除 —— 它把"效果"和"颜色"焊死成一个
# 下拉项，用户要的是"先选效果、再选颜色"两个独立维度，且当时列表里
# 只有 4 种真能生效（``blink`` 也在，但界面根本看不出区别）。
LIGHT_EFFECTS: tuple[tuple[str, str, bool], ...] = (
    ("off", "关闭", False),
    ("breath", "呼吸", True),
    ("solid", "常亮", False),
    ("starlight", "星光", True),
    ("flow", "流水（跑马灯）", True),
)

# "颜色随机"的线上哨兵值。
#
# 为什么借用 ``#000000`` 而不是加一个 ``random: true`` 字段：固件侧的判据是
# ``s->color == 0`` 才走随机彩点（``rgb.c`` 的 TWINKLE 分支），而黑色在
# WS2812B 上本来就等于"灭"，用户不可能主动选它 —— 天然是空闲哨兵。
# 加布尔字段则要同时改 schema / 归一 / 下发 / 三套前端，收益为零。
RANDOM_RGB = "#000000"

# 哪些动效**支持**随机色。
#
# 只有这两种，这不是保守而是事实：
#   · ``starlight`` —— 每闪一次换一个色相（rgb.c 里 ``hsv2rgb(hash)``）；
#   · ``flow``      —— 每个灯位一个色相、整体滚动。
# 而 ``solid`` / ``breath`` 在固件里是 ``c = s->color``、**不随时间变色**，
# ``color == 0`` 就是黑色 = 灯灭。把"随机"开放给它们，用户会看到
# "设了随机，灯不亮了" —— 这是最坏的一种降级。
# ``normalize_lights()`` 会把不合法的组合打回默认色，别指望界面能拦住全部入口。
RANDOM_EFFECTS: tuple[str, ...] = ("starlight", "flow")

# 安全色板 = 「颜色」列弹窗里能选的全部颜色。
#
# "安全"指两件事：
#   ① **不过流**：WS2812B 单颗满白 60mA，8 颗全白 480mA，对板载 5V 供电偏紧。
#      所以这张表**没有纯白**，最亮的只是偏暖的 ``#FFE9C4``。
#   ② **不刺眼**：饱和度都收在中段，没有 ``#00FF00`` / ``#0000FF`` 这种
#      夜里看着像灼烧的纯色 —— 这台设备是放在老人卧室的。
# 用户要自定义颜色得先改这里（前端不提供自由取色器，这是刻意的）。
LIGHT_COLORS: tuple[dict, ...] = (
    {"id": "warm_white", "name": "暖白", "rgb": "#FFE9C4"},
    {"id": "red", "name": "正红", "rgb": "#FF2020"},
    {"id": "orange", "name": "橙", "rgb": "#FF7A18"},
    {"id": "amber", "name": "琥珀", "rgb": "#FFB020"},
    {"id": "yellow", "name": "暖黄", "rgb": "#FFD24A"},
    {"id": "green", "name": "草绿", "rgb": "#3AD07A"},
    {"id": "cyan", "name": "青", "rgb": "#7FD3FF"},
    {"id": "blue", "name": "天蓝", "rgb": "#4A90D9"},
    {"id": "indigo", "name": "蓝紫", "rgb": "#6C6CFF"},
    {"id": "purple", "name": "紫", "rgb": "#B98CFF"},
    {"id": "pink", "name": "品红", "rgb": "#FF5FA2"},
    {"id": "gray_blue", "name": "灰蓝", "rgb": "#5B6B8C"},
)

# brightness 的合法区间。0 等于关闭，所以下限给 1 —— 允许存 0 会出现
# "设成 0 但 effect 是 breath"这种自相矛盾的组合，固件只能二选一。
BRIGHTNESS_MIN, BRIGHTNESS_MAX = 1, 100
# 间隔区间：200ms 以下人眼分辨不出闪烁而是常亮，5000ms 以上像卡死。
INTERVAL_MIN, INTERVAL_MAX = 200, 5000

_EFFECT_INTERVAL = {eff: has_iv for eff, _lab, has_iv in LIGHT_EFFECTS}


def _light(effect: str, rgb: str, brightness: int = 100,
           interval_ms: int = 800) -> dict:
    """造一条状态灯效（效果 + 颜色两个独立维度）。

    2026-11-01 之前这里是 ``_light(preset_id)``，颜色和效果焊在一个
    "预置"里。改成两个参数之后 ``preset`` 字段整个消失了 —— 它只是
    "用户选的是哪一条预置"的回显锚点，效果和颜色都能独立选之后，
    这个锚点既表达不了状态、又会跟实际值不一致（改颜色时锚点必须失效），
    留着只会变成第二个真源。
    """
    return {
        "rgb": rgb,
        "effect": effect,
        "brightness": brightness,
        "interval_ms": interval_ms,
    }


# 每个状态的出厂灯效。
# ``angry`` 原来是 ``blink_red``（闪烁红），blink 不在新的 5 种里了，
# 改成"呼吸红" —— 不能改成常亮红，那会和 ``alert``（常亮红）一模一样，
# 用户就分不出"生气"和"该吃药了"。
DEFAULT_LIGHTS: dict[str, dict] = {
    "neutral": _light("breath", "#4A90D9"),
    "listening": _light("breath", "#FFB020"),
    "speaking": _light("breath", "#7FD3FF", brightness=80, interval_ms=600),
    "happy": _light("breath", "#3AD07A"),
    "sad": _light("solid", "#5B6B8C", brightness=70),
    "angry": _light("breath", "#FF2020", interval_ms=400),
    "alert": _light("solid", "#FF2020"),
}


_RGB_RE = re.compile(r"^#[0-9A-F]{6}$")


def _norm_rgb(raw: object, effect: str, fallback: str) -> str:
    """把颜色归一成 ``#RRGGBB``（大写）。非法值 / 不支持随机的组合退回默认色。

    为什么非要在这里拦，而不是"前端已经限制过了"：
      · ``light_rgb_from_str()`` 在固件侧对非法串返回 **0**，而 0 对
        ``solid`` / ``breath`` 就是**灯灭**。一个拼错的颜色 = 灯不亮，
        且两端日志都正常 —— 这是最坏的一种降级。
      · ``#000000`` 是"随机"哨兵，但它只对 ``RANDOM_EFFECTS`` 有意义。
        用户先选"星光 + 随机"再把效果改成"常亮"时，rgb 会原样留着，
        变成"常亮 + 黑色" = 灯灭。归一必须把这个组合打回默认色。
    """
    s = str(raw or "").strip()
    if not s.startswith("#"):
        s = "#" + s          # '#' 可省略：老数据/手填的 "4A90D9" 是同一个颜色
    s = s.upper()
    if not _RGB_RE.match(s):
        return fallback
    if s == RANDOM_RGB and effect not in RANDOM_EFFECTS:
        return fallback
    return s


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
        effect = str(item.get("effect") or base["effect"])
        if effect not in _EFFECT_INTERVAL:
            # 认不出的效果（含已下线的 blink / 别的版本存的字符串）退回默认。
            # 注意这里**不能**退回 solid：把"呼吸"拼错成"breathh"就变成
            # 一直亮着，夜里刺眼且没人能排查出来。默认值是安全的。
            effect = base["effect"]
        # 颜色校验放在效果确定**之后**：随机色是否合法取决于效果。
        rgb = _norm_rgb(item.get("rgb"), effect, base["rgb"])
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
        out[key] = {
            "rgb": rgb, "effect": effect,
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
