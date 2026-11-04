#ifndef ELDER_RGB_H
#define ELDER_RGB_H

#include <stdint.h>
#include <stdbool.h>   /* elder_rgb_button_pending(bool) 的形参类型 */
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * RGB 灯效优先级仲裁器
 *
 * 灯带共 9 颗**受控**（见 elder_board.h 的 ELDER_RGB_LEDS；物理上是 20 颗，
 * #10~#20 每帧显式写 0 保持熄灭，见 ELDER_RGB_CHAIN）：
 *   #1       按键指示灯（独立状态机，见本文件下半部分）
 *   #2 ~ #9  对话/提醒灯效（本文件上半部分）
 *
 * 两路**完全分开渲染**：提醒在 #2~#9 上闪，不会把按键反馈盖掉；
 * 反过来按键呼吸也不会污染提醒的颜色语义。优先级只作用于 #2~#9。
 */

typedef enum {
    ELDER_RGB_PRIO_IDLE      = 0,  /* 呼吸/待机 */
    ELDER_RGB_PRIO_EMOTION   = 10, /* 对话状态（可被提醒打断） */
    ELDER_RGB_PRIO_REMINDER  = 50, /* 日程提醒中 */
    ELDER_RGB_PRIO_MED_PEND  = 90, /* 服药未确认 —— 除升级外最高 */
    ELDER_RGB_PRIO_ALARM     = 100 /* 求助/告警 */
} elder_rgb_prio_t;

typedef enum {
    ELDER_EMO_NEUTRAL = 0,
    ELDER_EMO_HAPPY,
    ELDER_EMO_SAD,
    ELDER_EMO_ANGRY,
    ELDER_EMO_LISTENING,
    ELDER_EMO_SPEAKING
} elder_emotion_t;

typedef enum {
    ELDER_FX_SOLID = 0,
    ELDER_FX_BREATH,
    ELDER_FX_BLINK_FAST,
    ELDER_FX_RAINBOW,   /* 整条灯带同步缓慢流转（原待机"五彩幻彩"，2026-10-28
                         * 已被 ELDER_FX_TWINKLE 替掉，保留备用） */
    ELDER_FX_FLOW,      /* **流水（跑马灯）**：8 颗全亮，随时间沿灯带流动。
                         * 2026-11-01 起分两种颜色模式（见 rgb.c）：
                         *   · color != 0：同色行波（颜色固定，亮度做行进三角波）；
                         *   · color == 0：五彩流水（每颗一个色相、整条滚动，
                         *     服务端用 rgb="#000000" 表达"颜色随机"）。 */
    ELDER_FX_CHASE,     /* **轮流亮起**：同一时刻只有一颗亮，亮点从 #2 跑到 #9
                         * （原"网络传输处理中"，2026-10-28 已被
                         * ELDER_FX_METEOR 替掉，保留备用）。
                         * 与 FLOW 的区别：FLOW 是"都亮、颜色在流"，CHASE 是
                         * "只有一颗亮、亮点在跑"。 */
    ELDER_FX_TWINKLE,   /* **闪烁星光**（2026-10-28 用户指定，待机用）：
                         * **全局一个节拍，一次只亮一颗**：2 秒一格、每格
                         * 挑一颗灯亮 300ms 渐隐，亮起时刻在格内抖 0~1 秒
                         * ⇒ 相邻两闪间隔 1~3 秒。颜色每闪一次换一个。
                         * （2026-10-29 用户二次调整，原为 4 秒一格 = 3~5 秒）
                         *
                         * 第一版是"每颗灯各自 1.2~3.0s 的独立周期"，8 颗
                         * 叠加起来每秒闪 3~4 次，用户实报"频率太快、像
                         * 每秒都有多颗在闪"。星光要的是稀疏，不是热闹。 */
    ELDER_FX_METEOR     /* **流星**（2026-10-28 用户指定，网络传输中用）：
                         * 一个亮头 + 拖尾沿灯带跑过去，头部最亮、尾部线性
                         * 衰减。每颗流星换一个色相（流星本身必须是**一个**
                         * 颜色，不能是彩虹），连续几颗换色 ⇒ 整体仍是五彩。 */
} elder_rgb_fx_t;

