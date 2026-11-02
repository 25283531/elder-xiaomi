#ifndef ELDER_CLIP_H
#define ELDER_CLIP_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * 固件内置的**预录语音片段**（离线可播，不走网络、不调 TTS）。
 *
 * 为什么要有这一层
 * ---------------
 * 提醒原本的节奏是「播 4.9 秒音乐 → 起任务延时 4.9 秒 → 请服务端 TTS 念」。
 * 老人要等两段：音乐放完、TTS 首帧到达（0.8~1.2 秒），才听到第一句人话。
 * 预录片段把「音乐 + 话术」压成一条 OGG 编进固件，人声紧接音乐出现，
 * 且完全不依赖网络与 TTS 可用性。
 *
 * 与 elder_ui.h 的关系
 * -------------------
 * 本文件**只认 id**，不认 xiaozhi 的任何类型，也不直接碰音频 ——
 * 真正的播放由 main/elder_bridge.cc 通过 `elder_ui_hooks_t::play_clip`
 * 注入。这样 elder 组件仍然能脱离 xiaozhi 单独编译（firmware/ 下的
 * 主机自测工程就是这么用的）。
 *
 * ⚠️ **本文件不得 include 任何 xiaozhi 头文件**（application.h / board.h /
 *    audio_service.h 都不行）。一旦引入，elder 组件就绑死在 main 上，
 *    host 单测和 tools/syntax_check_elder.py 立刻全废。
 */

/*: 片段 id。
 *
 * ⚠️ 顺序即 elder_clip.c 里 kClipMs 的下标，**只能追加在末尾**。
 *    中间插入会让所有已有取值重新编号，而错位不会编译报错 ——
 *    表现是"服药提醒播了待办的话"，极难归因。 */
typedef enum {
    ELDER_CLIP_MEDICINE = 0,   /* 提醒音 + 「您有一条服药提醒」 */
    ELDER_CLIP_TODO,           /* 提醒音 + 「您有一条待办事项」 */
    ELDER_CLIP_CONFIRM_HINT,   /* 「请长按机身顶部凸起的橙色大按键进行服药确认」 */
    ELDER_CLIP_DONE,           /* 「已记录，今天不用再吃了」 */
    ELDER_CLIP_ESCALATED,      /* 「该吃药了，请长按按钮」 */
    ELDER_CLIP_SKIPPED,        /* 「已跳过这一条提醒」 */
    ELDER_CLIP_NO_REMINDER,    /* 「今天没有别的提醒了」 */
    ELDER_CLIP_COUNT
} elder_clip_id_t;

/**
 * @brief 播一条预录片段。
 *
 * @return true  已交给上层播放（**不表示已经播完**，播放是异步的）；
 *         false 没有 play_clip 钩子（bridge 未接线）或 id 越界。
 *
 * ⚠️ 调用方**必须**处理 false —— 返回 false 时回落原逻辑（播提示音 +
 *    TTS 播报）。不回落的话，一旦某个板子没接这个钩子，老人就再也听不到
 *    提醒内容，而屏幕和日志看起来一切正常。
 *
 * ⚠️ 本函数**不阻塞**：播放请求最终被排进主事件循环（见 elder_bridge.cc
 *    的 OnPlayClip），调用方（提醒引擎任务 / 按键任务）立刻返回。
 *
 *    为什么必须这样（这条数字是量出来的，不是估的）：`AudioService::PlaySound`
 *    是**同步**的，它逐包调 `PushPacketToDecodeQueue(packet, true)`（第二参
 *    wait=true 表示队列满就等，不丢包），而解码队列上限
 *    `MAX_DECODE_PACKETS_IN_QUEUE = 2400 / 60 = 40` 包（2.4 秒）。
 *    所以推送一段 N 帧的 OGG 会让**调用它的那个任务**占用 (N - 40) × 60ms：
 *      · 提醒音乐 82 帧 → 2.52 秒
 *      · medicine 176 帧 → **8.16 秒**
 *      · todo     177 帧 → **8.22 秒**
 *    在 elder_input 任务（按键）上占用 8 秒，会把「长按 3 秒」的判定挤歪；
 *    在主事件循环上占用，至少还能保证调用方不被自己的栈压穿。
 *    ⇒ 新增任何"播一段东西"的地方，都必须走 Schedule，不许就地 PlaySound。
 *
 *    安全性已核实（2026-11-03，值取自 build/config/sdkconfig.h）：
 *    `CONFIG_ESP_TASK_WDT_TIMEOUT_S=10`、`CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0=1`、
 *    `..._CPU1=1` —— idle 任务**是**被看门狗盯着的。但这里的 8.16 秒是
 *    `audio_queue_cv_.wait()` 的**阻塞等待**，任务进 Blocked 态会主动让出 CPU，
 *    idle 任务照常运行、照常喂狗；只有"忙等/不阻塞"才会饿死 idle。
 *    另外 `CONFIG_ESP_TASK_WDT_PANIC` 未开启 ⇒ 即使真的超时也只打警告不重启。
 */
bool elder_clip_play(elder_clip_id_t id);

/**
 * @brief 该片段的**实际时长**（毫秒，60ms 帧向上取整后的值）。
 *
 * 用于"等这段声音播完再做事"的场合。数值来自 tools/make_elder_clips.py
 * 的实测输出（帧数 × 60ms），不是估算 —— 所以改了 .ogg 就必须同步改
 * elder_clip.c 里的表，否则延时会对不上（播早了盖住人声、播晚了老人以为
 * 设备卡住）。
 *
 * @return 时长毫秒；id 越界返回 0。
 */
uint32_t elder_clip_duration_ms(elder_clip_id_t id);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_CLIP_H */
