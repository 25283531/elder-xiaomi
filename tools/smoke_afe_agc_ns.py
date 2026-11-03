"""守 AGC 与降噪**真的按设计接线**（静态接线 + 配置一致性）。

为什么需要这个脚本 —— 这两件事都出过"配了但什么都没发生"或"配了直接把机器搞挂"，
而且两次都**构建全绿、CI 全绿**：

1. **降噪 = 纯算法（WebRTC NS），不加载任何模型。**
   这里踩过两个坑，一个比一个贵：

   a) 10-31 选了 `CONFIG_SR_NSN_NSNET2`，理由是"模型进 model 列表 ⇒
      ns_init=true"。可本机分区表（`partitions/v2/16m.csv`）**没有 `model`
      分区** ⇒ `esp_srmodel_filter(models, ESP_NSNET_PREFIX, NULL)` 恒为
      nullptr ⇒ 那条分支永远走不到。两个星期里"降噪"一帧都没做，唯一能看出
      来的地方是开机日志的 AFE 管线串（`|AEC(...)| ->  -> |VAD(...)|`，
      NS 槽是空的）。

   b) 11-04 上午想把 (a) 修好，照着 `esp_afe_config.h` 里那句
      `AFE_NS_MODE_WEBRTC = 0, // please use model name of NS, SSP: "WEBRTC"`
      写了 `ns_model_name = "WEBRTC"` —— **唤醒后设备必重启**（用户连试三次
      三次重启，Guru Meditation: LoadProhibited）。根因：AFE 只要看到
      ns_model_name 非空就按"模型名"去 libnsnet.a 里加载，空模型列表里解析
      "WEBRTC" ⇒ 空指针解引用 ⇒ create_from_config 不返回。

   ⇒ 现在 NSNet2 相关代码**全部删除**，只用算法降噪，且 `ns_model_name`
   必须是 `nullptr`。本脚本把这三件事同时钉死（第 2 组），因为少任何一件
   要么降噪静默失效、要么设备唤醒即重启。

2. **AGC**：从 10-15 第一个提交起就是 `agc_init = false`。现场实测上行峰值
   只有 -29 dBFS（正常 -6~-12），老人说「你刚才没有说内容吗？」被识别成
   「你刚才没有说。」—— 轻声的句尾整段丢失。这一条没有任何编译期约束，
   改回 false 也不会报错，只能靠断言守。

断言纪律（本仓踩过的坑）：
  * **先剥注释再匹配代码位**，否则解释这件事的注释自己会把断言踩红；
  * 负向断言与正向断言成对出现（"必须是这样" + "不许是那样"）；
  * 不做整文件子串匹配就下结论 —— 每个判据都限定在具体函数/具体段落内
    （`AEC(VOIP_HIGH_PERF)` 这个串在降噪事故注释里也出现过，整文件判据被它
    蒙过去一次，变异验证抓到的）。
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

    返回 "WEBRTC" / "NSNET2" / ""（都没选或选了多个）。
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
    print("\n── 1. AGC：必须开着，参数是「留余量 + 补得够」而不是 WebRTC 默认值 ──")
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
          "本管线里没有 WakeNet，WAKENET 模式会被 esp-sr 直接关掉（有日志为证）")
    check("AFE_AGC_MODE_WAKENET" not in body, "agc_mode 不许是 WAKENET（负向）",
          "esp-sr 会打印 wakenet is activated, disable WebRTC AGC. 并关掉 AGC")

    m = re.search(r"agc_target_level_dbfs\s*=\s*(\d+)", body)
    check(bool(m), "显式设置 agc_target_level_dbfs",
          "不写就吃 WebRTC 默认 -3dBFS，老人凑近说话会削顶")
    if m:
        val = int(m.group(1))
        # 3 = -3dBFS（默认，太热）；6 = -6dBFS（留 6dB 余量）；再大就等于不放大
        check(3 < val <= 12, f"目标电平 {val}（-{val}dBFS）在留余量的合理区间",
              "削顶对 ASR 的伤害比小声更大，必须留余量")

    m = re.search(r"agc_compression_gain_db\s*=\s*(\d+)", body)
    check(bool(m), "显式设置 agc_compression_gain_db")
    if m:
        val = int(m.group(1))
        # 9 是 esp-sr 默认值，实测补不够：上行峰值 -29dBFS，与正常 -6~-12
        # 差 20dB 以上，只补 9dB 补完还是偏轻。所以门槛定在"严格大于默认 9"。
        check(val > 9, f"压缩增益 {val} dB 严格大于 esp-sr 默认的 9",
              "9dB 是实测补不够的那个值（差 20dB 以上），别退回去")

    m = re.search(r"afe_linear_gain\s*=\s*([\d.]+)f?", body)
    check(bool(m), "显式设置 afe_linear_gain",
          "不写就依赖 afe_config_init 的默认值，读代码的人不知道有没有叠加固定增益")
    if m:
        check(abs(float(m.group(1)) - 1.0) < 1e-6,
              f"afe_linear_gain = {m.group(1)}（固定线性增益保持 1.0）",
              "AGC 已负责动态范围，再叠固定增益会把近场推爆")

    # 验证方法必须写在源码里：这台机器上**有两条** AFE 管线，只有第二条是我们的。
    # 只 grep 第一条（WakeNet 自己建的那条）会得出"AGC 没生效"的错误结论 —— 真踩过。
    #
    # ⚠️ 判据要限定在「紧贴 agc_init 的那段注释」里：`AEC(VOIP_HIGH_PERF)` 这个串
    # 在上面的降噪注释里也出现过，整文件子串判据会被它蒙过去（变异验证抓到过）。
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
    print("\n── 2. 降噪：只用算法（WebRTC NS），不许再有任何模型相关代码 ──")

    # 前提：分区表里没有 model 分区。这一条是"为什么不能用 NSNet2"的根。
    parts = partition_names()
    print(f"  分区表 {PARTS.name}：{parts}")
    check("model" not in parts,
          "分区表里确实没有 model 分区（NSNet2 不可能加载）",
          "有 model 分区了 ⇒ 可以重新评估 NSNet2，这条断言该改")

    raw = AFE.read_text(encoding="utf-8")
    code = strip_cpp_comments(raw)          # 剥注释：事故说明里就有这些字
    body = init_body(code)
    if not body:
        check(False, "能定位到 Initialize() 函数体（降噪判据的前提）")
        return

    # 2a. NSNet2 相关代码必须彻底消失（用户 2026-11-04 明确要求"删除"）。
    check("ESP_NSNET_PREFIX" not in body,
          "Initialize() 里不再有 esp_srmodel_filter(..., ESP_NSNET_PREFIX, ...)（负向）",
          "留着它就是在空模型列表里找 NSNet2，永远拿到 nullptr —— 走不到的死分支")
    check("AFE_NS_MODE_NET" not in body,
          "Initialize() 里不再有 AFE_NS_MODE_NET 分支（负向）",
          "神经网络降噪需要 model 分区，本机没有")
    check("afe_config->ns_model_name = ns_" not in body,
          "不再把 filter 结果赋给 ns_model_name（负向）",
          "那正是「没有模型」那条死分支的写法")

    # 2b. WebRTC NS 三件事，缺一不可。
    check("ns_init = true" in body, "Initialize() 里 ns_init = true",
          "关着 ⇒ 降噪一帧都不做（10-31~11-04 两个星期就是这么过的）")
    check("ns_init = false" not in body, "ns_init 不许是 false（负向）",
          "回到 false 就是「降噪静默失效」的那个状态")
    check("AFE_NS_MODE_WEBRTC" in body, "afe_ns_mode = AFE_NS_MODE_WEBRTC",
          "WebRTC NS 是纯算法，不读模型数据，与 model 分区无关")

    # 2c. 事故根因：ns_model_name 必须是 nullptr。
    check("ns_model_name = nullptr" in body,
          "ns_model_name = nullptr（**不是** \"WEBRTC\"）",
          "非空 ⇒ AFE 按模型名去 libnsnet.a 加载 ⇒ 空列表里解析 ⇒ 空指针 ⇒ 唤醒即重启")
    check('"WEBRTC"' not in body,
          'Initialize() 里不许出现 "WEBRTC" 这个伪模型名（负向）',
          '2026-11-04 事故原样：ns_model_name = "WEBRTC" ⇒ 三次唤醒三次重启')
    check("afe_config->ns_model_name = nullptr" in body,
          "显式写 nullptr（不是留空不管）",
          "不显式写就吃 afe_config_init 的默认值，等于没人负责")

    # 2d. 事故记录必须在（防止有人把注释删了、然后"重新发现"这个想法）。
    # ⚠️ 判据要限定在**紧贴 ns_init 的那段注释**里：
    #    * `2026-11-04` 在文件头的"定调"那句话里也有；
    #    * `create_from_config` 在本函数的代码里也有（`afe_data_ = ...`），
    #      整文件判据会被它蒙过去（变异验证两次都抓到了这种假绿）。
    idx = raw.find("afe_config->ns_init = true;")
    ns_blk = ""
    if idx > 0:
        b0 = raw.rfind("/*", 0, idx)
        b1 = raw.rfind("*/", 0, idx)
        if b0 >= 0 and b1 > b0:
            ns_blk = raw[b0:idx]
    check(bool(ns_blk), "能定位到紧贴 ns_init 的那段注释", "注释被挪走，下面三条判据失效")
    check("Guru Meditation" in ns_blk and "LoadProhibited" in ns_blk,
          "注释里记着崩溃类型（Guru Meditation / LoadProhibited）",
          "删掉记录 ⇒ 下一个人会照着同一个头文件注释再写一次 \"WEBRTC\"")
    # 崩溃调用链必须完整记下来：这些符号名和地址是**用 CI 产出的 xiaozhi.map
    # 解析 backtrace 得到的**，删掉就只能靠猜（这次就猜错过一次）。
    # ⚠️ 第一帧要判「函数名 + 偏移」而不是光判函数名：正文里也写着
    # "死在 esp_afe_handle_from_config 里面"，只判函数名会被正文蒙过去
    # （变异验证抓到的）。
    frames = ["esp_afe_handle_from_config +0x", "AfeAudioProcessor::Initialize",
              "AudioService::EnableVoiceProcessing", "Application::SetDeviceState",
              "OnWakeWordDetected"]
    missing = [f for f in frames if f not in ns_blk]
    check(not missing,
          "注释里记着完整的崩溃调用链（5 帧，含符号名与偏移）",
          f"缺：{missing} —— 只写「崩了」不写崩在哪，等于没记")
    check("ns_model_name 非空" in ns_blk,
          "注释里写清了根因（ns_model_name 非空 ⇒ 按模型名加载）",
          "不写根因 ⇒ 下次还是不知道为什么不能填 \"WEBRTC\"")

    # 2e. 配置：模板与生成物必须一致（sdkconfig.defaults 是生成文件，手改会被覆盖）
    sdk_val = ns_choice(SDK)
    tpl_val = ns_choice(MERGE)
    print(f"  sdkconfig.defaults: CONFIG_SR_NSN_{sdk_val or '?'}   "
          f"merge 模板: CONFIG_SR_NSN_{tpl_val or '?'}")
    check(tpl_val == "WEBRTC",
          f"模板选的是 WEBRTC（实际 {tpl_val or '未选'}）",
          "代码只走算法降噪，配置跟着选 WEBRTC；选 NSNET2 会让读代码的人以为支持模型降噪")
    check(sdk_val == tpl_val,
          "生成物与模板的 SR_NSN 选择一致",
          "不一致 ⇒ 生成文件被手改过，下次 merge 会把它改回去（MEMORY 第 6 条）")


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