/* ---------------------------------------------------------------- 对话四态
 *
 * 2026-10-28 用户最终定义（两轮，覆盖 10-27 的版本，见 docs/决策记录.md
 * D66 / D67）：
 *
 *   待机（进入后 0~1 分钟）  闪烁星光 Twinkle 60%  随机彩色，偶尔闪一下
 *   待机（超过 1 分钟）      闪烁星光 Twinkle 30%  同上，自动降亮
 *   唤醒后收听               #00BFFF 深天蓝 55%   常亮
 *   TTS 输出                 #FF8C00 深橙 70%     常亮
 *   网络传输处理中           流星 Meteor 20%      亮头 + 拖尾跑过灯带
 *
 * 四个状态**互斥**，共用 ELDER_RGB_PRIO_EMOTION 一个槽位 —— 切换即替换，
 * 不会留下过期槽位把新状态顶掉。
 *
 * ⚠️ 这里的"百分比"是**感知亮度**（人眼看着几成亮），不是写进灯珠的线性值。
 *    中间隔着一张 gamma 2.2 查表（rgb.c 的 GAMMA_LUT）。加表之前
 *    scale_color() 是纯线性乘法，写 30% 人眼看着约 58% —— 这正是
 *    "WS2812B 好像控制不了亮度"的真正原因。
 *
 * 为什么亮度要单独传、而不是把颜色预先调暗：
 *   星光和流星都是**动态**算出来的（星光会衰减、流星尾部一直在变），
 *   颜色值预先乘 0.3 之后再叠加动态亮度，峰值就变成 0.3×255×动态值，
 *   实测峰值只有约 9% —— 看起来像灯坏了。所以亮度必须是"算完之后统一乘"。
 */
typedef enum {
    ELDER_RGB_UI_IDLE = 0,   /* 待机：闪烁星光（TWINKLE），60% → 1 分钟后 30% */
    ELDER_RGB_UI_LISTENING,  /* 唤醒后收听：深天蓝 #00BFFF，55%，常亮 */
                             /* （原 #0000FF/100%，2026-10-27 用户实报刺眼） */
    ELDER_RGB_UI_CONNECTING, /* 数据上传/等回复：流星（METEOR），20% */
    ELDER_RGB_UI_SPEAKING    /* TTS 输出：深橙 #FF8C00，70%，常亮 */
} elder_rgb_ui_t;

/**
 * @brief 初始化 RMT + WS2812 灯带，并启动灯效任务
 */
esp_err_t elder_rgb_init(void);

/**
 * @brief 请求某种灯效。优先级低的请求不会抢占高的。
 * @param prio   优先级，见 elder_rgb_prio_t
 * @param fx     灯效
 * @param color  0xRRGGBB
 * @param ttl_ms 持续时间；0 表示常驻直到被释放
 */
esp_err_t elder_rgb_request(elder_rgb_prio_t prio, elder_rgb_fx_t fx,
                            uint32_t color, uint32_t ttl_ms);

/**
 * @brief 同上，但可以指定亮度等级。
 * @param level 0~255，含义是**感知亮度**（人眼看着几成亮），255 = 100%。
 *              **不是** 0~100，别按百分数传。
 *
 *              ⚠️ 这里传的不是写进灯珠的线性值：内部会过一张 gamma 2.2
 *              查表（rgb.c 的 GAMMA_LUT）。所以"30%"真的是三成亮，
 *              而不是线性乘法时代的"看着像 58%"。低于约 8% 感知亮度
 *              会落到表里的 0 —— WS2812B 低端不可靠，这是硬件下限。
 */
esp_err_t elder_rgb_request_ex(elder_rgb_prio_t prio, elder_rgb_fx_t fx,
                               uint32_t color, uint8_t level, uint32_t ttl_ms);

/**
 * @brief 释放之前申请的灯效，回落到次高优先级的请求
 */
void elder_rgb_release(elder_rgb_prio_t prio);

/**
 * @brief 设置对话四态灯效（推荐入口，语义见 elder_rgb_ui_t）
 */
void elder_rgb_set_ui_state(elder_rgb_ui_t st);

