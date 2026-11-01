"""灯效契约冒烟测试（效果 5 种 + 安全色 + 颜色随机）。

用法：
    python tools/smoke_lights.py

**为什么不起服务**：这里守的全是「纯函数 + 源码静态判据」，秒级出结果。
端到端的「改了灯效 → 心跳里原样下发」在 ``tools/smoke_home.py`` 的 3.5 组，
那里已经有一台真服务，为同一个契约再起一台只是多 40 秒和一份端口占用。

**这个脚本存在的理由（2026-11-01 改版时补）**
------------------------------------------------
改版前 ``grep LIGHT_EFFECTS|LIGHT_PRESETS|normalize_lights|light-presets tools/``
是**零命中** —— 灯效这块从落地起就没有任何断言。于是出现了用户实际观察到的
那个现象：界面上列了 15 个「预置」，实际只有 4 种能在设备上生效，
而全链路（前端 / 接口 / 下发 / 固件）**没有一处报错**。

具体要挡住的三类回归：

1. **服务端加了个固件不认识的 effect 字符串**
   ``sync.c`` 的 ``light_effect_from_str()`` 认不出就 ``LIGHT_EFFECT_KEEP``
   （静默保留默认），用户看到的是「换了个效果、灯没变」。两边日志都正常。
   ⇒ A / B 组用「解析固件源码」把两张表钉在一起，而不是靠人眼看。

2. **把"随机色"开放给了不支持的效果**
   固件对 ``solid`` / ``breath`` 是 ``c = s->color``、**不随时间变色**，
   ``color == 0`` 就是黑色 = 灯灭。界面上「常亮 + 随机」会变成「常亮 + 不亮」。
   ⇒ C 组同时验「服务端声明」与「归一化拦得住」。

3. **只改了一套前端**
   手机端有两套（``server/app/static/user.html`` 网页、
   ``app/src/pages/elder/avatar.vue`` APP）+ 管理端 ``index.html``。
   改「网页」漏「APP」时三边都会构建成功，只有装 APP 的人发现没变。
   ⇒ G 组逐文件断言。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
TOOLS = Path(__file__).resolve().parent
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from app.avatar_style import (  # noqa: E402
    DEFAULT_LIGHTS, LIGHT_COLORS, LIGHT_EFFECTS, LIGHT_STATES, RANDOM_EFFECTS,
    RANDOM_RGB, normalize_lights,
)

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))
    return ok


def strip_c_comments(text: str) -> str:
    """去掉 C 的 ``/* */`` 与 ``//`` 注释，**但不动字符串字面量里的内容**。

    为什么必须剥：静态判据是按「代码位」判的，不剥注释的话，一句
    「历史上这里是 if (s->color) ...」的注释就能让断言恒真 —— 那等于没有断言
    （本仓已经踩过一次：防回归注释自己踩红，见 MEMORY 第 3 条）。

    为什么要手写状态机而不是正则：``"http://..."`` 这种串里的 ``//``
    会被正则当成行注释，把后面的代码整段吃掉，断言随即变成永真。
    """
    out: list[str] = []
    i, n = 0, len(text)
    state = "code"          # code | line | block | str | chr
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "/" and nxt == "/":
                state = "line"; i += 2; continue
            if c == "/" and nxt == "*":
                state = "block"; i += 2; continue
            if c == '"':
                state = "str"; out.append(c); i += 1; continue
            if c == "'":
                state = "chr"; out.append(c); i += 1; continue
            out.append(c); i += 1; continue
        if state == "line":
            if c == "\n":
                state = "code"; out.append(c)
            i += 1; continue
        if state == "block":
            if c == "*" and nxt == "/":
                state = "code"; i += 2; continue
            if c == "\n":
                out.append(c)      # 保留换行，行号不错位
            i += 1; continue
        # str / chr：原样照抄，只认转义与收尾引号
        if c == "\\":
            out.append(text[i:i + 2]); i += 2; continue
        if (state == "str" and c == '"') or (state == "chr" and c == "'"):
            state = "code"
        out.append(c); i += 1
    return "".join(out)


def brace_body(text: str, sig: str) -> str:
    """取 ``sig`` 之后第一个 ``{`` 到配对的 ``}``（含花括号）。"""
    i = text.index(sig)
    j = text.index("{", i)
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[j:k + 1]
    raise AssertionError(f"没找到 {sig} 的闭合花括号")


# ============================================================ A 效果清单
def group_a() -> None:
    print("\n[A] 服务端效果清单（界面「灯效」下拉的唯一来源）")
    names = [e for e, _lab, _iv in LIGHT_EFFECTS]
    check("恰好 5 种效果", len(names) == 5, str(names))
    check("★ 效果顺序 = 用户指定的「关闭/呼吸/常亮/星光/流水」",
          names == ["off", "breath", "solid", "starlight", "flow"], str(names))
    check("★ 已下线的 blink 不再出现", "blink" not in names, str(names))
    check("效果 id 无重复", len(set(names)) == len(names), str(names))
    labels = {e: lab for e, lab, _iv in LIGHT_EFFECTS}
    check("「流水」的标签写明是跑马灯（用户原话）",
          "流水" in labels.get("flow", "") and "跑马灯" in labels.get("flow", ""),
          labels.get("flow", ""))
    # interval 是「间隔」列显不显示的依据：常亮/关闭没有"间隔"这回事。
    iv = {e: has for e, _lab, has in LIGHT_EFFECTS}
    check("常亮/关闭不吃 interval，呼吸/星光/流水吃",
          iv["solid"] is False and iv["off"] is False
          and iv["breath"] is True and iv["starlight"] is True and iv["flow"] is True,
          str(iv))


# ============================================================ B 两端对齐
def group_b() -> None:
    print("\n[B] 服务端效果 ⊆ 固件认识的字符串（sync.c）")
    src = (ROOT / "firmware/components/elder/src/sync.c").read_text(
        encoding="utf-8", errors="replace")
    body = brace_body(strip_c_comments(src),
                      "static uint8_t light_effect_from_str(const char *s)")
    fw = set(re.findall(r'strcmp\(\s*s\s*,\s*"([a-z_]+)"\s*\)', body))
    check("固件映射表解析出 >= 6 个字符串（含已下线但保留的 blink）",
          len(fw) >= 6, str(sorted(fw)))
    for eff, _lab, _iv in LIGHT_EFFECTS:
        check(f"★ 固件认识 \"{eff}\"（否则用户换效果灯不变，且两端都不报错）",
              eff in fw, f"固件只认 {sorted(fw)}")
    # 反向不要求相等：blink 是**刻意**保留的（灰度/回滚时老服务端还会发）。
    # 但固件认识的字符串不能全是没人发的 —— 至少 5 种里要覆盖住。
    check("固件不认识的效果字符串为 0（服务端不发明固件不认识的词）",
          set(e for e, _l, _i in LIGHT_EFFECTS) - fw == set(),
          str(set(e for e, _l, _i in LIGHT_EFFECTS) - fw))


# ============================================================ C 随机色
def group_c() -> None:
    print("\n[C] 颜色随机（哨兵 + 支持范围 + 归一拦截）")
    check("★ 哨兵值就是 #000000（固件靠 s->color == 0 识别，不需要第二个字段）",
          RANDOM_RGB == "#000000", RANDOM_RGB)
    check("★ 支持随机的只有 starlight 与 flow（用户明确点名的两种）",
          tuple(RANDOM_EFFECTS) == ("starlight", "flow"), str(RANDOM_EFFECTS))
    # 常亮/呼吸在固件里 c = s->color、不随时间变色 ⇒ color==0 就是灯灭。
    for eff in ("solid", "breath", "off"):
        out = normalize_lights({"neutral": {"effect": eff, "rgb": RANDOM_RGB}})["neutral"]
        check(f"★ {eff} + 随机色被打回默认色（否则「设了随机」= 灯不亮）",
              out["rgb"] != RANDOM_RGB and out["rgb"].startswith("#"),
              str(out))
    for eff in ("starlight", "flow"):
        out = normalize_lights({"neutral": {"effect": eff, "rgb": RANDOM_RGB}})["neutral"]
        check(f"★ {eff} + 随机色被保留（这是用户要的核心效果）",
              out["rgb"] == RANDOM_RGB and out["effect"] == eff, str(out))


# ============================================================ D 颜色归一
def group_d() -> None:
    print("\n[D] 颜色归一（拼错一个字符 = 灯不亮，必须在服务端拦住）")
    base = DEFAULT_LIGHTS["neutral"]["rgb"]

    def rgb_of(raw: str, eff: str = "breath") -> str:
        return normalize_lights({"neutral": {"effect": eff, "rgb": raw}})["neutral"]["rgb"]

    check("合法 6 位 hex 原样保留", rgb_of("#123456") == "#123456", rgb_of("#123456"))
    check("小写归一成大写", rgb_of("#4a90d9") == "#4A90D9", rgb_of("#4a90d9"))
    check("'#' 可省略（老数据/手填）", rgb_of("4A90D9") == "#4A90D9", rgb_of("4A90D9"))
    for bad in ("#FFF", "#FF00000", "#GGGGGG", "", "red", "#12 456", None):
        got = rgb_of(bad)          # type: ignore[arg-type]
        check(f"非法颜色 {bad!r} 退回默认色而不是原样落库",
              got == base, f"{got} != {base}")
    check("默认色本身是合法 hex（否则默认值会被自己的校验打回）",
          re.fullmatch(r"#[0-9A-F]{6}", base) is not None, base)


# ============================================================ E 固件流水
def group_e() -> None:
    print("\n[E] 固件「流水」支持固定色与随机色（rgb.c）")
    src = (ROOT / "firmware/components/elder/src/rgb.c").read_text(
        encoding="utf-8", errors="replace")
    code = strip_c_comments(src)
    i = code.index("case ELDER_FX_FLOW:")
    j = code.index("case ELDER_FX_RAINBOW:")
    block = code[i:j]
    check("★ 流水分支读 s->color（不读就等于固定色被忽略，选什么都是彩虹）",
          "s->color" in block, block[:160])
    check("★ 随机分支仍在（s->color == 0 时走 hsv2rgb 彩点）",
          "hsv2rgb" in block, block[:160])
    check("流水分支用 mod 做行波（固定色时靠亮度变化才看得出「在流」）",
          re.search(r"\bmod\s*=", block) is not None, block[:160])
    # 行波底不能是 0：全灭会被当成"灯带断了"，尤其这台设备是给老人看的。
    m = re.search(r"mod\s*=\s*(\d+)u?\s*\+", block)
    check("★ 行波有非零底（否则每颗灯都会周期性全灭，像故障）",
          bool(m) and int(m.group(1)) >= 20, m.group(0) if m else "没找到底值")


# ============================================================ F 下发形状
def group_f() -> None:
    print("\n[F] 下发形状（4 个字段，不含给人看的字段）")
    out = normalize_lights(None)["neutral"]
    check("★ 归一输出恰好 4 个字段", set(out) == {"rgb", "effect", "brightness", "interval_ms"},
          str(sorted(out)))
    check("★ preset 字段已彻底消失（效果/颜色拆开后它只会变成第二份真源）",
          "preset" not in out, str(sorted(out)))
    check("7 个状态全在", set(normalize_lights(None)) == {k for k, _l, _d in LIGHT_STATES},
          str(sorted(normalize_lights(None))))
    api = (SERVER / "app/routers/device_api.py").read_text(encoding="utf-8")
    check("device_api 不再引用 v[\"preset\"]",
          'v["preset"]' not in api and "v['preset']" not in api, "")
    for f in ("effect", "rgb", "brightness", "interval_ms"):
        check(f"device_api 下发含 {f}", f'"{f}"' in api, "")


# ============================================================ G 三端前端
def group_g() -> None:
    print("\n[G] 三套前端都改了（只改一套时三边都能构建成功）")
    pages = [
        (SERVER / "app/static/index.html", "管理端网页"),
        (SERVER / "app/static/user.html", "用户端网页"),
    ]
    for path, label in pages:
        text = path.read_text(encoding="utf-8", errors="replace")
        tag = f"{label}({path.name})"
        check(f"★ {tag} 已无 lightPresets（旧 15 条预置下拉的残留）",
              "lightPresets" not in text, "")
        check(f"★ {tag} 已无 pickLight（旧「选预置」处理器）",
              "pickLight(" not in text, "")
        check(f"★ {tag} 已无 emotion_lights[st.key].preset",
              "emotion_lights[st.key].preset" not in text, "")
        for need in ("pickEffect", "openLightColor", "lightColors",
                     "lightRandomRgb", "lightColorName"):
            check(f"{tag} 含 {need}", need in text, "")
        check(f"{tag} 灯效下拉遍历 lightEffects",
              'v-for="e in lightEffects"' in text, "")
    # APP（uni-app）只有只读摘要，但 effect → 中文的兜底表必须跟着加 flow，
    # 否则装 APP 的人看到的是英文 "flow"。
    app = (ROOT / "app/src/pages/elder/avatar.vue").read_text(
        encoding="utf-8", errors="replace")
    check("★ APP 形象页的兜底表含 flow（否则显示英文 flow）",
          re.search(r"flow:\s*'流水'", app) is not None, "")
    check("APP 形象页把 #000000 显示成「随机」而不是黑色",
          "随机" in app and "000000" in app, "")


# ============================================================ 色板
def group_h() -> None:
    print("\n[H] 安全色板（弹窗里能选的全部颜色）")
    check("色板至少 8 色", len(LIGHT_COLORS) >= 8, str(len(LIGHT_COLORS)))
    check("色板 id 唯一", len({c["id"] for c in LIGHT_COLORS}) == len(LIGHT_COLORS), "")
    check("色板中文名唯一", len({c["name"] for c in LIGHT_COLORS}) == len(LIGHT_COLORS), "")
    bad = [c for c in LIGHT_COLORS if re.fullmatch(r"#[0-9A-F]{6}", c["rgb"]) is None]
    check("每色都是大写 #RRGGBB（固件解析器只认 6 位）", not bad, str(bad))
    check("★ 色板里没有纯白 #FFFFFF（8 颗满白 ≈480mA，板载 5V 偏紧）",
          all(c["rgb"].upper() != "#FFFFFF" for c in LIGHT_COLORS), "")
    check("★ 色板里没有黑色 #000000（它是「随机」哨兵，不能当普通色选）",
          all(c["rgb"].upper() != RANDOM_RGB for c in LIGHT_COLORS), "")


def main() -> int:
    print("=" * 64)
    print("灯效契约冒烟（效果 5 种 / 安全色 / 颜色随机）")
    print("=" * 64)
    group_a()
    group_b()
    group_c()
    group_d()
    group_e()
    group_f()
    group_g()
    group_h()
    print("\n" + "=" * 64)
    print(f"共 {PASS + FAIL} 条：通过 {PASS}，失败 {FAIL}")
    if FAILED:
        print("失败项：")
        for n in FAILED:
            print("  - " + n)
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
