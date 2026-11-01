#ifndef LCD_DISPLAY_H
#define LCD_DISPLAY_H

#include "lvgl_display.h"
#include "gif/lvgl_gif.h"

#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <font_emoji.h>

#include <atomic>
#include <memory>
#include <string>

#define PREVIEW_IMAGE_DURATION_MS 5000

/* 防烧屏轮播（2026-10-20 用户要求：屏幕常亮固定一张图，担心烧屏）。
 *
 * 做法：空闲时每隔一段时间把表情图换一张。为什么是"轮换表情"而不是
 * "整屏平移"：这是 320x240 IPS，真正会留下残影的是**长期不动的高对比
 * 区域**（中央的表情图正是这种）；整屏平移一两个像素在 LVGL 里要动
 * 所有对象、每帧重排，代价远大于收益，而且用户看不懂"界面在抖"。
 *
 * 两条判据，缺一不可：
 *   ① 「距上次 UI 变化多久」—— "界面 60 秒没变过"是烧屏风险的定义本身；
 *   ② 「设备是否处于待机态」—— 屏保播的是**待机指示灯**（动态熊猫），
 *      非待机态播它等于对老人撒谎（见 AntiBurnInStep 里的 2026-11-01 事故注释）。
 *
 * 早期只写了 ①，理由是「设备状态要问 Application，显示层不该反向依赖它」。
 * 那个理由站不住：判据不取 Application 的私有状态，走的是 device_state_event
 * 的广播（Application 主动 PostStateChangeEvent），方向仍是 Application → 显示层，
 * 没有反向依赖。代价是只靠 ① 时，老人沉默停在聆听态 60 秒就会把待机熊猫播上去。
 */
#define ANTI_BURN_CHECK_MS  20000   /* 每 20s 检查一次 */
#define ANTI_BURN_IDLE_MS   60000   /* 界面 60s 无变化才轮换 */


class LcdDisplay : public LvglDisplay {
protected:
    esp_lcd_panel_io_handle_t panel_io_ = nullptr;
    esp_lcd_panel_handle_t panel_ = nullptr;
    
    lv_draw_buf_t draw_buf_;
    lv_obj_t* top_bar_ = nullptr;
    lv_obj_t* status_bar_ = nullptr;
    lv_obj_t* content_ = nullptr;
    lv_obj_t* container_ = nullptr;
    lv_obj_t* side_bar_ = nullptr;
    lv_obj_t* bottom_bar_ = nullptr;
    lv_obj_t* preview_image_ = nullptr;
    lv_obj_t* emoji_label_ = nullptr;
    lv_obj_t* emoji_image_ = nullptr;
    std::unique_ptr<LvglGif> gif_controller_ = nullptr;
    lv_obj_t* emoji_box_ = nullptr;
    lv_obj_t* chat_message_label_ = nullptr;
    esp_timer_handle_t preview_timer_ = nullptr;
    std::unique_ptr<LvglImage> preview_image_cached_ = nullptr;
    bool hide_subtitle_ = false;  // Control whether to hide chat messages/subtitles

    void InitializeLcdThemes();
    void SetupUI();
    virtual bool Lock(int timeout_ms = 0) override;
    virtual void Unlock() override;

    /* 空闲屏保：在 SetupUI 末尾启动，静止 60s 后循环播放熊猫动图，
     * 一有语境（SetEmotion / SetChatMessage）就停掉、回到静态表情图标。 */
    void StartAntiBurnInTimer();
    void AntiBurnInStep();
    lv_timer_t* anti_burn_timer_ = nullptr;
    uint32_t last_ui_change_tick_ = 0;      /* lv_tick 毫秒 */
    /* 设备当前是否处于待机态（kDeviceStateIdle）。防烧屏屏保的唯一开关。
     *
     * 谁写：StartAntiBurnInTimer() 里注册的 DeviceStateEventManager 回调，
     *       由 Application::SetDeviceState() → PostStateChangeEvent() 触发。
     * 谁读：AntiBurnInStep()，跑在 LVGL 定时器任务里。
     * volatile：写的一方是默认事件循环任务（sys_evt），读的一方是 LVGL
     *       定时器任务，跨任务且无锁 —— 编译器不得把这次读取跨调用缓存/
     *       合并（lv_timer 在它自己的循环里反复调用 AntiBurnInStep，
     *       一旦读被提到循环外，写侧置位就永远看不到）。只存一个 bool，
     *       单字节读写天然原子，不需要 atomic / 互斥量。
     * 初始 false：开机流程里 display 先于状态机就位，没收到事件前一律
     *       按"非待机"处理 —— 宁可少播一次熊猫，也不许在开机自检/配网
     *       界面乱播待机指示灯。 */
    volatile bool device_idle_ = false;
    std::string anti_burn_last_emoji_;      /* 兜底轮播用：避免连选同一张 */

protected:
    // Add protected constructor
    LcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel, int width, int height);
    