/**
 * @brief 设置对话情绪（内部映射到 ELDER_RGB_PRIO_EMOTION）
 *
 * NEUTRAL/LISTENING/SPEAKING 三个会转成对应的 UI 四态；
 * HAPPY/SAD/ANGRY 走"状态灯效表"里的同名项。
 */
void elder_rgb_set_emotion(elder_emotion_t emo);

/* ---------------------------------------------------------- 灯效配置表
 *
 * 2026-10-31：灯效从"固件里写死"改成"服务端下发"。
 *
 * 为什么改：用户要「每种状态下拉选灯效、可设亮度、星光可设闪烁间隔」。
 * 写死在固件里的话，改一次就要重刷固件 —— 而本仓固件一律走 CI 编译，
 * 一次改动的代价是十几分钟加一次刷机，根本没法当配置项用。
 *
 * 谁是真源：服务端 `server/app/avatar_style.py` 的 LIGHT_PRESETS。
 * 设备只认下面 5 个 effect 字符串，不认预置 id —— 预置是"给人挑的"，
 * 下发时已经展开成 rgb/effect/亮度/间隔，设备不需要知道它叫什么名字。
 */
typedef enum {
    ELDER_RGB_ST_NEUTRAL = 0,   /* 待机 */
    ELDER_RGB_ST_LISTENING,     /* 聆听中 */
    ELDER_RGB_ST_SPEAKING,      /* 说话中 */
    ELDER_RGB_ST_HAPPY,         /* 开心 */
    ELDER_RGB_ST_SAD,           /* 难过 */
    ELDER_RGB_ST_ANGRY,         /* 生气 */
    ELDER_RGB_ST_ALERT,         /* 提醒警示（待办 / 用药） */
    ELDER_RGB_ST_COUNT
} elder_rgb_state_t;

/** effect 取值：与 elder_rgb_fx_t 的编号对齐，另加一个"关闭" */
#define ELDER_RGB_EFFECT_OFF 0xFF

typedef struct {
    uint8_t  effect;      /* elder_rgb_fx_t 或 ELDER_RGB_EFFECT_OFF */
    uint32_t color;       /* 0xRRGGBB；0 = 灯效自定色（星光随机色） */
    uint8_t  level;       /* 0~255 感知亮度，见 elder_rgb_request_ex */
    uint16_t period_ms;   /* 呼吸/闪烁/星光的周期；0 = 用内置默认 */
} elder_rgb_cfg_item_t;

/**
 * @brief 应用服务端下发的灯效表。**整表替换**，不合并。
 *
 * @param items 长度必须是 ELDER_RGB_ST_COUNT；传 NULL 表示整表回落内置默认。
 *
 * 为什么整表替换而不是逐项覆盖：合并语义下"服务端把某个状态删了"就永远
 * 回不到默认值，而用户看到的界面是"7 个状态都在"，两边对不上。
 *
 * 线程安全：内部加锁。**会自动重放当前状态**，所以心跳到达后不需要上层
 * 再调一次 set_ui_state —— 否则新配置要等下一次状态切换才生效，
 * 用户会以为"保存了没用"。
 */
void elder_rgb_apply_config(const elder_rgb_cfg_item_t *items);

/**
 * @brief 取一份**内置默认**灯效表（长度 ELDER_RGB_ST_COUNT）。
 *
 * 用途只有一个：服务端下发的 JSON 若缺了某个状态，固件要从"默认"而不是
 * 从"全零"开始 —— 全零意味着 effect=SOLID、color=黑、level=0，表现是
 * 那个状态下灯彻底不亮，而服务端界面上它明明是有配置的。
 */
void elder_rgb_get_default_config(elder_rgb_cfg_item_t *out);

/* 需求 11 的语义封装 */
void elder_rgb_med_pending(void);   /* 长亮红 */
void elder_rgb_med_done(void);      /* 长亮绿（默认 60s 后回落） */
void elder_rgb_med_escalate(void);  /* 快速闪烁红 */

