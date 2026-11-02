#include "elder_clip.h"

#include "elder_ui.h"

#include <stddef.h>

#include "esp_log.h"

/*
 * 预录语音片段的**时长表与转发**。
 *
 * 这里刻意没有任何音频代码：真正把 OGG 交给 AudioService 的是
 * main/elder_bridge.cc 的 OnPlayClip（经 elder_ui_hooks_t::play_clip 注入）。
 * 本文件只做三件事：id 合法性检查、转发、报时长。
 *
 * 为什么不在这里直接持有资源指针：资源是链接期符号
 * （_binary_elder_medicine_ogg_start 等），只存在于 main 组件里；
 * elder 组件是独立编译单元，引用它们会导致 host 自测工程链接失败。
 */

static const char *TAG = "elder.clip";

/*: 片段时长（毫秒），**实测值**，来自 tools/make_elder_clips.py 的输出
 *  （帧数 × 60ms，向上取整到整帧）。
 *
 * ⚠️ 与 elder_clip_id_t 的顺序**严格对应**，改一个就要改另一个。
 *    重新生成 .ogg 之后必须重跑脚本、把新的时长抄回来 ——
 *    表错了不会报错，只会让"等片段播完"的地方错位（播早了盖住人声）。
 *
 * 2026-11-03 实测（7 条，24kHz/mono/60ms 帧）：
 *   medicine       176 帧 = 10560ms（音乐 4.92s + 200ms 静音 + 话术 5.4s）
 *   todo           177 帧 = 10620ms
 *   confirm_hint    76 帧 =  4560ms
 *   done            43 帧 =  2580ms
 *   escalated       42 帧 =  2520ms
 *   skipped         34 帧 =  2040ms
 *   no_reminder     32 帧 =  1920ms
 *
 * ⚠️ medicine/todo 在 2026-11-03 加长过一次：原话术只有「您有一条服药提醒」
 *    （7080ms），后来补上「请长按机身顶部凸起的橙色大按键确认」——
 *    屏幕早就写了这个动作，语音不说等于让不看屏的老人无从下手。
 *    代价是片段从 7.1s 涨到 10.6s，`PlaySound` 在主事件循环的阻塞时间
 *    也随之从 ~4.7s 涨到 ~8.2s（算式见 elder_clip.h 的说明）。
 */
static const uint32_t kClipMs[ELDER_CLIP_COUNT] = {
    10560,  /* ELDER_CLIP_MEDICINE */
    10620,  /* ELDER_CLIP_TODO */
    4560,   /* ELDER_CLIP_CONFIRM_HINT */
    2580,   /* ELDER_CLIP_DONE */
    2520,   /* ELDER_CLIP_ESCALATED */
    2040,   /* ELDER_CLIP_SKIPPED */
    1920,   /* ELDER_CLIP_NO_REMINDER */
};

/*: 日志用的名字。写死而不是用 enum 名：C 里拿不到枚举的字符串形式，
 *  而现场排查时"片段 3 播不出来"远不如"片段 done 播不出来"有用。 */
static const char *const kClipName[ELDER_CLIP_COUNT] = {
    "medicine", "todo", "confirm_hint", "done",
    "escalated", "skipped", "no_reminder",
};

static bool clip_id_ok(int id)
{
    return id >= 0 && id < (int)ELDER_CLIP_COUNT;
}

bool elder_clip_play(elder_clip_id_t id)
{
    if (!clip_id_ok((int)id)) {
        ESP_LOGE(TAG, "片段 id %d 越界（有效 0..%d）", (int)id,
                 (int)ELDER_CLIP_COUNT - 1);
        return false;
    }
    if (!elder_ui_play_clip((int)id)) {
        /* 不是错误：无屏/无音频的板子、或 bridge 还没接线。
         * 调用方据此回落「提示音 + TTS」，所以这里只打 INFO ——
         * 打成 ERROR 会让每一次提醒都刷一条假告警，掩盖真正的错误。 */
        ESP_LOGI(TAG, "片段 '%s' 无播放实现，调用方应回落 TTS",
                 kClipName[(int)id]);
        return false;
    }
    ESP_LOGI(TAG, "播放预录片段 '%s'（%lu ms）", kClipName[(int)id],
             (unsigned long)kClipMs[(int)id]);
    return true;
}

uint32_t elder_clip_duration_ms(elder_clip_id_t id)
{
    if (!clip_id_ok((int)id)) return 0;
    return kClipMs[(int)id];
}
