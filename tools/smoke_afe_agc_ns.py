"""守 AGC 与降噪**真的生效**（静态接线 + 配置一致性）。

为什么需要这个脚本 —— 这两件事都出过"配了但什么都没发生"：

1. **降噪**：10-31 把 `CONFIG_SR_NSN_WEBRTC` 改成 `CONFIG_SR_NSN_NSNET2`，
   理由是"模型进 model 列表 ⇒ ns_init=true"。可本机分区表
   （`partitions/v2/16m.csv`）**没有 `model` 分区** ⇒ `esp_srmodel_filter`
   恒为空 ⇒ `ns_init=false` 是唯一结局。结果是两个星期里"降噪"一直没开，
   而构建、CI、代码审查全绿 —— 唯一能看出来的地方是开机日志的 AFE 管线串
   （`|AEC(...)| ->  -> |VAD(...)|`，NS 槽是空的）。
   所以本脚本把「没有 model 分区 ⇒ 必须走 WebRTC 算法」这条推理钉死：
   哪天有人加了 model 分区，第 2 组会红，提醒重新评估（那时 NSNet2 才可用）。

2. **AGC**：从 10-15 第一个提交起就是 `agc_init = false`。现场实测上行峰值
   只有 -29 dBFS（正常 -6~-12），老人说「你刚才没有说内容吗？」被识别成
   「你刚才没有说。」—— 轻声的句尾整段丢失。这一条没有任何编译期约束，
   改回 false 也不会报错，只能靠断言守。

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


# ================================================================ 2. 降噪
def t_ns() -> None:
    print("\n── 2. 降噪：没有 model 分区 ⇒ 只能走 WebRTC 算法，且必须真的走到 ──")

    # 前提：分区表里没有 model 分区。这一条是"为什么不能用 NSNet2"的根。
    parts = []
    for line in PARTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts.append(line.split(",")[0].strip())
    print(f"  分区表 {PARTS.name}：{parts}")
    check("model" not in parts,
          "分区表里确实没有 model 分区（NSNet2 不可能加载）",
          "有 model 分区了 ⇒ 可以重新评估 NSNet2，这条断言该改")

    raw = AFE.read_text(encoding="utf-8")
    body = init_body(strip_cpp_comments(raw))
    check("AFE_NS_MODE_WEBRTC" in body, "Initialize() 里有 WebRTC 降噪分支",
          "没有 ⇒ 没有模型时只能 ns_init=false，降噪永远不生效")
    check('"WEBRTC"' in body, '使用了 esp-sr 约定的伪模型名 "WEBRTC"',
          "AFE_NS_MODE_WEBRTC 必须配 ns_model_name=\"WEBRTC\"，否则 NS 起不来")
    check("else if (kWebrtcNs)" in body,
          "WebRTC 分支的条件就是 kWebrtcNs（而不是被 #if 编译掉）",
          "写成 #if 包整段 ⇒ 只有一种配置被编译过，另一种是没编过的死代码；"
          "改成 else if (false) ⇒ 降噪永远走不到（变异验证抓到过）")

    # 两种配置都必须能编译：kWebrtcNs 的定义必须有 #ifdef / #else 两支
    head = strip_cpp_comments(raw)
    m = re.search(r"#ifdef\s+CONFIG_SR_NSN_WEBRTC(.*?)#endif", head, re.S)
    check(bool(m), "kWebrtcNs 用 #ifdef CONFIG_SR_NSN_WEBRTC 定义")
    if m:
        seg = m.group(1)
        check("constexpr bool kWebrtcNs = true" in seg, "#ifdef 分支里是 true")
        check("constexpr bool kWebrtcNs = false" in seg, "#else 分支里是 false",
              "只定义一支 ⇒ 另一种配置下编译不过")

    # 配置：生成的 sdkconfig 必须选 WebRTC，不许再选 NSNET2
    sdk = SDK.read_text(encoding="utf-8")
    sdk_code = "\n".join(l for l in sdk.splitlines() if not l.lstrip().startswith("#"))
    check("CONFIG_SR_NSN_WEBRTC=y" in sdk_code,
          "sdkconfig.defaults 里选了 CONFIG_SR_NSN_WEBRTC=y",
          "没选 ⇒ kWebrtcNs 为 false，降噪又变成死代码")
    check("CONFIG_SR_NSN_NSNET2=y" not in sdk_code,
          "sdkconfig.defaults 里不再选 CONFIG_SR_NSN_NSNET2=y（负向）",
          "选了它而本机没有 model 分区 ⇒ 降噪静默失效（本次修的就是这个）")

    # 模板与生成物必须一致：sdkconfig.defaults 是生成文件，手改会被覆盖
    merge = MERGE.read_text(encoding="utf-8")
    check("CONFIG_SR_NSN_WEBRTC=y" in merge,
          "模板 merge_to_xiaozhi.py 里也是 WEBRTC",
          "只改生成文件 ⇒ 下次 merge 被覆盖，功能静默消失（MEMORY 第 6 条）")
    check("CONFIG_SR_NSN_NSNET2=y" not in
          "\n".join(l for l in merge.splitlines() if not l.lstrip().startswith("#")),
          "模板里不再选 NSNET2（负向）")


# ================================================================ 3. 门禁
def t_gate() -> None:
    print("\n── 3. 这个文件本身必须被检查、且被 CI 跑 ──")
    syn = SYNTAX.read_text(encoding="utf-8")
    check("afe_audio_processor.cc" in syn,
          "syntax_check_elder.py 覆盖了 afe_audio_processor.cc",
          "它编译失败 = 整机起不来（AFE 创建不出来就没有音频任务）")
    wf = WF.read_text(encoding="utf-8")
    check("tools/smoke_afe_agc_ns.py" in wf,
          "firmware-build.yml 里跑了本脚本",
          "写了不跑 = 没门禁，而且不报错（MEMORY 第 1 条）")


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
