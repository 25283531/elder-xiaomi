"""形象「外观资产」的唯一真源：情绪灯效。

为什么单独一个模块而不是塞在 ``routers/avatars.py`` 里
------------------------------------------------------
这些常量被**三方**同时需要：
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
# 这张表的取法**不是**"挑 12 个好看的颜色"，而是"这台产品实际在用的颜色"：
# 前 6 个（标注了出处的）就是固件 ``k_default_cfg`` 里 7 个状态的出厂色，
# 后 6 个是给用户换色时备选的。为什么必须把出厂色放进来：
# 用户新建形象 / 老数据没配过时，颜色列显示的是出厂色；它若不在色板里，
# 前端只能显示裸 hex（"#00BFFF"），用户既看不懂、也没法从色板里选回来。
#
# "安全"指两件事：
#   ① **不过流**：WS2812B 单颗满白 60mA，8 颗全白 480mA，对板载 5V 供电偏紧。
#      所以这张表**没有纯白**，最亮的只是偏暖的 ``#FFE9C4``。
#   ② **能亮得久**：没有 ``#00FF00`` / ``#0000FF`` 这类在暗处刺眼的纯绿纯蓝。
#      ``#FF0000`` / ``#D50000`` 是例外 —— 它们是提醒/生气的出厂色，
#      红光的电流效率高、观感也不刺，而且只在这两种状态下短时出现。
LIGHT_COLORS: tuple[dict, ...] = (
    {"id": "warm_white", "name": "暖白", "rgb": "#FFE9C4"},
    # ↓ 以下 6 个 = 固件 k_default_cfg 的出厂色（顺序对应状态）
    {"id": "alert_red", "name": "警示红", "rgb": "#FF0000"},    # 提醒警示
    {"id": "deep_red", "name": "深红", "rgb": "#D50000"},       # 生气
    {"id": "deep_orange", "name": "深橙", "rgb": "#FF8C00"},    # 说话中
    {"id": "green", "name": "翠绿", "rgb": "#00C853"},          # 开心
    {"id": "sky", "name": "深天蓝", "rgb": "#00BFFF"},          # 聆听中
    {"id": "blue", "name": "宝蓝", "rgb": "#2962FF"},           # 难过
    # ↓ 以下 5 个是备选色
    {"id": "amber", "name": "琥珀", "rgb": "#FFB020"},
    {"id": "yellow", "name": "暖黄", "rgb": "#FFD24A"},
    {"id": "cyan", "name": "天青", "rgb": "#7FD3FF"},
    {"id": "purple", "name": "紫", "rgb": "#B98CFF"},
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


# 每个状态的出厂灯效 —— **必须与固件 rgb.c 的 ``k_default_cfg`` 逐项一致**。
#
# 为什么必须一致（2026-11-01 用户问「新设备没自定义形象时是什么灯效」时发现）：
# 固件的兜底是**编译进二进制的** ``k_default_cfg`` —— 设备没网、第一次心跳还没
# 成功之前，它只能按那张表亮。而服务端每次心跳都会下发 ``DEFAULT_LIGHTS``。
# 两张表只要有一项不同，新设备就会在**第一次心跳成功的那一刻变色**：
# 开机是 A，联网几秒后变成 B，用户看到的是"设备自己乱改灯"。
#
# 所以这里的数值**不是重新设计的**，是从 rgb.c 抄过来的（对应关系见
# tools/smoke_lights.py 的 I 组，它解析 k_default_cfg 逐项比对，改错就红）：
#   k_default_cfg 的 level 是 0~255 感知亮度 → 这里换成 1~100 百分比
#   （固件下发时按 b*255/100 反算，153↔60、140↔55、178↔70 正好往返）。
DEFAULT_LIGHTS: dict[str, dict] = {
    # 待机：星光 + **颜色随机**。这不是我挑的 —— k_default_cfg 里
    # ELDER_RGB_ST_NEUTRAL 是 { ELDER_FX_TWINKLE, 0u, 153, 2000 }，
    # color=0 在 TWINKLE 分支里就是"每闪一次换一个随机色相"。
    # 也正因为如此，用户才会强调「在星光和流水灯效中，颜色随机是很重要的效果」。
    "neutral": _light("starlight", RANDOM_RGB, brightness=60, interval_ms=2000),
    "listening": _light("solid", "#00BFFF", brightness=55),   # 深天蓝
    "speaking": _light("solid", "#FF8C00", brightness=70),    # 深橙
    "happy": _light("solid", "#00C853"),
    "sad": _light("solid", "#2962FF"),
    "angry": _light("solid", "#D50000"),
    # 提醒：常亮红。interval_ms=500 不是给 effect 用的，是固件 rgb_task 的
    # 催促闪烁节奏（k_default_cfg 的 period_ms=500）。所以界面上"间隔"列
    # 对这一行显示 "—"（solid 不吃 interval），但值仍要存对、仍要下发。
    "alert": _light("solid", "#FF0000", interval_ms=500),
}


_RGB_RE = re.compile(r"^#[0-9A-F]{6}$")

# 最后的兜底色：暖白（= 安全色板第一项）。
#
# 什么时候会用到它：某状态的**默认色本身就是** RANDOM_RGB（待机就是），
# 而用户把效果改成了不支持随机的（呼吸/常亮）。此时"退回默认色"会退回
# #000000，而 #000000 对呼吸/常亮就是**灯灭** —— 等于用一个看似合理的
# 规则把灯关掉。所以兜底必须是一个真正能亮的颜色。
FALLBACK_RGB = "#FFE9C4"


def _norm_rgb(raw: object, effect: str, fallback: str) -> str:
    """把颜色归一成 ``#RRGGBB``（大写）。非法值 / 不支持随机的组合退回安全色。

    为什么非要在这里拦，而不是"前端已经限制过了"：
      · ``light_rgb_from_str()`` 在固件侧对非法串返回 **0**，而 0 对
        ``solid`` / ``breath`` 就是**灯灭**。一个拼错的颜色 = 灯不亮，
        且两端日志都正常 —— 这是最坏的一种降级。
      · ``#000000`` 是"随机"哨兵，但它只对 ``RANDOM_EFFECTS`` 有意义。
        用户先选"星光 + 随机"再把效果改成"常亮"时，rgb 会原样留着，
        变成"常亮 + 黑色" = 灯灭。归一必须把这个组合打回安全色。
    """
    s = str(raw or "").strip()
    if not s.startswith("#"):
        s = "#" + s          # '#' 可省略：老数据/手填的 "4A90D9" 是同一个颜色
    s = s.upper()
    if not _RGB_RE.match(s):
        s = fallback.upper()
    # ⚠️ 这一条必须作用在**最终值**上，包括刚从 fallback 取来的那个：
    # 待机的默认色就是 #000000，用户把待机改成"呼吸"时 fallback 会带着
    # #000000 过来，不拦就等于把灯关掉。
    if s == RANDOM_RGB and effect not in RANDOM_EFFECTS:
        return FALLBACK_RGB
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
