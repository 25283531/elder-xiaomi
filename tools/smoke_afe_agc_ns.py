"""守 AGC **真的开着**、降噪**真的关着**（静态接线 + 配置一致性）。

为什么需要这个脚本 —— 这两件事都出过"配了但什么都没发生"或"配了直接把机器搞挂"：

1. **AGC**：从 10-15 第一个提交起就是 `agc_init = false`。现场实测上行峰值
   只有 -29 dBFS（正常 -6~-12），老人说「你刚才没有说内容吗？」被识别成
   「你刚才没有说。」—— 轻声的句尾整段丢失。这一条没有任何编译期约束，
   改回 false 也不会报错，只能靠断言守。

2. **降噪**：这里出过两次事故，两次都**构建全绿**：

   a) 10-31 把 `CONFIG_SR_NSN_WEBRTC` 改成 `CONFIG_SR_NSN_NSNET2`，
      理由是"模型进 model 列表 ⇒ ns_init=true"。可本机分区表
      （`partitions/v2/16m.csv`）**没有 `model` 分区** ⇒ `esp_srmodel_filter`
      恒为空 ⇒ `ns_init=false` 是唯一结局。两个星期里"降噪"一直没开，
      唯一能看出来的地方是开机日志的 AFE 管线串（`|AEC(...)| ->  -> |VAD(...)|`，
      NS 槽是空的）。

   b) 11-04 想把 (a) 修好，于是"没有模型就走 WebRTC 算法"（WebRTC NS 是算法、
      不需要 model 分区，看起来完美）。结果 **唤醒后设备必重启**，用户连试
      三次三次重启。原因：WebRTC NS 仍然要求 `ns_model_name` 能在模型列表里
      解析出来，而列表是空的 ⇒ 空指针解引用，`create_from_config` 根本没返回。

   所以现在的结论是**关着**：没有 model 分区就不要开降噪。本脚本第 2 组把这条
   结论钉死 —— 谁再"好心"打开它，CI 立刻红，而不是等到用户手上重启三次。

断言纪律（本仓踩过的坑）：
  * **先剥注释再匹配代码位**，否则解释这件事的注释自己会把断言踩红；
  * 负向断言与正向断言成对出现（"必须是这样" + "不许是那样"）；
  * 不做整文件子串匹配就下结论 —— 每个判据都限定在具体函数/具体段落内。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AFE = ROOT / "firmware" / "xiaozhi" / "main" / "audio" / "processors" / "afe_audio_processor.cc"
SDK = ROOT / "firmware" / "xiaozhi" / "sdkconfig.defaults"
MERGE = ROOT / "tools" / "merge_to_xiaozhi.py"
PARTS = ROOT / "firmware" / "xiaozhi" / "partitions" / "v2" / "16m.csv"
SYNTAX = ROOT / "tools" / "syntax_check_elder.py"
WF = ROOT / ".github" / "workflows" / "firmware-build.yml"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"   {detail}" if not ok and detail else ""))


def strip_cpp_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，保留字符串字面量。

    不剥注释的后果（真踩过）：解释"为什么不能写 agc_init = false"的那段
    注释里就有这串字，负向断言会自己踩红，而且看起来像是代码真的错了。
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':                       # 字符串：原样搬到收尾引号
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(src[i:j])
            i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def init_body(src: str) -> str:
    """只取 Initialize() 的函数体：AFE 配置全在这里，别处的同名串不算。"""
    m = re.search(r"void\s+AfeAudioProcessor::Initialize\s*\(", src)
    if not m:
        return ""
    start = src.find("{", m.end())
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return src[start:]


def partition_names() -> list[str]:
    parts = []
    for line in PARTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts.append(line.split(",")[0].strip())
    return parts


def ns_choice(path: Path) -> str:
    """取文件里生效的 `CONFIG_SR_NSN_xxx=y`（注释行不算）。

    返回 "WEBRTC" / "NSNET2" / ""（都没选或选了两个以上）。
    """
    picked = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        m = re.fullmatch(r"CONFIG_SR_NSN_(WEBRTC|NSNET2)=y", s)
        if m:
            picked.append(m.group(1))
    return picked[0] if len(picked) == 1 else ""


# ================================================================ 1. AGC
def t_agc() -> None:
    print("\n── 1. AGC：必须开着，且参数是「留余量」而不是 WebRTC 默认的 -3dBFS ──")
    raw = AFE.read_text(encoding="utf-8")
    body = init_body(strip_cpp_comments(raw))
    check(bool(body), "能定位到 Initialize() 函数体", "函数被改名/挪走，本脚本失效")
    if not body:
        return

    check("agc_init = true" in body, "Initialize() 里 agc_init = true",
          "关着 ⇒ 远场说话电平上不去，ASR 丢轻声尾字（实测峰值只有 -29dBFS）")
    check("agc_init = false" not in body, "agc_init 不许是 false（负向）",
          "回到 false 就是本次要修的那个 bug")
    check("AFE_AGC_MODE_WEBRTC" in body, "agc_mode = AFE_AGC_MODE_WEBRTC",
          "AGC 模式必须显式指定，别依赖 afe_config_init 的默认值")

    m = re.search(r"agc_target_level_dbfs\s*=\s*(\d+)", body)
    check(bool(m), "显式设置 agc_target_level_dbfs",
          "不写就吃 WebRTC 默认 -3dBFS，老人凑近说话会削顶")
    if m:
        val = int(m.group(1))
        # 3 = -3dBFS（默认，太热）；6 = -6dBFS（留 6dB 余量）；再大就等于不放大
        check(3 < val <= 12, f"目标电平 {val}（-{val}dBFS）在留余量的合理区间",
              "削顶对 ASR 的伤害比小声更大，必须留余量")

    m = re.search(r"agc_compression_gain_db\s*=\s*(\d+)", body)
    check(bool(m) and int(m.group(1)) > 0, "显式设置 agc_compression_gain_db > 0",
          "压缩增益为 0 等于没放大")

    # 验证方法必须写在源码里：这台机器上**有两条** AFE 管线，只有第二条是我们的。
    # 只 grep 第一条（WakeNet 自己建的那条）会得出"AGC 没生效"的错误结论 —— 真踩过。
    #
    # ⚠️ 判据要限定在「紧贴 agc_init 的那段注释」里：`AEC(VOIP_HIGH_PERF)` 这个串
    # 在上面的降噪事故注释里也出现过，整文件子串判据会被它蒙过去（变异验证抓到过）。
    idx = raw.find("afe_config->agc_init")
    blk = ""
    if idx > 0:
        b0 = raw.rfind("/*", 0, idx)
        b1 = raw.rfind("*/", 0, idx)
        if b0 >= 0 and b1 > b0:
            blk = raw[b0:idx]
    check("第二条" in blk and "|AGC(WebRTC)|" in blk,
          "紧贴 agc_init 的注释里写了「看第二条管线 + 找 |AGC(WebRTC)|」",
          "不写下来，下一个人还会去 grep 第一条 WakeNet 管线然后误判没生效")


# ================================================================ 2. 降噪
def t_ns() -> None:
    print("\n── 2. 降噪：没有 model 分区 ⇒ 必须关着（打开过一次，唤醒必重启）──")

    # 前提：分区表里没有 model 分区。这一条是"为什么不能开降噪"的根。
    parts = partition_names()
    print(f"  分区表 {PARTS.name}：{parts}")
    check("model" not in parts,
          "分区表里确实没有 model 分区（NSNet2 加载不到；WebRTC 伪名会崩）",
          "有 model 分区了 ⇒ 可以重新评估降噪，这条断言该改")

    raw = AFE.read_text(encoding="utf-8")
    code = strip_cpp_comments(raw)          # 剥注释：事故说明里就有这些字
    body = init_body(code)
    if not body:
        check(False, "能定位到 Initialize() 函数体（降噪判据的前提）")
        return

    # 2a. 三种"重新打开 WebRTC 降噪"的写法，逐一点名拦。
    check("AFE_NS_MODE_WEBRTC" not in body,
          "Initialize() 里不许出现 AFE_NS_MODE_WEBRTC（负向）",
          "2026-11-04 事故原样：没有 model 分区时它会空指针解引用 ⇒ 唤醒即重启")
    check('"WEBRTC"' not in body,
          'Initialize() 里不许把 ns_model_name 设成伪模型名 "WEBRTC"（负向）',
          "空模型列表里找 \"WEBRTC\" ⇒ 解析不出来 ⇒ 崩")
    check("kWebrtcNs" not in code,
          "整个文件里不许再有 kWebrtcNs 这个开关（负向）",
          "留着它就是留了一条「改配置就崩」的路；要开降噪得先加 model 分区")

    # 2b. 无模型分支必须是 ns_init = false（位置判据，不整文件搜）。
    assigns = re.findall(r"ns_init\s*=\s*(true|false)", body)
    check(bool(assigns), "Initialize() 里显式设置了 ns_init",
          "不写就吃 afe_config_init 的默认值，等于没人负责")
    if assigns:
        check(assigns[-1] == "false",
              f"最后一处 ns_init 赋值是 false（实际序列 {assigns}）",
              "最后一处是 true ⇒ 无模型时也在开降噪 ⇒ 唤醒重启")
    check("afe_config->ns_init = false;" in body,
          "无模型分支写的是 ns_init = false（字面判据）",
          "这一句是「没有 model 分区就关降噪」的唯一落地点")

    # 2c. 事故记录必须在（防止有人把注释删了、然后"重新发现"这个想法）。
    check("2026-11-04 事故" in raw, "源码里保留了 11-04 事故的记录",
          "删掉记录 ⇒ 下一个人会再试一次同样的改法，再重启三次")
    check("空指针" in raw and "create_from_config" in raw,
          "事故记录写清了崩在哪（空指针 / create_from_config 没返回）",
          "只写「崩了」不写崩在哪，等于没记")

    # 2d. 配置：模板与生成物必须一致（sdkconfig.defaults 是生成文件，手改会被覆盖）
    sdk_val = ns_choice(SDK)
    tpl_val = ns_choice(MERGE)
    print(f"  sdkconfig.defaults: CONFIG_SR_NSN_{sdk_val or '?'}   "
          f"merge 模板: CONFIG_SR_NSN_{tpl_val or '?'}")
    check(bool(tpl_val), "merge 模板里只选了一个 SR_NSN 算法",
          "一个都没选或选了多个 ⇒ esp-sr 的 Kconfig choice 不确定")
    check(sdk_val == tpl_val,
          "生成物与模板的 SR_NSN 选择一致",
          "不一致 ⇒ 生成文件被手改过，下次 merge 会把它改回去（MEMORY 第 6 条）")

    # 现在这个符号对代码已经不起作用（代码不再 #ifdef 它），所以两个值都能编译；
    # 但**必须**和模板一致，否则就是"本地看着是 A、CI 编出来是 B"。
    check(sdk_val in ("WEBRTC", "NSNET2"),
          f"SR_NSN 选择落在合法值内（{sdk_val}）",
          "值为空 ⇒ 配置文件被写坏")


# ================================================================ 3. 门禁
def t_gate() -> None:
    print("\n── 3. 这个文件本身必须被检查、且被 CI 跑 ──")
    syn = SYNTAX.read_text(encoding="utf-8")
    check("afe_audio_processor.cc" in syn,
          "syntax_check_elder.py 覆盖了 afe_audio_processor.cc",
          "它编译失败 = 整机起不来（AFE 创建不出来就没有音频任务）")
    wf = WF.read_text(encoding="utf-8")
    # ⚠️ 不能只判"文件里出现过这个脚本名" —— `on.push.paths` 过滤器里也有一份，
    # 有人把 run 步骤删了、只留过滤器，子串判据照样绿（这就是"写了不跑"的坑）。
    # 所以必须限定在 `run:` 行上判。
    run_lines = [l.strip() for l in wf.splitlines() if l.strip().startswith("run:")]
    check(any("smoke_afe_agc_ns.py" in l for l in run_lines),
          "firmware-build.yml 里有 `run: ... smoke_afe_agc_ns.py` 这一步",
          "写了不跑 = 没门禁，而且不报错（MEMORY 第 1 条）；"
          "只留在 paths 过滤器里不算跑")


def main() -> int:
    print("=" * 64)
    print("AGC / 降噪 生效性检查（静态）")
    print("=" * 64)
    t_agc()
    t_ns()
    t_gate()

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