/* ---------------------------------------------- 状态灯总开关（2026-11-01）
 *
 * 用户原话：「关闭状态灯（严禁关闭事件灯）」。
 *
 * **状态灯** = 待机呼吸/星光、唤醒聆听、TTS 说话、开心/难过/生气表情。
 * **事件灯** = 提醒闪灯、服药未确认、求助告警、#1 按键指示灯。
 *
 * 关掉状态灯之后，设备平时完全不发光（夜里不刺眼），但该吃药、该做的事
 * 照样会亮 —— 这是这个功能存在的全部意义。判据在 rgb.c 里落在**优先级**上
 * （``ELDER_RGB_PRIO_REMINDER`` 及以上一律不受开关影响），而不是另立一张
 * "哪些算事件灯"的名单：名单会漂移，优先级不会。
 *
 * 开关值落 NVS，重启后保持 —— 老人晚上关了灯，半夜 OTA 重启又亮起来，
 * 他会以为设备不听话。
 */

/**
 * @brief 开/关状态灯。事件灯不受影响。
 */
void elder_rgb_set_status_light(bool on);

/**
 * @brief 当前状态灯是否开着。默认 true。
 */
bool elder_rgb_status_light_enabled(void);

/* ------------------------------------------------- 需求 8：提醒的闪灯节奏
 *
 * 用户 2026-10-22 原话：「定时任务提醒时，应该红灯闪烁（闪烁10秒，
 * 10分钟后再闪烁10秒，2小时内以此重复。2小时后不再亮灯。）」
 *
 * 为什么节奏放在**灯效模块内部**而不是让上层每 10 分钟调一次 request：
 *   1. 上层要自己做这件事就必须再起一个任务或定时器 —— 本仓已经因为
 *      "回调跑在 esp_timer 任务里、栈只有 3.5KB" 出过随机 LoadProhibited
 *      （见 elder_bridge.cc 的 ScheduleAnnounce 注释），能不加任务就不加；
 *   2. rgb_task 本来就每 30ms 醒一次，判断"现在是不是该闪"是零成本的；
 *   3. 节奏只有一份真源。上层若另算一遍，"2 小时后关闭"就有两个判据，
 *      迟早分叉成"灯还闪、界面已经没了"。
 *
 * 语义：
 *   * nag_start() 从**调用时刻**起算 2 小时，每 10 分钟的前 10 秒红灯闪；
 *   * 2 小时到点后灯自然熄灭（不需要上层记得来关）；
 *   * 期间若老人确认完成（med_done）或出现更高优先级的灯效
 *     （ELDER_RGB_PRIO_MED_PEND 及以上，例如求助告警），nag 让位；
 *   * nag_stop() 立即停（确认完成时调）。
 */
void elder_rgb_reminder_nag_start(void);
void elder_rgb_reminder_nag_stop(void);

/* ---------------------------------------------- 第 1 颗灯：按键指示灯
 *
 * 2026-10-27 用户原话（历史引文，保留以对照）：
 *
 *   「第一颗LED一般情况下不亮，只有需要按键时才亮。当有待确认的日程提醒
 *     （服药）时，为深橙色 #FF8C00 高亮（100%）呼吸灯效。当用户按下确认
 *     按键时，从浅绿色 #90EE90 向深绿色 #006400 转变，转变时长为按钮定义
 *     的需要按键时长。确认完成时以深蓝绿色 #00CED1 闪烁三次结束。如果按键
 *     中途松开，没有完成确认，则以深粉红色 #FF1493 闪烁5次结束。」
 *
 * 2026-11-04 用户重新定义颜色与长按灯效（**以本段为准**）：
 *
 *   「长按的事件指示灯光效变一下，变成天青色由暗变亮的过程，3 秒钟的时候
 *     直接切换为天蓝色全亮闪烁 3 次，失败的话是粉红色全亮闪烁 5 次。」
 *
 * 四个状态（互斥）：
 *   ① 熄灭      —— 没有待确认提醒，也没在按。**这是常态**，夜里不发光。
 *   ② 橙色呼吸  —— 有待确认提醒。作用是"吸引注意"：老人听到提醒后要知道
 *                  该按哪个键，灯亮着的那颗就是。
 *   ③ 天青变亮  —— 按住期间，天青 #00E5FF **同一色相由暗变亮**（起点是
 *                  ELDER_RGB_BTN_GRAD_MIN_LEVEL，不从全黑起，按下瞬间就有
 *                  反馈）。渐变本身是**进度条**：老人能看出"还差多少"，
 *                  比一个突然亮起的"完成"更不容易半途松手。
 *   ④ 结果闪烁  —— 天蓝 #00BFFF 全亮闪 3 次 = 3 秒达标（**含追问路径**，
 *                  只要按满 3 秒就是成功回执）；粉红 #FF69B4 全亮闪 5 次 =
 *                  中途松开没成。
 *
 * 为什么"没待确认提醒时按下什么都不亮"（而不是给个"按到了"的反馈）：
 *   本按键只做确认。没有可确认的东西时给正反馈，等于告诉老人"办成了"，
 *   而实际什么都没发生 —— 这是最坏的一类欺骗。安静地不亮才是诚实的。
 *
 * ⚠️ 渐变时长必须与 input.c 的长按判定**同源**：两边都用
 *    elder_board.h 的 ELDER_BTN_CONFIRM_HOLD_MS。各写一份的话，只要有人
 *    改了一处，就会出现"灯已经亮满天青、按键却还没算确认"（或反过来）
 *    —— 灯在骗人，比没有灯更糟。也正因如此，渐变必须用**墙上时钟**算，
 *    不能用闪烁那套渲染时钟。
 */

