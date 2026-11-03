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
#include <vector>

#define PREVIEW_IMAGE_DURATION_MS 5000

/* 防烧屏轮播（2026-10-20 用户要求：屏幕常亮固定一张图，担心烧屏）。
 *
 * 做法：空闲时把表情图换成"待机跑马灯"（见 StartIdleMarquee），即图片区里
 * 一张表情从右往左匀速滚过、滚完换下一张。为什么是"滚动表情"而不是
 * "整屏平移"：这是 320x240 IPS，真正会留下残影的是**长期不动的高对比
 * 区域**（中央的表情图正是这种）；整屏平移一两个像素在 LVGL 里要动
 * 所有对象、每帧重排，代价远大于收益，而且用户看不懂"界面在抖"。
 *
 * 两条判据，缺一不可：
 *   ① 「距上次 UI 变化多久」—— "界面 60 秒没变过"是烧屏风险的定义本身；
 *   ② 「设备是否处于待机态」—— 屏保播的是**待机指示灯**（待机跑马灯），
 *      非待机态播它等于对老人撒谎（见 AntiBurnInStep 里的 2026-11-01 事故注释）。
 *
 * 早期只写了 ①，理由是「设备状态要问 Application，显示层不该反向依赖它」。
 * 那个理由站不住：判据不取 Application 的私有状态，走的是 device_state_event
 * 的广播（Application 主动 PostStateChangeEvent），方向仍是 Application → 显示层，
 * 没有反向依赖。代价是只靠 ① 时，老人沉默停在聆听态 60 秒就会把待机指示灯播上去。
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
    /* 左列第二行：用户刚说的话（设计稿里是蓝色 #4982FC 那一行）。
     *
     * 为什么需要第二个 label：设计稿的对话态是「上面一行用户问话（蓝）+
     * 下面一段小安回答（深色）」。而 SetChatMessage(role, content) 每来一条
     * 就把 label 整段覆盖，只留一个 label 的话，回答一出现用户的话就没了。
     * 两块 label 分工：user → chat_user_label_，assistant/system →
     * chat_message_label_，互不覆盖。 */
    lv_obj_t* chat_user_label_ = nullptr;
    /* 状态栏右上角的日期「10月29日 周三」。时钟走 SetStatusClock()，
     * 日期跟它一起刷（同一个入口，避免两处各算一遍时间）。 */
    lv_obj_t* date_label_ = nullptr;
    esp_timer_handle_t preview_timer_ = nullptr;
    std::unique_ptr<LvglImage> preview_image_cached_ = nullptr;
    bool hide_subtitle_ = false;  // Control whether to hide chat messages/subtitles

    void InitializeLcdThemes();
    void SetupUI();
    virtual bool Lock(int timeout_ms = 0) override;
    virtual void Unlock() override;

    /* 空闲屏保：在 SetupUI 末尾启动，静止 60s 后开播待机跑马灯，
     * 一有语境（SetEmotion / SetChatMessage）就停掉、回到静态表情图标。 */
    void StartAntiBurnInTimer();
    void AntiBurnInStep();
    lv_timer_t* anti_burn_timer_ = nullptr;
    uint32_t last_ui_change_tick_ = 0;      /* lv_tick 毫秒 */

    /* ---- 对话分页（2026-11-03 用户需求）----
     *
     * 「用户说的话和 AI 说的话放在不同的两页，如果一页显示不全，自动翻页」。
     * 实现是**按页滚动**左列容器 bottom_bar_，不是两个 label 轮显：
     * 一段话本身就可能跨页，轮显只能二选一，翻不了页内。
     *
     * 页高 = 整行文字的整数倍（不是容器高度）：188px 的容器按 42px 行高只
     * 放得下 4 行（168px），按 188 滚会把交界处那一行切成半截字。
     *
     * 所有成员只在 LVGL 任务里读写（SetChatMessage / 定时器回调都在持锁后
     * 调用），所以不需要 atomic。 */
    int ChatLineH();        /* 一行多少像素：字体行高 + 行间距，现算不缓存 */
    int ChatPageH();        /* 一页多少像素：整行倍数 */
    void ChatPageLayout();  /* 重新量文字、决定是否让回答另起一页、按需起定时器 */
    void ChatPageStep();    /* 定时器回调：滚到下一页，最后一页停住 */
    lv_timer_t* chat_page_timer_ = nullptr;
    int chat_page_index_ = 0;       /* 当前页号，0 起 */
    int chat_page_count_ = 0;       /* 总页数 */
    int chat_page_h_ = 0;           /* 页高（像素） */
    int chat_page_max_scroll_ = 0;  /* 内容高 - 容器高，最后一页可能滚不到整页 */

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
     *       按"非待机"处理 —— 宁可少播一次待机指示灯，也不许在开机自检/
     *       配网界面乱播它。 */
    volatile bool device_idle_ = false;
    std::string anti_burn_last_emoji_;      /* 兜底轮播用：避免连选同一张 */

    /* >>> BEGIN elder-idle-marquee-internals (2026-11-03：待机跑马灯，类内部实现) >>>
     *
     * ⚠️ 本文件里有**两个** elder-idle-marquee 标记，别当成插重复了：
     *   · 这里 `-internals`：protected 段的成员与方法声明（实现细节）
     *   · 下面 public 段的 `elder-idle-marquee`：对外只暴露 PlayIdleAnimation()
     *     （elder_bridge.cc 要用）。两段是一件事的两半。
     *
     * 待机指示灯的实现：图片区里**一个** lv_image，由 lv_anim 驱动 x 匀速
     * 从右边缘外移到左边缘外，移完换下一张表情的 src 并回到右边缘外。
     *
     * 为什么不用原来那套编译进 .rodata 的帧动画（AnimEmojiPlayer + 熊猫）：
     *   · 269,568 字节压在 app 分区，而 app 分区是本项目最紧的一块；
     *   · 熊猫是"一整张会动的图"，看久了像屏保/装饰；跑马灯滚的是表情，
     *     语义上更贴"小安在待机、可以喊我"。
     * 为什么复用 assets 里的 cbin 表情而不是另做一套 PNG：本机
     * CONFIG_LV_CACHE_DEF_SIZE=0（图片缓存关着），PNG 每帧都要 lodepng
     * 重解，逐帧滚动会把 LVGL 任务拖死 —— 见 assets.cc 里 2026-11-02 的注释。
     *
     * 线程：全部方法都要求调用方**已持 DisplayLockGuard**（lv_anim 的回调
     * 跑在 LVGL 任务里，那里本来就持锁）。 */
    bool StartIdleMarquee();     /* 幂等：已经在跑就直接返回 true */
    void StopIdleMarquee();      /* 幂等：没在跑就只清状态 */
    bool idle_marquee_running_ = false;
    lv_obj_t* idle_marquee_image_ = nullptr;    /* 懒创建，只建一个 */
    /* 第二张（错开半个行程）：设计稿主区里同时能看到 3~4 个圆，只放一张的话
     * 任何时刻屏上只有一个 60px 的圆漂在 320px 空场里，观感是"有个东西在飘"。
     * 两张共用同一条 lv_anim（同一个 exec 回调里算两个 x），相位天然同步。 */
    lv_obj_t* idle_marquee_image2_ = nullptr;
    int32_t idle_marquee_size_ = 0;             /* 表情边长，exec 回调算错位用 */
    /* 第二张「当前处于回绕状态」的标志（见 IdleMarqueeExecCb）。
     * 它的换图时机**必须**与第一张不同：两张相位差半程，第一张回绕时第二张
     * 正好在屏幕中间，那一刻换 src 会让老人看到"屏中央那张脸突然变了"。 */
    bool idle_marquee_image2_wrapped_ = false;
    int32_t idle_marquee_last_x_ = 0;           /* 上一帧的 x，用来判「滚完一轮」 */
    /* 两张图共用的取图游标：谁先换图谁拿走下一张，因此两张永远不会撞成
     * 同一个表情（ready 列表 ≥ 2 项时）。 */
    size_t idle_marquee_next_ = 0;              /* 下一张在 ready 列表里的下标 */
    /* 本次启动时校验过"取得到图"的名字（指向静态字面量，无需释放）。
     * 每次启动重建：assets 分区挂载晚于 SetupUI，主题也可能换，缓存会过期。 */
    std::vector<const char*> idle_marquee_ready_;
    static void IdleMarqueeExecCb(void* var, int32_t x);
    /* 只给**一张**换图：调用方决定是谁、以及此刻它是否在屏外。
     * 取 target 参数而不是内部轮流，是为了让"换图发生在屏外"这件事由调用点
     * 负责（两张的屏外时刻不同，见 IdleMarqueeExecCb 里的说明）。 */
    void AdvanceIdleMarqueeImage(lv_obj_t* target);
    // <<< END elder-idle-marquee-internals <<<