public:
    ~LcdDisplay();
    virtual void SetEmotion(const char* emotion) override;
    virtual void SetChatMessage(const char* role, const char* content) override; 
    virtual void SetPreviewImage(std::unique_ptr<LvglImage> image) override;

    // Add theme switching function
    virtual void SetTheme(Theme* theme) override;
    
    // Set whether to hide chat messages/subtitles
    void SetHideSubtitle(bool hide);

    // >>> BEGIN elder-reminder-fullscreen (2026-10-22，需求 8) >>>
    /* 全屏显示一条提醒内容（图片区不再显示，用整屏大字）。
     *
     * 为什么放在 LcdDisplay 而不是基类 Display：
     *   基类 display.h 是上游文件，加虚函数会牵动所有子类（OLED / Emote /
     *   RGB……全部要跟着实现一遍）。而"全屏大字提醒"只对这块 320x240 的
     *   屏有意义，所以只在本类暴露，由 elder_bridge 用 dynamic_cast 取到
     *   （本工程 -frtti 已开）。代价：没有屏/不是这台机时拿不到 overlay，
     *   但那条路径本来就不该显示全屏提醒。
     *
     * 显隐由调用方控制（elder_ui 负责 4 小时倒计时），本类不管计时。
     */
    void ShowReminder(const char* text);
    void HideReminder();
    lv_obj_t* reminder_overlay_ = nullptr;   /* 懒创建，全屏不透明 */
    lv_obj_t* reminder_label_ = nullptr;
    // <<< END elder-reminder-fullscreen <<<

    // >>> BEGIN elder-idle-panda (2026-10-22 需求 7：待机立即播熊猫) >>>
    /* 进入待机时**立即**开播动态熊猫，不等防烧屏的 60 秒。
     *
     * 需求原话（2026-10-22）：「进入待机状态强制切换到待机动态熊猫图案，
     * 以便区分硬件是否在监听声音，让用户知道是否需要唤醒词进行唤醒。」
     *
     * 与 AntiBurnInStep() 的本质区别：那条路径开头有
     * `if (lv_tick_elaps(last_ui_change_tick_) < ANTI_BURN_IDLE_MS) return;`
     * —— 界面刚变过（SetEmotion("neutral") 正是界面变化）之后的 60 秒内
     * 一律跳过，而"进待机"恰好每次都紧跟一次 SetEmotion，于是熊猫永远
     * 播不上。用户看到的待机屏与"死机"无从区分。
     *
     * 由 elder_bridge.cc 调用（dynamic_cast 拿到本类时）；无屏/非本板卡
     * 时拿不到本类，静默不播，待机本身不受影响。 */
    void PlayIdleAnimation();
    // <<< END elder-idle-panda <<<
};

// SPI LCD display
class SpiLcdDisplay : public LcdDisplay {
public:
    SpiLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                  int width, int height, int offset_x, int offset_y,
                  bool mirror_x, bool mirror_y, bool swap_xy);
};

// RGB LCD display
class RgbLcdDisplay : public LcdDisplay {
public:
    RgbLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                  int width, int height, int offset_x, int offset_y,
                  bool mirror_x, bool mirror_y, bool swap_xy);
};

// MIPI LCD display
class MipiLcdDisplay : public LcdDisplay {
public:
    MipiLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                   int width, int height, int offset_x, int offset_y,
                   bool mirror_x, bool mirror_y, bool swap_xy);
};

#endif // LCD_DISPLAY_H
