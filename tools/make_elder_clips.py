"""生成固件内置的**老人常用语音片段**（一次生成、永久入库，运行时不再合成）。

产物
----
``firmware/xiaozhi/main/assets/common/elder_<用途>.ogg`` 共 7 条。

它们由 ``firmware/xiaozhi/main/CMakeLists.txt`` 的
``file(GLOB COMMON_SOUNDS .../assets/common/*.ogg)`` 自动收进 ``EMBED_FILES``，
链接期变成 ``_binary_elder_<用途>_ogg_start`` / ``_end`` 两个符号。
固件侧 ``elder_bridge.cc::OnPlayClip`` 直接 ``app.PlaySound(...)``。

为什么要把「提醒音 + 话术」拼成**一条** OGG
-------------------------------------------
现状是「先播 4.9 秒纯音乐 → 起一个任务延时 4.9 秒 → 再请服务端 TTS 念」。
老人要听满 4.9 秒音乐、再等 TTS 首帧（0.8~1.2 秒），才听到第一句人话。
预录成一条之后：音乐结束**紧接着**就是人声，且整条不依赖网络与 TTS 时延。
这是本方案的全部收益所在 —— 不是为了省那点流量。

为什么必须 24kHz / mono / 每帧 60ms
-----------------------------------
``AudioService::PlaySound()`` 里 ``frame_duration`` 是**硬编码 60**（毫秒），
且采样率一旦与当前解码器不同就会走 ``SetDecodeSampleRate()`` **重建解码器**
（``audio_service.cc`` 的 398-411 行），每次播放都有重建开销和潜在爆音。
所以采样率、声道、帧长三者都不是"可调风格"，是接口契约：
  * 24kHz —— 与 ``server/app/voice/asr_tts.TTS_SAMPLE_RATE`` 一致；
  * mono  —— 板子只有一路 ES8311；
  * 60ms  —— 1440 样本 / 2880 字节 PCM，与线上语音帧长同口径。
（``popup.ogg`` 是 16kHz，**不要照抄它的采样率** —— 它每次播放都在重建解码器。）

为什么本脚本在 **Docker 容器里**跑
----------------------------------
两个原因，都绕不过去：
1. Opus 编码需要 libopus，本机没装，生产镜像里有；
2. 语音必须用**服务端自己的 TTS**合成，才能保证音色与线上逐字一致 ——
   换第三方 TTS 就等于让老人听到两个不同的声音，比不预录更糟。
而 TTS 的密钥/配置行存在生产的 ``/data/elder.db`` 里，只能在那儿取。

    docker cp tools/make_elder_clips.py tools/make_reminder_chime.py elder-server:/tmp/
    docker exec elder-server python /tmp/make_elder_clips.py --outdir /tmp/clips
    docker cp elder-server:/tmp/clips/. <仓库>/firmware/xiaozhi/main/assets/common/

⚠️ 只做 ``docker cp`` / ``docker exec``，**绝不 restart / rebuild 容器** ——
重建会丢掉全部历史日志，那是审计依据。

音效部分
--------
直接 ``import make_reminder_chime`` 复用它的 ``synth_pcm()``，**不重写旋律**。
理由：那段五声音阶铃声是 2026-10-22 用户反馈「太刺耳、太突然」后四处调过的，
任何"再改一版"都是把已验收的听感推倒重来。同一份代码生成，逐字节相同。
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

#: 编码采样率。**不要改**，理由见模块头（PlaySound 的 frame_duration 硬编码 60）。
SAMPLE_RATE = 24000

#: 每帧毫秒数。同上，硬契约。
FRAME_MS = 60

#: 音乐与人声之间留的静音。
#:
#: 为什么需要：``synth_pcm()`` 的最后一个音（C4，1.25s）是指数衰减，尾巴一直
#: 拖到缓冲区末尾。人声直接贴上去，第一个字的辅音会被余音掩掉 —— 老人本来
#: 就听不清辅音，"您有一条服药提醒"会变成"您有一条__药提醒"。
#: 200ms 足够让衰减落到听不见，又不至于让老人以为设备卡住了。
GAP_MS = 200

#: 每条片段的定义。
#:
#: ``with_chime`` 为真的才是「提醒音 + 话术」：这两条对应"到点第一次响铃"，
#: 老人需要先被声音叫过来，再听清是什么事。其余五条是**应答**（确认/跳过/
#: 查询结果），此时老人已经站在设备前面了，再放 4.9 秒音乐纯属让他多等。
#:
#: ⚠️ ``wired`` 标记这条片段**当前有没有调用点**。没有调用点的不要删 ——
#:    文件留着、编进固件（多占几百字节），等有场景时直接接；但**也不要为了
#:    消灭这个标记就去硬接一个调用点**，那等于凭空新增语音行为。
#:    标记与 `firmware/components/elder/src/elder_bridge.cc` 里的实际调用点
#:    必须一致，改一边就要改另一边。
#:
#: 2026-11-03 的接线范围（当天两次需求变更后的**实际**状态）：
#:   MEDICINE / TODO  → OnReminder(PENDING) 的**离线回落**。
#:                      在线已改走 TTS：时段话术「您今天上午…」带变量，
#:                      预录片段零参数化能力，做不到（见 elder_bridge.cc）。
#:   CONFIRM_HINT     → OnReminder(ESCALATED) 且 type==服药，带 tone 回落
#:   ESCALATED        → OnReminder(ESCALATED) 且非服药，带 tone 回落
#:   DONE             → **暂不接**（排队问题，见下面 DONE 那条的注释）
#:   SKIPPED / NO_REMINDER → **没有对应语音场景**
CLIPS: tuple[dict, ...] = (
    # 2026-11-04 第三版：**去掉行动指令**，与在线话术对齐。
    #
    # 在线（TTS）话术是「您今天{上午|中午|下午|晚上}有{服药提醒|待办事项}，
    # 请您知悉」（elder_bridge.cc 的 ScheduleAnnounce 分支）—— 里面**没有**
    # 「请长按…确认」。而预录片段此前是「您有一条服药提醒，请长按机身顶部
    # 凸起的橙色大按键确认」，于是同一条提醒在断网时催老人按键、联网时不催，
    # 老人听到的话取决于当时有没有 Wi-Fi。这是必须消灭的不一致。
    #
    # 行动指令并没有丢：它现在只由 CONFIRM_HINT 在**升级态**（ESCALATED）播。
    # 那也是更该说的时机 —— 刚响第一声就催一遍属于聒噪，见 CONFIRM_HINT 的注释。
    #
    # 为什么片段里念不出时段：PlaySound 只吃固定资源名，零参数化能力，
    # 所以只能取在线话术里**不含变量**的那半句。
    #
    # 顺带把时长从 10.6s 降回 ~7s：PlaySound 阻塞主事件循环的时间同比缩短
    # （见 elder_clip.c 的 kClipMs 注释），老人按键到听见「已记录」的延迟也变短。
    {"id": "MEDICINE", "file": "elder_medicine.ogg", "chime": True, "wired": True,
     "text": "您今天有服药提醒，请您知悉"},
    {"id": "TODO", "file": "elder_todo.ogg", "chime": True, "wired": True,
     "text": "您今天有待办事项，请您知悉"},
    # CONFIRM_HINT：2026-11-03 起**有调用点** —— OnReminder(ESCALATED) 的
    # 服药分支（elder_bridge.cc 的 ELDER_RMD_ESCALATED）。
    #
    # 需求原话：「『请长按机身顶部凸起的橙色大按键确认』这句提醒音只在服药
    # 日程生效但是未确认的提醒升级状态主动播放。」PENDING 不播它（老人刚听到
    # 提醒，再催一遍只是聒噪），非服药也不播（话里明说"服药确认"）。
    #
    # 为什么只播它、不和 ESCALATED 连播：两条 2.52 + 4.56 ≈ 7.1 秒，而老人
    # 已经等了 15 分钟；这句话正是此刻唯一有用的信息（按键位置 + 动作），
    # escalated 的「该吃药了，请长按按钮」与它语义重叠，先播只会把关键指令
    # 推后 2.5 秒。判断依据同步写在 elder_bridge.cc 的分支注释里。
    #
    # 历史备注（保留，说明它当初为什么空着）：本片段原打算做"二次催促"，
    # 但本工程没有那条路径 —— reminder.c 的「每 10 分钟闪 10 秒、持续 2 小时」
    # 是 rgb.c 自己的计时器，只闪灯、不重新进 OnReminder(PENDING)；
    # CONFIRM_WIN 在正式固件里不可达。现在接在升级态，语义更准。
    {"id": "CONFIRM_HINT", "file": "elder_confirm_hint.ogg", "chime": False,
     "wired": True,
     "text": "请长按机身顶部凸起的橙色大按键进行服药确认"},
    # DONE：**暂不接**（2026-11-03 team-lead 定的条件式决策）。
    #
    # 原因不是"这句没用"，而是**排队**：PlaySound 把包推进解码队列，队列满就
    # 阻塞等待，所以任何后到的声音都必须等前面整段播完。老人按确认时
    # MEDICINE 片段往往还在播（10.5 秒），此刻 DONE 无论播片段还是播原来的
    # 0.5 秒 tone，**都只能等片段播完才开始** —— 也就是说"按了键 9 秒后才听到
    # 已记录"。team-lead 的口径是"如果确实会排很久，就先不接 DONE"。
    #
    # ⚠️ 注意这个延迟**不是接 DONE 造成的**：不接也一样，只是那时老人听到的
    #    是 0.5 秒的 tone 而不是这句"已记录"。要真正缩短延迟，只有两条路：
    #    ① 缩短 MEDICINE/TODO 的话术（每减 1 个汉字约省 0.21 秒）；
    #    ② 动音频通路加"打断"（红线，禁止）。
    #    按下的即时反馈目前由 #1 指示灯（闪深蓝绿 3 次）+ 屏幕切确认态提供。
    {"id": "DONE", "file": "elder_done.ogg", "chime": False, "wired": False,
     "text": "已记录，今天不用再吃了"},
    {"id": "ESCALATED", "file": "elder_escalated.ogg", "chime": False, "wired": True,
     "text": "该吃药了，请长按按钮"},
    {"id": "SKIPPED", "file": "elder_skipped.ogg", "chime": False, "wired": False,
     "text": "已跳过这一条提醒"},
    {"id": "NO_REMINDER", "file": "elder_no_reminder.ogg", "chime": False,
     "wired": False,
     "text": "今天没有别的提醒了"},
)

#: TTS 失败重试次数（**不含**首次）。
#:
#: 服务端 TTS 厂商偶发 500（腾讯云/Edge 都遇到过）。一次失败就重跑整个脚本
#: 代价太大（音乐合成 + 7 条合成 + 上传下载），而连续三次都失败基本可以
#: 断定不是抖动而是配置/密钥问题 —— 那时候就该报错让人看，而不是继续重试。
TTS_RETRIES = 2


def _silence(ms: int) -> bytes:
    """一段 S16LE 静音。"""
    return b"\x00" * (SAMPLE_RATE * ms // 1000 * 2)


def _pcm_stats(pcm: bytes) -> tuple[int, float]:
    """(峰值, RMS)，都归一化到 0.0-1.0，仅用于打印，不参与处理。"""
    n = len(pcm) // 2
    if n <= 0:
        return 0, 0.0
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    peak = max(abs(s) for s in samples)
    total = 0
    for s in samples:
        total += s * s
    return peak, (total / n) ** 0.5 / 32768.0


def _import_chime():
    """导入同目录的 make_reminder_chime（本机在 tools/，容器里在 /tmp/）。

    ⚠️ 容器里**必须**把两个脚本一起 cp 进去：本模块依赖它的旋律与 OGG
    封装实现，缺了就是 ImportError。这不是可以"顺手内联一份"的东西 ——
    两处各写一遍 OGG 页写入，CRC 算法写错一个字节就是"invalid page"、
    整段不响，而且这种错**不会在编码阶段暴露**。
    """
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import make_reminder_chime  # noqa: PLC0415  必须在 sys.path 之后
    return make_reminder_chime


def synth_speech(text: str) -> bytes:
    """用**服务端自己的 TTS**合成一句话 → 24kHz/mono/S16LE PCM。

    走 ``asr_tts.tts(db, ...)`` 而不是直接调某个厂商适配层：那个函数内部
    按生产的 ``ai_providers`` 配置行（provider / api_style / tts_voice）
    分发，是线上语音的**唯一真源**。绕开它自己拼请求，音色就会与线上漂移，
    而且以后运维换音色时这里不会跟着变 —— 老人会听到两个声音。

    ``resolve_row`` 缺省取 priority 最小的那条 tts 配置，与设备播报时一致。
    """
    from app.db import SessionLocal          # noqa: PLC0415
    from app.voice import asr_tts            # noqa: PLC0415

    last: Exception | None = None
    for attempt in range(TTS_RETRIES + 1):
        db = SessionLocal()
        try:
            pcm = asr_tts.tts(db, text)
            if not pcm:
                raise RuntimeError("TTS 返回空音频")
            if len(pcm) % 2:
                pcm = pcm[:-1]               # 16bit 采样按 2 字节对齐
            return pcm
        except Exception as exc:             # noqa: BLE001  厂商 500 是常态
            last = exc
            if attempt < TTS_RETRIES:
                wait = 2.0 * (attempt + 1)
                print(f"      TTS 失败（第 {attempt + 1} 次）：{exc}"
                      f" —— {wait:.0f}s 后重试", flush=True)
                time.sleep(wait)
        finally:
            db.close()
    raise RuntimeError(f"TTS 连续 {TTS_RETRIES + 1} 次失败：{last}") from last


def compose(clip: dict, chime_pcm: bytes) -> bytes:
    """拼出这一条片段的完整 PCM（音乐 + 静音 + 人声）。

    **不做电平归一化**：音乐那 0.55 峰值是用户 2026-10-22 反馈"太刺耳"之后
    调出来的，人声就是老人今天在线上听到的音量。两者按原样拼接，等于
    复现"先响音乐、再念话"的现状 —— 任何自动增益都会改变已验收的听感，
    而且没有证据说改了更好。这里只**报告**各自的峰值/RMS，异常时人能看见。
    """
    speech = synth_speech(clip["text"])
    parts = []
    if clip["chime"]:
        parts.append(chime_pcm)
        parts.append(_silence(GAP_MS))
    parts.append(speech)
    pcm = b"".join(parts)

    # 补齐到 60ms 整数帧：Opus 按帧编码，尾部不足一帧会被 encode_opus 补零，
    # 这里显式补一次是为了让"帧数 × 60ms"和实际时长严格对应 ——
    # 固件里 elder_clip_duration_ms() 返回的就是这个值。
    frame_bytes = SAMPLE_RATE * FRAME_MS // 1000 * 2
    if len(pcm) % frame_bytes:
        pcm += b"\x00" * (frame_bytes - len(pcm) % frame_bytes)
    return pcm


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成固件内置的老人常用语音片段")
    ap.add_argument("--outdir", default=str(
        Path(__file__).resolve().parent.parent
        / "firmware/xiaozhi/main/assets/common"))
    ap.add_argument("--only", default="",
                    help="只生成指定 id（逗号分隔），调试用")
    args = ap.parse_args(argv)

    chime = _import_chime()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    only = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    targets = [c for c in CLIPS if not only or c["id"] in only]

    # 音乐只合成一次（两条带音乐的片段共用同一段 PCM，保证听感一致）
    chime_pcm = b""
    if any(c["chime"] for c in targets):
        print("合成提醒音乐（复用 make_reminder_chime 的旋律）...", flush=True)
        chime_pcm = chime.synth_pcm()
        cp, cr = _pcm_stats(chime_pcm)
        print(f"  音乐 PCM {len(chime_pcm)} 字节 / "
              f"{len(chime_pcm) / 2 / SAMPLE_RATE:.2f}s / "
              f"peak={cp} rms={cr:.4f}", flush=True)

    table: list[dict] = []
    failed: list[str] = []
    for clip in targets:
        print(f"[{clip['id']}] {clip['text']}", flush=True)
        try:
            pcm = compose(clip, chime_pcm)
        except Exception as exc:              # noqa: BLE001  如实报告，不占位
            print(f"  ✗ 失败：{exc}", flush=True)
            failed.append(f"{clip['id']}（{exc}）")
            continue

        packets = chime.encode_packets(pcm)
        ogg = chime.mux_ogg(packets, vendor=b"voice-avatar-box elder clip")
        path = outdir / clip["file"]
        path.write_bytes(ogg)

        # 时长以**帧数**为准，不是 len(pcm)：固件按 60ms 一帧解码，
        # 播放结束的时刻就是 frames*60ms，用它做延时才对得上。
        ms = len(packets) * FRAME_MS
        peak, rms = _pcm_stats(pcm)
        print(f"  → {path.name}  {len(ogg)} 字节 / {len(packets)} 包 / "
              f"{ms} ms / peak={peak} rms={rms:.4f}", flush=True)
        table.append({"id": clip["id"], "file": clip["file"],
                      "bytes": len(ogg), "frames": len(packets), "ms": ms,
                      "peak": peak, "rms": round(rms, 4),
                      "wired": clip["wired"], "text": clip["text"]})

    print("\n==== 时长表（写进 elder_clip.c 的 kClipMs）====")
    print(json.dumps(table, ensure_ascii=False, indent=2))

    if failed:
        # ⚠️ **绝不用静音/空文件占位**：一条静音片段会让老人以为设备坏了，
        # 而固件那边看起来"播放成功"，日志里一片正常 —— 这是最难查的假成功。
        print("\n以下片段未生成（请修好 TTS 后重跑）：", file=sys.stderr)
        for f in failed:
            print("  - " + f, file=sys.stderr)
        return 1
    print(f"\n全部 {len(table)} 条生成完毕 → {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