protected:
    // Add protected constructor
    LcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel, int width, int height);
    
public:
    ~LcdDisplay();
    virtual void SetEmotion(const char* emotion) override;
    virtual void SetChatMessage(const char* role, const char* content) override; 
    virtual void SetPreviewImage(std::unique_ptr<LvglImage> image) override;

    /* 状态栏时钟入口（2026-11-03 设计稿还原）。
     * 基类 Display 的默认实现是「当普通状态文案处理」，这里覆盖成
     * 「写左上的时钟 label + 顺手刷新右上角日期」。 */
    virtual void SetStatusClock(const char* text) override;

    /* 普通状态文案（正在连接 / 已连上 / 配网中……）。
     *
     * 为什么要覆盖：设计稿把状态栏切成了「左时钟 34% + 右图标日期」两段，
     * 时钟只占 34%（108px）。而状态文案动辄十几个字，塞进 108px 会被
     * LONG_DOT 截成「正在连…」—— 老人看到半句话，比什么都不显示更慌。
     * 所以走这条通道时临时把日期藏起来、把左段放宽到 78%，时钟恢复时
     * （SetStatusClock）再收回去。 */
    virtual void SetStatus(const char* status) override;

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
    lv_obj_t* reminder_overlay_ = nullptr;   /* 懒创建，不透明，只占状态栏以下 */

    /* ---- 2026-11-03：按设计稿改造成结构化卡片 ----
     *
     * 设计稿（deliverables/gstack/design/mock-1-reminder.png）是：
     *   左列  药丸图标 + 标题「该吃药了」
     *         药品逐条（药名在左、剂量在右）
     *         橙色提示「请及时服用」
     *         橙色圆角按钮「长按机身顶部大按键确认」
     *   右侧  90x90 头像圆
     *
     * 文案仍然只有一个来源：ShowReminder(text) 收到的字符串。约定是
     * **一行一件事**（见 reminder.c 的 elder_reminder_screen_text）：
     *   第 0 行 标题
     *   第 1..n 行 明细（每行一条；超过 4 行截断，屏上放不下）
     *   最后一行若以「请」开头 ⇒ 当橙色提示语，不当明细
     * 这个约定写在两边的注释里 —— 改一边必须改另一边。 */
    lv_obj_t* reminder_title_ = nullptr;     /* 标题行（带红色药丸图标） */
    lv_obj_t* reminder_items_ = nullptr;     /* 明细列表（多行） */
    lv_obj_t* reminder_hint_ = nullptr;      /* 橙色提示，可为空 */
    lv_obj_t* reminder_button_ = nullptr;    /* 橙色圆角按钮（UI 常量文案） */
    lv_obj_t* reminder_button_label_ = nullptr; /* 按钮上的白字，SetTheme 要补字体 */
    lv_obj_t* reminder_avatar_ = nullptr;    /* 右侧头像圆 */
    // <<< END elder-reminder-fullscreen <<<

    // >>> BEGIN elder-idle-marquee (2026-11-03：待机指示灯 = 跑马灯) >>>
    /* 进入待机时**立即**开播待机跑马灯，不等防烧屏的 60 秒。
     *
     * 需求来源（2026-10-22 用户原话）：「进入待机状态强制切换到待机动态图案，
     * 以便区分硬件是否在监听声音，让用户知道是否需要唤醒词进行唤醒。」
     * 2026-11-03 用户改为：待机熊猫删掉，这个位置放跑马灯（见 StartIdleMarquee）。
     *
     * 与 AntiBurnInStep() 的本质区别：那条路径开头有
     * `if (lv_tick_elaps(last_ui_change_tick_) < ANTI_BURN_IDLE_MS) return;`
     * —— 界面刚变过（SetEmotion("neutral") 正是界面变化）之后的 60 秒内
     * 一律跳过，而"进待机"恰好每次都紧跟一次 SetEmotion，于是跑马灯永远
     * 起不来。用户看到的待机屏与"死机"无从区分。
     *
     * 由 elder_bridge.cc 调用（dynamic_cast 拿到本类时）；无屏/非本板卡
     * 时拿不到本类，静默不播，待机本身不受影响。 */
    void PlayIdleAnimation();
    // <<< END elder-idle-marquee <<<
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