/** 按下（不管当时有没有可确认的提醒；没有就保持熄灭） */
void elder_rgb_button_press(void);

/** 松开。仍在渐变中 ⇒ 判定为"中途松开没确认"，粉红闪 5 次。
 *  已经出过结果（confirmed 或已闪烁）⇒ 忽略，不会重复反馈。
 *
 *  ⚠️ 2026-11-04 起还有一道**抖动兜底**：本轮按压总时长
 *  < ELDER_RGB_BTN_DEBOUNCE_MIN_MS(300ms) 时**不闪粉红**，直接回
 *  "橙色呼吸/熄灭"并把本轮标成已出结果。
 *
 *  为什么需要它：用户实报「按键超过 1 秒就开始进行粉红色闪烁」—— 老人按住
 *  不动，机械按键中途抖几十毫秒就被当成松手，渐变被掐掉、灯报"你没按够"。
 *  input.c 已把松手消抖单独加到 80ms（RELEASE_DEBOUNCE_MS），这里是第二道
 *  防线：300ms 以内不可能有人主动放弃，那一段里的"松开"只可能是抖动。
 *
 *  这**不是**放宽标准：3 秒才算确认的判据（ELDER_BTN_CONFIRM_HOLD_MS）一个字
 *  都没动，300ms 之后的松手照样粉红闪 5 次。 */
void elder_rgb_button_release(void);

/** 确认完成 ⇒ 天蓝 #00BFFF 全亮闪 3 次 */
void elder_rgb_button_confirmed(void);

/** 按压**已受理** ⇒ 与 confirmed() 完全一致：天蓝 #00BFFF 全亮闪 3 次。
 *
 *  为什么必须有这个入口（2026-11-03 用户实报「长按服药键一直是错误状态，
 *  闪烁错误事件灯，但我的操作是正确的长按 3 秒以上」）：
 *    多药/单味药提醒的长按不再直接确认，而是先问一句「您吃的是哪种药？」
 *    再进对话（见 elder_bridge.cc 的 ELDER_KEY_EV_LONG_HOLD）。那一刻老人
 *    确实按够了 3 秒 —— 用户 2026-11-04 的要求是「3 秒钟的时候直接切换为
 *    天蓝色全亮闪烁 3 次」，追问路径同样达标，所以必须闪天蓝，不能退回
 *    橙色呼吸让老人看不到任何回执。
 *    它同时负责把本轮标成"已出结果"（btn_enter_flash 置 btn_resolved），
 *    松开时不会再补一次粉红"失败"闪 —— 那正是 11-03 实报的错误灯。 */
void elder_rgb_button_acknowledged(void);

/** 是否存在待确认的提醒 ⇒ #1 橙色呼吸 / 熄灭。
 *  由 reminder.c 在提醒状态集合变化时调用，是"吸引注意"态的唯一真源。 */
void elder_rgb_button_pending(bool pending);

#ifdef __cplusplus
}
#endif

#endif /* ELDER_RGB_H */
