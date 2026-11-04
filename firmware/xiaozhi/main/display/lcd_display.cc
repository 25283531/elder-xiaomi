#include "lcd_display.h"
#include "gif/lvgl_gif.h"
#include "settings.h"
#include "lvgl_theme.h"
#include "assets/lang_config.h"

#include <vector>
#include <algorithm>
#include <font_awesome.h>
#include <esp_log.h>
#include <esp_err.h>
#include <esp_random.h>
#include <esp_lvgl_port.h>
#include <esp_psram.h>
#include <cstring>
#include <cstdio>
#include <ctime>
#include <string>   // 字幕累积：std::string 拼上一句 + 这一句（SetChatMessage）

#include "board.h"

/* 防烧屏屏保必须知道「设备是不是待机」：待机跑马灯是待机指示灯，不是普通屏保。
 * 两个头都在 main 组件内（与 main/display/ 同级目录），直接 include 即可，
 * elder_bridge.cc 用的也是这两个。这里只读广播出来的 DeviceState，
 * 不碰 Application 本身，所以不构成"显示层反向依赖业务层"。 */
#include "device_state_event.h"
#include "device_state.h"


// >>> BEGIN elder-companion overlay (由 tools/merge_to_xiaozhi.py 注入，勿手改) >>>
// LVGL 9.3.0 把 lv_image_cache_resize() 藏在内部头文件里，lvgl.h 未暴露，
// 而本文件在 CONFIG_SPIRAM 分支调用了它 —— 补上声明，否则编译失败。
#if defined(__has_include)
#if __has_include("src/misc/cache/instance/lv_image_cache.h")
#include "src/misc/cache/instance/lv_image_cache.h"
#else
extern "C" void lv_image_cache_resize(uint32_t new_size, bool evict_now);
#endif
#else
#include "src/misc/cache/instance/lv_image_cache.h"
#endif
// <<< END elder-companion overlay <<<

#define TAG "LcdDisplay"

LV_FONT_DECLARE(BUILTIN_TEXT_FONT);
LV_FONT_DECLARE(BUILTIN_ICON_FONT);
LV_FONT_DECLARE(font_awesome_30_4);

/* ===== 2026-11-03 设计稿还原：配色与尺寸的**唯一真源** =====
 *
 * 来源：deliverables/gstack/design/mock-{1-reminder,2-chat,3-idle}.png
 * （三张都是 320x240，用 PIL 逐像素量出来的，不是目测）。
 *
 * 尺寸一律写成「按 240 高 / 320 宽折算」，不写死像素：LcdDisplay 同时被
 * SPI / RGB / MIPI 三种屏继承，写死 320x240 的数值会在别的分辨率上错位。
 */
namespace {

/* 配色（实测色值，不要改成"看起来差不多"的近色） */
constexpr uint32_t kColorStatusBar = 0x2A365B;  /* 状态栏藏青 */
constexpr uint32_t kColorMainBg    = 0xD3DBFC;  /* 主区浅蓝紫 */
constexpr uint32_t kColorTextDark  = 0x232F54;  /* 正文深色 */
constexpr uint32_t kColorTextUser  = 0x4982FC;  /* 用户话蓝色 */
constexpr uint32_t kColorAccent    = 0xF5A623;  /* 橙色提示 / 按钮 */
constexpr uint32_t kColorAvatar    = 0x7B93E8;  /* 头像圆底 */
constexpr uint32_t kColorPillRed   = 0xDD2E44;  /* 药丸图标红 */

/* 状态栏高 30px（320x240 实测 0..29） */
inline int StatusBarH()  { return LV_VER_RES * 30 / 240; }
/* 文字列左边距 18px */
inline int MainPadX()    { return LV_HOR_RES * 18 / 320; }
/* 头像直径 90px（实测包围盒 89x89，含抗锯齿按 90 算） */
inline int AvatarSize()  { return LV_VER_RES * 90 / 240; }
/* 头像右边距 21px（实测右边 299 ⇒ 320-299=21） */
inline int AvatarRight() { return LV_HOR_RES * 21 / 320; }
/* 头像顶边距状态栏底边 44px（实测头像包围盒顶边 y=74，状态栏底 30） */
inline int AvatarTop()   { return StatusBarH() + LV_VER_RES * 44 / 240; }
/* 左列文字顶边距状态栏底边 14px（实测首行文字 y≈44，比头像高 30px） */
inline int TextTop()     { return StatusBarH() + LV_VER_RES * 14 / 240; }

/* 待机跑马灯的表情直径 60px（mock-3 实测 60x60，纵向居中于主区） */
inline int MarqueeSize() { return LV_VER_RES * 60 / 240; }

}  // namespace

void LcdDisplay::InitializeLcdThemes() {
    auto text_font = std::make_shared<LvglBuiltInFont>(&BUILTIN_TEXT_FONT);
    auto icon_font = std::make_shared<LvglBuiltInFont>(&BUILTIN_ICON_FONT);
    auto large_icon_font = std::make_shared<LvglBuiltInFont>(&font_awesome_30_4);

    // light theme
    auto light_theme = new LvglTheme("light");
    /* 2026-11-03 设计稿：主区底色 #D3DBFC、正文 #232F54。
     *
     * 原来这里是纯白底 + 纯黑字。改的是**主题色**而不是各对象的局部色：
     * screen / container_ / bottom_bar_ / reminder_overlay_ 都读
     * theme->background_color()，改一处全部跟上；反过来，只在 SetupUI 里
     * 逐个设 bg_color 的话，SetTheme() 换主题时又会被刷回主题色。
     *
     * ⚠️ 只改 light：dark 主题是夜间/低光用的，改成浅蓝紫等于把夜间模式
     * 废掉。本机 Settings 默认 theme="light"，实际生效的就是这一套。 */
    light_theme->set_background_color(lv_color_hex(kColorMainBg));
    light_theme->set_text_color(lv_color_hex(kColorTextDark));
    light_theme->set_chat_background_color(lv_color_hex(kColorMainBg));
    light_theme->set_user_bubble_color(lv_color_hex(0x00FF00));
    light_theme->set_assistant_bubble_color(lv_color_hex(0xDDDDDD));
    light_theme->set_system_bubble_color(lv_color_hex(0xFFFFFF));
    light_theme->set_system_text_color(lv_color_hex(0x000000));
    light_theme->set_border_color(lv_color_hex(0x000000));
    light_theme->set_low_battery_color(lv_color_hex(0x000000));
    light_theme->set_text_font(text_font);
    light_theme->set_icon_font(icon_font);
    light_theme->set_large_icon_font(large_icon_font);

    // dark theme
    auto dark_theme = new LvglTheme("dark");
    dark_theme->set_background_color(lv_color_hex(0x000000));
    dark_theme->set_text_color(lv_color_hex(0xFFFFFF));
    dark_theme->set_chat_background_color(lv_color_hex(0x1F1F1F));
    dark_theme->set_user_bubble_color(lv_color_hex(0x00FF00));
    dark_theme->set_assistant_bubble_color(lv_color_hex(0x222222));
    dark_theme->set_system_bubble_color(lv_color_hex(0x000000));
    dark_theme->set_system_text_color(lv_color_hex(0xFFFFFF));
    dark_theme->set_border_color(lv_color_hex(0xFFFFFF));
    dark_theme->set_low_battery_color(lv_color_hex(0xFF0000));
    dark_theme->set_text_font(text_font);
    dark_theme->set_icon_font(icon_font);
    dark_theme->set_large_icon_font(large_icon_font);

    auto& theme_manager = LvglThemeManager::GetInstance();
    theme_manager.RegisterTheme("light", light_theme);
    theme_manager.RegisterTheme("dark", dark_theme);
}

LcdDisplay::LcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel, int width, int height)
    : panel_io_(panel_io), panel_(panel) {
    width_ = width;
    height_ = height;

    // Initialize LCD themes
    InitializeLcdThemes();

    // Load theme from settings
    Settings settings("display", false);
    std::string theme_name = settings.GetString("theme", "light");
    current_theme_ = LvglThemeManager::GetInstance().GetTheme(theme_name);

    // Create a timer to hide the preview image
    esp_timer_create_args_t preview_timer_args = {
        .callback = [](void* arg) {
            LcdDisplay* display = static_cast<LcdDisplay*>(arg);
            display->SetPreviewImage(nullptr);
        },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "preview_timer",
        .skip_unhandled_events = false,
    };
    esp_timer_create(&preview_timer_args, &preview_timer_);
}

SpiLcdDisplay::SpiLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                           int width, int height, int offset_x, int offset_y, bool mirror_x, bool mirror_y, bool swap_xy)
    : LcdDisplay(panel_io, panel, width, height) {

    // draw white
    std::vector<uint16_t> buffer(width_, 0xFFFF);
    for (int y = 0; y < height_; y++) {
        esp_lcd_panel_draw_bitmap(panel_, 0, y, width_, y + 1, buffer.data());
    }

    // Set the display to on
    ESP_LOGI(TAG, "Turning display on");
    {
        esp_err_t __err = esp_lcd_panel_disp_on_off(panel_, true);
        if (__err == ESP_ERR_NOT_SUPPORTED) {
            ESP_LOGW(TAG, "Panel does not support disp_on_off; assuming ON");
        } else {
            ESP_ERROR_CHECK(__err);
        }
    }

    ESP_LOGI(TAG, "Initialize LVGL library");
    lv_init();

#if CONFIG_SPIRAM
    // lv image cache, currently only PNG is supported
    size_t psram_size_mb = esp_psram_get_size() / 1024 / 1024;
    if (psram_size_mb >= 8) {
        lv_image_cache_resize(2 * 1024 * 1024, true);
        ESP_LOGI(TAG, "Use 2MB of PSRAM for image cache");
    } else if (psram_size_mb >= 2) {
        lv_image_cache_resize(512 * 1024, true);
        ESP_LOGI(TAG, "Use 512KB of PSRAM for image cache");
    }
#endif

    ESP_LOGI(TAG, "Initialize LVGL port");
    lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    port_cfg.task_priority = 1;
#if CONFIG_SOC_CPU_CORES_NUM > 1
    port_cfg.task_affinity = 1;
#endif
    lvgl_port_init(&port_cfg);

    ESP_LOGI(TAG, "Adding LCD display");
    const lvgl_port_display_cfg_t display_cfg = {
        .io_handle = panel_io_,
        .panel_handle = panel_,
        .control_handle = nullptr,
        .buffer_size = static_cast<uint32_t>(width_ * 20),
        .double_buffer = false,
        .trans_size = 0,
        .hres = static_cast<uint32_t>(width_),
        .vres = static_cast<uint32_t>(height_),
        .monochrome = false,
        .rotation = {
            .swap_xy = swap_xy,
            .mirror_x = mirror_x,
            .mirror_y = mirror_y,
        },
        .color_format = LV_COLOR_FORMAT_RGB565,
        .flags = {
            .buff_dma = 1,
            .buff_spiram = 0,
            .sw_rotate = 0,
            .swap_bytes = 1,
            .full_refresh = 0,
            .direct_mode = 0,
        },
    };

    display_ = lvgl_port_add_disp(&display_cfg);
    if (display_ == nullptr) {
        ESP_LOGE(TAG, "Failed to add display");
        return;
    }

    if (offset_x != 0 || offset_y != 0) {
        lv_display_set_offset(display_, offset_x, offset_y);
    }

    SetupUI();
}


// RGB LCD implementation
RgbLcdDisplay::RgbLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                           int width, int height, int offset_x, int offset_y,
                           bool mirror_x, bool mirror_y, bool swap_xy)
    : LcdDisplay(panel_io, panel, width, height) {

    // draw white
    std::vector<uint16_t> buffer(width_, 0xFFFF);
    for (int y = 0; y < height_; y++) {
        esp_lcd_panel_draw_bitmap(panel_, 0, y, width_, y + 1, buffer.data());
    }

    ESP_LOGI(TAG, "Initialize LVGL library");
    lv_init();

    ESP_LOGI(TAG, "Initialize LVGL port");
    lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    port_cfg.task_priority = 1;
    port_cfg.timer_period_ms = 50;
    lvgl_port_init(&port_cfg);

    ESP_LOGI(TAG, "Adding LCD display");
    const lvgl_port_display_cfg_t display_cfg = {
        .io_handle = panel_io_,
        .panel_handle = panel_,
        .buffer_size = static_cast<uint32_t>(width_ * 20),
        .double_buffer = true,
        .hres = static_cast<uint32_t>(width_),
        .vres = static_cast<uint32_t>(height_),
        .rotation = {
            .swap_xy = swap_xy,
            .mirror_x = mirror_x,
            .mirror_y = mirror_y,
        },
        .flags = {
            .buff_dma = 1,
            .swap_bytes = 0,
            .full_refresh = 1,
            .direct_mode = 1,
        },
    };

    const lvgl_port_display_rgb_cfg_t rgb_cfg = {
        .flags = {
            .bb_mode = true,
            .avoid_tearing = true,
        }
    };
    
    display_ = lvgl_port_add_disp_rgb(&display_cfg, &rgb_cfg);
    if (display_ == nullptr) {
        ESP_LOGE(TAG, "Failed to add RGB display");
        return;
    }
    
    if (offset_x != 0 || offset_y != 0) {
        lv_display_set_offset(display_, offset_x, offset_y);
    }

    SetupUI();
}

MipiLcdDisplay::MipiLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                            int width, int height,  int offset_x, int offset_y,
                            bool mirror_x, bool mirror_y, bool swap_xy)
    : LcdDisplay(panel_io, panel, width, height) {

    ESP_LOGI(TAG, "Initialize LVGL library");
    lv_init();

    ESP_LOGI(TAG, "Initialize LVGL port");
    lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    lvgl_port_init(&port_cfg);

    ESP_LOGI(TAG, "Adding LCD display");
    const lvgl_port_display_cfg_t disp_cfg = {
        .io_handle = panel_io,
        .panel_handle = panel,
        .control_handle = nullptr,
        .buffer_size = static_cast<uint32_t>(width_ * 50),
        .double_buffer = false,
        .hres = static_cast<uint32_t>(width_),
        .vres = static_cast<uint32_t>(height_),
        .monochrome = false,
        /* Rotation values must be same as used in esp_lcd for initial settings of the screen */
        .rotation = {
            .swap_xy = swap_xy,
            .mirror_x = mirror_x,
            .mirror_y = mirror_y,
        },
        .flags = {
            .buff_dma = true,
            .buff_spiram =false,
            .sw_rotate = true,
        },
    };

    const lvgl_port_display_dsi_cfg_t dpi_cfg = {
        .flags = {
            .avoid_tearing = false,
        }
    };
    display_ = lvgl_port_add_disp_dsi(&disp_cfg, &dpi_cfg);
    if (display_ == nullptr) {
        ESP_LOGE(TAG, "Failed to add display");
        return;
    }

    if (offset_x != 0 || offset_y != 0) {
        lv_display_set_offset(display_, offset_x, offset_y);
    }

    SetupUI();
}

LcdDisplay::~LcdDisplay() {
    SetPreviewImage(nullptr);
    
    // Clean up GIF controller
    if (gif_controller_) {
        gif_controller_->Stop();
        gif_controller_.reset();
    }

    /* 待机跑马灯的 lv_anim 必须在删对象**之前**停：它的 exec 回调里拿的是
     * idle_marquee_image_ 这个指针，对象被 lv_obj_del 之后那条动画还在动画
     * 链表里，下一帧就会对已释放内存调 lv_obj_set_x（LoadProhibited）。 */
    StopIdleMarquee();

    if (preview_timer_ != nullptr) {
        esp_timer_stop(preview_timer_);
        esp_timer_delete(preview_timer_);
    }

    if (preview_image_ != nullptr) {
        lv_obj_del(preview_image_);
    }
    /* >>> BEGIN elder-reminder-fullscreen >>> 懒创建的 overlay 也要在这里删：
     * 它挂在 screen 下，不随任何子对象一起释放；漏掉就是一块 320x240 的
     * 不透明遮罩留在屏上（表现为"重启后屏幕全黑，只有状态栏")。
     *
     * 2026-11-03 起卡片有 5 个子对象（标题/明细/提示/按钮/头像），全部挂在
     * overlay 下面，lv_obj_del(overlay) 会连带删掉 —— 这里只需把**指针**
     * 清空，否则析构后再进 ShowReminder 会对着已释放的内存设属性。 */
    if (reminder_overlay_ != nullptr) {
        lv_obj_del(reminder_overlay_);
        reminder_overlay_ = nullptr;
        reminder_title_ = nullptr;
        reminder_items_ = nullptr;
        reminder_hint_ = nullptr;
        reminder_button_ = nullptr;
        reminder_button_label_ = nullptr;
        reminder_avatar_ = nullptr;
    }
    /* <<< END elder-reminder-fullscreen <<< */
    if (chat_user_label_ != nullptr) {
        lv_obj_del(chat_user_label_);
        chat_user_label_ = nullptr;
    }
    if (chat_message_label_ != nullptr) {
        lv_obj_del(chat_message_label_);
    }
    /* user_box_ 是 chat_user_label_ 的父对象：label 已删，这里只删空容器。
     * 与 bottom_bar_ 同一写法（见下面那段），别漏 —— 漏了就是析构后仍有一个
     * 挂着空 label 的容器留在屏上。 */
    if (user_box_ != nullptr) {
        lv_obj_del(user_box_);
        user_box_ = nullptr;
    }
    chat_page_box_ = nullptr;
    if (emoji_label_ != nullptr) {
        lv_obj_del(emoji_label_);
    }
    if (emoji_image_ != nullptr) {
        lv_obj_del(emoji_image_);
    }
    if (emoji_box_ != nullptr) {
        lv_obj_del(emoji_box_);
    }
    if (content_ != nullptr) {
        lv_obj_del(content_);
    }
    if (bottom_bar_ != nullptr) {
        lv_obj_del(bottom_bar_);
    }
    if (status_bar_ != nullptr) {
        lv_obj_del(status_bar_);
    }
    if (top_bar_ != nullptr) {
        lv_obj_del(top_bar_);
    }
    if (side_bar_ != nullptr) {
        lv_obj_del(side_bar_);
    }
    if (container_ != nullptr) {
        lv_obj_del(container_);
    }
    if (display_ != nullptr) {
        lv_display_delete(display_);
    }

    if (panel_ != nullptr) {
        esp_lcd_panel_del(panel_);
    }
    if (panel_io_ != nullptr) {
        esp_lcd_panel_io_del(panel_io_);
    }
}

bool LcdDisplay::Lock(int timeout_ms) {
    return lvgl_port_lock(timeout_ms);
}

void LcdDisplay::Unlock() {
    lvgl_port_unlock();
}

#if CONFIG_USE_WECHAT_MESSAGE_STYLE
void LcdDisplay::SetupUI() {
    DisplayLockGuard lock(this);

    auto lvgl_theme = static_cast<LvglTheme*>(current_theme_);
    auto text_font = lvgl_theme->text_font()->font();
    auto icon_font = lvgl_theme->icon_font()->font();
    auto large_icon_font = lvgl_theme->large_icon_font()->font();

    auto screen = lv_screen_active();
    lv_obj_set_style_text_font(screen, text_font, 0);
    lv_obj_set_style_text_color(screen, lvgl_theme->text_color(), 0);
    lv_obj_set_style_bg_color(screen, lvgl_theme->background_color(), 0);

    /* Container */
    container_ = lv_obj_create(screen);
    lv_obj_set_size(container_, LV_HOR_RES, LV_VER_RES);
    lv_obj_set_style_radius(container_, 0, 0);
    lv_obj_set_flex_flow(container_, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_all(container_, 0, 0);
    lv_obj_set_style_border_width(container_, 0, 0);
    lv_obj_set_style_pad_row(container_, 0, 0);
    lv_obj_set_style_bg_color(container_, lvgl_theme->background_color(), 0);
    lv_obj_set_style_border_color(container_, lvgl_theme->border_color(), 0);

    /* Layer 1: Top bar - for status icons */
    top_bar_ = lv_obj_create(container_);
    lv_obj_set_size(top_bar_, LV_HOR_RES, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(top_bar_, 0, 0);
    lv_obj_set_style_bg_opa(top_bar_, LV_OPA_50, 0);  // 50% opacity background
    lv_obj_set_style_bg_color(top_bar_, lvgl_theme->background_color(), 0);
    lv_obj_set_style_border_width(top_bar_, 0, 0);
    lv_obj_set_style_pad_all(top_bar_, 0, 0);
    lv_obj_set_style_pad_top(top_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_bottom(top_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_left(top_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_pad_right(top_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_flex_flow(top_bar_, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(top_bar_, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_scrollbar_mode(top_bar_, LV_SCROLLBAR_MODE_OFF);

    // Left icon
    network_label_ = lv_label_create(top_bar_);
    lv_label_set_text(network_label_, "");
    lv_obj_set_style_text_font(network_label_, icon_font, 0);
    lv_obj_set_style_text_color(network_label_, lvgl_theme->text_color(), 0);

    // Right icons container
    lv_obj_t* right_icons = lv_obj_create(top_bar_);
    lv_obj_set_size(right_icons, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(right_icons, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(right_icons, 0, 0);
    lv_obj_set_style_pad_all(right_icons, 0, 0);
    lv_obj_set_flex_flow(right_icons, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(right_icons, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

    mute_label_ = lv_label_create(right_icons);
    lv_label_set_text(mute_label_, "");
    lv_obj_set_style_text_font(mute_label_, icon_font, 0);
    lv_obj_set_style_text_color(mute_label_, lvgl_theme->text_color(), 0);

    battery_label_ = lv_label_create(right_icons);
    lv_label_set_text(battery_label_, "");
    lv_obj_set_style_text_font(battery_label_, icon_font, 0);
    lv_obj_set_style_text_color(battery_label_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_margin_left(battery_label_, lvgl_theme->spacing(2), 0);

    /* Layer 2: Status bar - for center text labels */
    status_bar_ = lv_obj_create(screen);
    lv_obj_set_size(status_bar_, LV_HOR_RES, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(status_bar_, 0, 0);
    lv_obj_set_style_bg_opa(status_bar_, LV_OPA_TRANSP, 0);  // Transparent background
    lv_obj_set_style_border_width(status_bar_, 0, 0);
    lv_obj_set_style_pad_all(status_bar_, 0, 0);
    lv_obj_set_style_pad_top(status_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_bottom(status_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_scrollbar_mode(status_bar_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_style_layout(status_bar_, LV_LAYOUT_NONE, 0);  // Use absolute positioning
    lv_obj_align(status_bar_, LV_ALIGN_TOP_MID, 0, 0);  // Overlap with top_bar_

    notification_label_ = lv_label_create(status_bar_);
    lv_obj_set_width(notification_label_, LV_HOR_RES * 0.8);
    lv_obj_set_style_text_align(notification_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(notification_label_, lvgl_theme->text_color(), 0);
    lv_label_set_text(notification_label_, "");
    lv_obj_align(notification_label_, LV_ALIGN_CENTER, 0, 0);
    lv_obj_add_flag(notification_label_, LV_OBJ_FLAG_HIDDEN);

    status_label_ = lv_label_create(status_bar_);
    lv_obj_set_width(status_label_, LV_HOR_RES * 0.8);
    lv_label_set_long_mode(status_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_set_style_text_align(status_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(status_label_, lvgl_theme->text_color(), 0);
    lv_label_set_text(status_label_, Lang::Strings::INITIALIZING);
    lv_obj_align(status_label_, LV_ALIGN_CENTER, 0, 0);
    
    /* Content - Chat area */
    content_ = lv_obj_create(container_);
    lv_obj_set_style_radius(content_, 0, 0);
    lv_obj_set_width(content_, LV_HOR_RES);
    lv_obj_set_flex_grow(content_, 1);
    lv_obj_set_style_pad_all(content_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_border_width(content_, 0, 0);
    lv_obj_set_style_bg_color(content_, lvgl_theme->chat_background_color(), 0); // Background for chat area

    // Enable scrolling for chat content
    lv_obj_set_scrollbar_mode(content_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_scroll_dir(content_, LV_DIR_VER);
    
    // Create a flex container for chat messages
    lv_obj_set_flex_flow(content_, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(content_, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_row(content_, lvgl_theme->spacing(4), 0); // Space between messages

    // We'll create chat messages dynamically in SetChatMessage
    chat_message_label_ = nullptr;

    low_battery_popup_ = lv_obj_create(screen);
    lv_obj_set_scrollbar_mode(low_battery_popup_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_size(low_battery_popup_, LV_HOR_RES * 0.9, text_font->line_height * 2);
    lv_obj_align(low_battery_popup_, LV_ALIGN_BOTTOM_MID, 0, -lvgl_theme->spacing(4));
    lv_obj_set_style_bg_color(low_battery_popup_, lvgl_theme->low_battery_color(), 0);
    lv_obj_set_style_radius(low_battery_popup_, lvgl_theme->spacing(4), 0);
    low_battery_label_ = lv_label_create(low_battery_popup_);
    lv_label_set_text(low_battery_label_, Lang::Strings::BATTERY_NEED_CHARGE);
    lv_obj_set_style_text_color(low_battery_label_, lv_color_white(), 0);
    lv_obj_center(low_battery_label_);
    lv_obj_add_flag(low_battery_popup_, LV_OBJ_FLAG_HIDDEN);

    emoji_image_ = lv_img_create(screen);
    lv_obj_align(emoji_image_, LV_ALIGN_TOP_MID, 0, text_font->line_height + lvgl_theme->spacing(8));

    // Display AI logo while booting
    emoji_label_ = lv_label_create(screen);
    lv_obj_center(emoji_label_);
    lv_obj_set_style_text_font(emoji_label_, large_icon_font, 0);
    lv_obj_set_style_text_color(emoji_label_, lvgl_theme->text_color(), 0);
    lv_label_set_text(emoji_label_, FONT_AWESOME_MICROCHIP_AI);

    StartAntiBurnInTimer();
}
#if CONFIG_IDF_TARGET_ESP32P4
#define  MAX_MESSAGES 40
#else
#define  MAX_MESSAGES 20
#endif
void LcdDisplay::SetChatMessage(const char* role, const char* content) {
    /* 有新字幕 = 界面正在变化 ⇒ 重置防烧屏空闲计时（见 StartAntiBurnInTimer） */
    last_ui_change_tick_ = lv_tick_get();
    DisplayLockGuard lock(this);
    if (content_ == nullptr) {
        return;
    }
    
    // Check if message count exceeds limit
    uint32_t child_count = lv_obj_get_child_cnt(content_);
    if (child_count >= MAX_MESSAGES) {
        // Delete the oldest message (first child object)
        lv_obj_t* first_child = lv_obj_get_child(content_, 0);
        lv_obj_t* last_child = lv_obj_get_child(content_, child_count - 1);
        if (first_child != nullptr) {
            lv_obj_del(first_child);
        }
        // Scroll to the last message immediately
        if (last_child != nullptr) {
            lv_obj_scroll_to_view_recursive(last_child, LV_ANIM_OFF);
        }
    }
    
    // Collapse system messages (if it's a system message, check if the last message is also a system message)
    if (strcmp(role, "system") == 0) {
        if (child_count > 0) {
            // Get the last message container
            lv_obj_t* last_container = lv_obj_get_child(content_, child_count - 1);
            if (last_container != nullptr && lv_obj_get_child_cnt(last_container) > 0) {
                // Get the bubble inside the container
                lv_obj_t* last_bubble = lv_obj_get_child(last_container, 0);
                if (last_bubble != nullptr) {
                    // Check if bubble type is system message
                    void* bubble_type_ptr = lv_obj_get_user_data(last_bubble);
                    if (bubble_type_ptr != nullptr && strcmp((const char*)bubble_type_ptr, "system") == 0) {
                        // If the last message is also a system message, delete it
                        lv_obj_del(last_container);
                    }
                }
            }
        }
    } else {
        // Hide the centered AI logo
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
    }

    // Avoid empty message boxes
    if(strlen(content) == 0) {
        return;
    }

    auto lvgl_theme = static_cast<LvglTheme*>(current_theme_);
    auto text_font = lvgl_theme->text_font()->font();

    // Create a message bubble
    lv_obj_t* msg_bubble = lv_obj_create(content_);
    lv_obj_set_style_radius(msg_bubble, 8, 0);
    lv_obj_set_scrollbar_mode(msg_bubble, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_style_border_width(msg_bubble, 0, 0);
    lv_obj_set_style_pad_all(msg_bubble, lvgl_theme->spacing(4), 0);

    // Create the message text
    lv_obj_t* msg_text = lv_label_create(msg_bubble);
    lv_label_set_text(msg_text, content);
    
    // Calculate actual text width
    lv_coord_t text_width = lv_txt_get_width(content, strlen(content), text_font, 0);

    // Calculate bubble width
    lv_coord_t max_width = LV_HOR_RES * 85 / 100 - 16;  // 85% of screen width
    lv_coord_t min_width = 20;  
    lv_coord_t bubble_width;
    
    // Ensure text width is not less than minimum width
    if (text_width < min_width) {
        text_width = min_width;
    }

    // If text width is less than max width, use text width
    if (text_width < max_width) {
        bubble_width = text_width; 
    } else {
        bubble_width = max_width;
    }
    
    // Set message text width
    lv_obj_set_width(msg_text, bubble_width);  // Subtract padding
    lv_label_set_long_mode(msg_text, LV_LABEL_LONG_WRAP);

    // Set bubble width
    lv_obj_set_width(msg_bubble, bubble_width);
    lv_obj_set_height(msg_bubble, LV_SIZE_CONTENT);

    // Set alignment and style based on message role
    if (strcmp(role, "user") == 0) {
        // User messages are right-aligned with green background
        lv_obj_set_style_bg_color(msg_bubble, lvgl_theme->user_bubble_color(), 0);
        lv_obj_set_style_bg_opa(msg_bubble, LV_OPA_70, 0);
        // Set text color for contrast
        lv_obj_set_style_text_color(msg_text, lvgl_theme->text_color(), 0);
        
        // Set custom attribute to mark bubble type
        lv_obj_set_user_data(msg_bubble, (void*)"user");
        
        // Set appropriate width for content
        lv_obj_set_width(msg_bubble, LV_SIZE_CONTENT);
        lv_obj_set_height(msg_bubble, LV_SIZE_CONTENT);
        
        // Don't grow
        lv_obj_set_style_flex_grow(msg_bubble, 0, 0);
    } else if (strcmp(role, "assistant") == 0) {
        // Assistant messages are left-aligned with white background
        lv_obj_set_style_bg_color(msg_bubble, lvgl_theme->assistant_bubble_color(), 0);
        lv_obj_set_style_bg_opa(msg_bubble, LV_OPA_70, 0);
        // Set text color for contrast
        lv_obj_set_style_text_color(msg_text, lvgl_theme->text_color(), 0);
        
        // Set custom attribute to mark bubble type
        lv_obj_set_user_data(msg_bubble, (void*)"assistant");
        
        // Set appropriate width for content
        lv_obj_set_width(msg_bubble, LV_SIZE_CONTENT);
        lv_obj_set_height(msg_bubble, LV_SIZE_CONTENT);
        
        // Don't grow
        lv_obj_set_style_flex_grow(msg_bubble, 0, 0);
    } else if (strcmp(role, "system") == 0) {
        // System messages are center-aligned with light gray background
        lv_obj_set_style_bg_color(msg_bubble, lvgl_theme->system_bubble_color(), 0);
        lv_obj_set_style_bg_opa(msg_bubble, LV_OPA_70, 0);
        // Set text color for contrast
        lv_obj_set_style_text_color(msg_text, lvgl_theme->system_text_color(), 0);
        
        // Set custom attribute to mark bubble type
        lv_obj_set_user_data(msg_bubble, (void*)"system");
        
        // Set appropriate width for content
        lv_obj_set_width(msg_bubble, LV_SIZE_CONTENT);
        lv_obj_set_height(msg_bubble, LV_SIZE_CONTENT);
        
        // Don't grow
        lv_obj_set_style_flex_grow(msg_bubble, 0, 0);
    }
    
    // Create a full-width container for user messages to ensure right alignment
    if (strcmp(role, "user") == 0) {
        // Create a full-width container
        lv_obj_t* container = lv_obj_create(content_);
        lv_obj_set_width(container, LV_HOR_RES);
        lv_obj_set_height(container, LV_SIZE_CONTENT);
        
        // Make container transparent and borderless
        lv_obj_set_style_bg_opa(container, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(container, 0, 0);
        lv_obj_set_style_pad_all(container, 0, 0);
        
        // Move the message bubble into this container
        lv_obj_set_parent(msg_bubble, container);
        
        // Right align the bubble in the container
        lv_obj_align(msg_bubble, LV_ALIGN_RIGHT_MID, -25, 0);
        
        // Auto-scroll to this container
        lv_obj_scroll_to_view_recursive(container, LV_ANIM_ON);
    } else if (strcmp(role, "system") == 0) {
        // Create full-width container for system messages to ensure center alignment
        lv_obj_t* container = lv_obj_create(content_);
        lv_obj_set_width(container, LV_HOR_RES);
        lv_obj_set_height(container, LV_SIZE_CONTENT);
        
        lv_obj_set_style_bg_opa(container, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(container, 0, 0);
        lv_obj_set_style_pad_all(container, 0, 0);
        
        lv_obj_set_parent(msg_bubble, container);
        lv_obj_align(msg_bubble, LV_ALIGN_CENTER, 0, 0);
        lv_obj_scroll_to_view_recursive(container, LV_ANIM_ON);
    } else {
        // For assistant messages
        // Left align assistant messages
        lv_obj_align(msg_bubble, LV_ALIGN_LEFT_MID, 0, 0);

        // Auto-scroll to the message bubble
        lv_obj_scroll_to_view_recursive(msg_bubble, LV_ANIM_ON);
    }
    
    // Store reference to the latest message label
    chat_message_label_ = msg_text;
}

void LcdDisplay::SetPreviewImage(std::unique_ptr<LvglImage> image) {
    DisplayLockGuard lock(this);
    if (content_ == nullptr) {
        return;
    }

    if (image == nullptr) {
        return;
    }
    
    auto lvgl_theme = static_cast<LvglTheme*>(current_theme_);
    // Create a message bubble for image preview
    lv_obj_t* img_bubble = lv_obj_create(content_);
    lv_obj_set_style_radius(img_bubble, 8, 0);
    lv_obj_set_scrollbar_mode(img_bubble, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_style_border_width(img_bubble, 0, 0);
    lv_obj_set_style_pad_all(img_bubble, lvgl_theme->spacing(4), 0);
    
    // Set image bubble background color (similar to system message)
    lv_obj_set_style_bg_color(img_bubble, lvgl_theme->assistant_bubble_color(), 0);
    lv_obj_set_style_bg_opa(img_bubble, LV_OPA_70, 0);
    
    // Set custom attribute to mark bubble type
    lv_obj_set_user_data(img_bubble, (void*)"image");

    // Create the image object inside the bubble
    lv_obj_t* preview_image = lv_image_create(img_bubble);
    
    // Calculate appropriate size for the image
    lv_coord_t max_width = LV_HOR_RES * 70 / 100;  // 70% of screen width
    lv_coord_t max_height = LV_VER_RES * 50 / 100; // 50% of screen height
    
    // Calculate zoom factor to fit within maximum dimensions
    auto img_dsc = image->image_dsc();
    lv_coord_t img_width = img_dsc->header.w;
    lv_coord_t img_height = img_dsc->header.h;
    if (img_width == 0 || img_height == 0) {
        img_width = max_width;
        img_height = max_height;
        ESP_LOGW(TAG, "Invalid image dimensions: %ld x %ld, using default dimensions: %ld x %ld", img_width, img_height, max_width, max_height);
    }
    
    lv_coord_t zoom_w = (max_width * 256) / img_width;
    lv_coord_t zoom_h = (max_height * 256) / img_height;
    lv_coord_t zoom = (zoom_w < zoom_h) ? zoom_w : zoom_h;
    
    // Ensure zoom doesn't exceed 256 (100%)
    if (zoom > 256) zoom = 256;
    
    // Set image properties
    lv_image_set_src(preview_image, img_dsc);
    lv_image_set_scale(preview_image, zoom);
    
    // Add event handler to clean up LvglImage when image is deleted
    // We need to transfer ownership of the unique_ptr to the event callback
    LvglImage* raw_image = image.release(); // Release ownership of smart pointer
    lv_obj_add_event_cb(preview_image, [](lv_event_t* e) {
        LvglImage* img = (LvglImage*)lv_event_get_user_data(e);
        if (img != nullptr) {
            delete img; // Properly release memory by deleting LvglImage object
        }
    }, LV_EVENT_DELETE, (void*)raw_image);
    
    // Calculate actual scaled image dimensions
    lv_coord_t scaled_width = (img_width * zoom) / 256;
    lv_coord_t scaled_height = (img_height * zoom) / 256;
    
    // Set bubble size to be 16 pixels larger than the image (8 pixels on each side)
    lv_obj_set_width(img_bubble, scaled_width + 16);
    lv_obj_set_height(img_bubble, scaled_height + 16);
    
    // Don't grow in flex layout
    lv_obj_set_style_flex_grow(img_bubble, 0, 0);
    
    // Center the image within the bubble
    lv_obj_center(preview_image);
    
    // Left align the image bubble like assistant messages
    lv_obj_align(img_bubble, LV_ALIGN_LEFT_MID, 0, 0);

    // Auto-scroll to the image bubble
    lv_obj_scroll_to_view_recursive(img_bubble, LV_ANIM_ON);
}
#else
// >>> BEGIN elder-layout-design-20261103 (2026-11-03 按设计稿重排，勿手改；
// 被上游 sync 冲掉时 merge_to_xiaozhi.py --check 会红，按 README「上游定制补丁」节重贴) >>>
// 简洁风格布局（本板启用，320x240 横屏；config.h 已把 swap_xy/mirror 关掉）。
//
// 2026-11-03 起按 deliverables/gstack/design/mock-{1,2,3} 重排为**左右分栏**：
//
//    ┌──────────────────────────────────┐
//    │ 上午 08:30            (wifi)10月29日 周三 │ ← top_bar_ 藏青 30px
//    ├──────────────────────────────────┤
//    │ 用户的话（蓝 #4982FC）      ╭────╮ │ ← bottom_bar_ / user_box_（左列 x18 宽183）
//    │ 小安的回答（深 #232F54）    │头像│ │    emoji_box_（右侧 90x90 圆）
//    │                            ╰────╯ │
//    └──────────────────────────────────┘
//
// 待机态：头像圆整块隐藏，表情按 60px 从屏幕右缘外滚到左缘外（跑马灯）。
// 提醒态：reminder_overlay_ 盖住状态栏以下的 210px，画成结构化卡片。
//
// 沿革：2026-10-19 用户拍板过「上下 70/30 分割」（字幕占 70%、右下角 30%
// 放表情），2026-11-03 用户按设计稿改成上面的左右分栏 —— 旧的 70/30 已经不
// 存在了。**标记名跟着改**（原 `elder-layout-70-30`）：名字留着不改，
// 下一个读到这行的人会以为屏上还是上下分割，而 merge --check 只 grep 这个
// 标记，标记说错了它照样绿。
// 改动只涉及默认风格分支；WECHAT 气泡风格（CONFIG_USE_WECHAT_MESSAGE_STYLE，
// 本板未启用）不动。
// <<< END elder-layout-design-20261103 >>>
void LcdDisplay::SetupUI() {
    DisplayLockGuard lock(this);
    LvglTheme* lvgl_theme = static_cast<LvglTheme*>(current_theme_);
    auto text_font = lvgl_theme->text_font()->font();
    auto icon_font = lvgl_theme->icon_font()->font();
    auto large_icon_font = lvgl_theme->large_icon_font()->font();

    /* 对话区字号（2026-11-03 适老化：老人反馈左列文字太小）。
     *
     * 用 assets 里单独打包的大号字体（本板 30px，line_height 38），**不**直接
     * 把 text_font 换大：状态栏只有 30px 高，30px 字体放进去上下各切 4px。
     *
     * 没打包 large 字体时（其他板子）回落到 text_font —— 这里绝不能假定
     * large_text_font() 非空，否则换块板子就是空指针解引用。 */
    auto chat_font = text_font;
    if (lvgl_theme->large_text_font() != nullptr &&
        lvgl_theme->large_text_font()->font() != nullptr) {
        chat_font = lvgl_theme->large_text_font()->font();
        ESP_LOGI(TAG, "对话区使用大号字体 line_height=%d（常规 %d）",
                 (int)chat_font->line_height, (int)text_font->line_height);
    }

    /* ---- 字幕不显示定因诊断（2026-10-22）----
     * 打印字体指针、line_height、以及用「待」字做一次真实 glyph 查表。
     * 如果 get_glyph_dsc 返回 false，就是字库/struct 布局问题；
     * 如果返回 true 但 line_height=0，是描述符损坏。 */
    {
        lv_font_glyph_dsc_t gd;
        bool ok = false;
        if (text_font != nullptr && text_font->get_glyph_dsc != nullptr) {
            ok = text_font->get_glyph_dsc(text_font, &gd, 0x5F85 /* 待 */, 0);
        }
        ESP_LOGW("FontDiag", "text_font=%p lh=%d bl=%d glyph_dsc_fn=%p bitmap_fn=%p",
                 (const void*)text_font,
                 text_font ? text_font->line_height : -1,
                 text_font ? text_font->base_line : -1,
                 text_font ? (const void*)text_font->get_glyph_dsc : nullptr,
                 text_font ? (const void*)text_font->get_glyph_bitmap : nullptr);
        ESP_LOGW("FontDiag", "test glyph U+5F85(待): ok=%d adv_w=%d box=%dx%d ofs=%d,%d",
                 (int)ok, (int)gd.adv_w, (int)gd.box_w, (int)gd.box_h,
                 (int)gd.ofs_x, (int)gd.ofs_y);
    }

    auto screen = lv_screen_active();
    lv_obj_set_style_text_font(screen, text_font, 0);
    lv_obj_set_style_text_color(screen, lvgl_theme->text_color(), 0);
    lv_obj_set_style_bg_color(screen, lvgl_theme->background_color(), 0);
    /* 主区底色必须**不透明**：设计稿的 #D3DBFC 是实底。只设 bg_color 不设
     * bg_opa 时，screen 用的是 LVGL 默认的不透明度，而 container_ 铺在上面
     * 同样只有 bg_color —— 一旦哪一层被设成半透明，底下的黑屏就会透上来，
     * 整屏发灰。两层都显式 COVER，结论不依赖默认值。 */
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);

    /* screen 必须**不可滚动**（2026-11-03 真机反馈：待机跑马灯时屏幕底部
     * 有一条小的左右滚动条）。
     *
     * 根因：跑马灯的两张 lv_image 是 screen 的**直接子对象**，x 从 LV_HOR_RES
     * 一路走到 -img_w —— 它们的包围盒超出 screen 的内容区。LVGL 的 screen
     * 默认带 LV_OBJ_FLAG_SCROLLABLE，一旦子对象超出范围就把自己变成可滚动
     * 容器，并在底部画一条水平滚动条。
     *
     * 之前没暴露是因为跑马灯挂在 emoji_box_ 底下（那个对象自己清过
     * SCROLLABLE）；2026-11-03 改成 screen 的直接子对象（为了跨全屏滚）之后
     * 才冒出来。滚动条不在任何设计稿里，而且每滚一轮闪一次。
     *
     * ⚠️ 只清 SCROLLABLE 不够，还要把 scrollbar_mode 关掉：LVGL 在内容变化时
     * 会按 mode 重新决定是否画条，MODE_OFF 才是"永远不画"。 */
    lv_obj_clear_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scrollbar_mode(screen, LV_SCROLLBAR_MODE_OFF);

    /* Container - used as background */
    container_ = lv_obj_create(screen);
    lv_obj_set_size(container_, LV_HOR_RES, LV_VER_RES);
    lv_obj_set_style_radius(container_, 0, 0);
    lv_obj_set_style_pad_all(container_, 0, 0);
    lv_obj_set_style_border_width(container_, 0, 0);
    lv_obj_set_style_bg_color(container_, lvgl_theme->background_color(), 0);
    lv_obj_set_style_bg_opa(container_, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(container_, lvgl_theme->border_color(), 0);
    lv_obj_clear_flag(container_, LV_OBJ_FLAG_SCROLLABLE);

    /* Center layer: emoji_box_ - 右侧头像圆（2026-11-03 设计稿）
     *
     * 布局来源：mock-2-chat / mock-1-reminder 里右侧那个 90x90 的蓝紫圆
     * （实测包围盒 (210,74)-(298,162)，圆心 (254,118)）。
     *
     * 与 2026-10-20 那版「屏幕正中 42%x46% 大图区」的区别：
     *   · 位置从正中挪到右侧 —— 设计稿的左边整列要留给对话文字；
     *   · 尺寸从 134x110 缩到 90x90 —— 它是"头像"不是"脸占满半屏"；
     *   · 圆角改成 LV_RADIUS_CIRCLE + 圆底 #7B93E8，表情图裁在圆内。
     *
     * ⚠️ 待机跑马灯**不再**挂在这个对象下面（见 StartIdleMarquee）：
     * 跑马灯要横跨整屏，而这个圆只有 90px 宽，挂在里面就只能在圆内滚 ——
     * 这正是 2026-11-03 用户实报「没有从全屏右侧进入、左侧出」的成因。
     *
     * 2026-11-04：曾把它缩到 52px 换 242px 列宽，用户实报「表情图标变小了，
     * 位置也变了」⇒ **已全部撤销**，回到设计稿的 90px / 右边距 21px。 */
    const int avatar_sz = AvatarSize();
    emoji_box_ = lv_obj_create(screen);
    lv_obj_set_size(emoji_box_, avatar_sz, avatar_sz);
    lv_obj_set_style_radius(emoji_box_, LV_RADIUS_CIRCLE, 0);
    /* clip_corner：让子对象（表情图）被圆的轮廓裁掉。不加这一条，表情的
     * 四个角会露出圆外，看着像"方块上贴了个圆"。 */
    lv_obj_set_style_clip_corner(emoji_box_, true, 0);
    lv_obj_set_style_bg_color(emoji_box_, lv_color_hex(kColorAvatar), 0);
    lv_obj_set_style_bg_opa(emoji_box_, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_all(emoji_box_, 0, 0);
    lv_obj_set_style_border_width(emoji_box_, 0, 0);
    lv_obj_clear_flag(emoji_box_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_align(emoji_box_, LV_ALIGN_TOP_RIGHT, -AvatarRight(), AvatarTop());

    emoji_label_ = lv_label_create(emoji_box_);
    lv_obj_set_style_text_font(emoji_label_, large_icon_font, 0);
    lv_obj_set_style_text_color(emoji_label_, lvgl_theme->text_color(), 0);
    lv_label_set_text(emoji_label_, FONT_AWESOME_MICROCHIP_AI);
    lv_obj_center(emoji_label_);

    emoji_image_ = lv_img_create(emoji_box_);
    // 表情图（fluent3d cbin）铺满头像圆 + CONTAIN 等比缩放
    lv_obj_set_size(emoji_image_, lv_pct(100), lv_pct(100));
    lv_image_set_inner_align(emoji_image_, LV_IMAGE_ALIGN_CONTAIN);
    lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);

    /* Middle layer: preview_image_ - 与头像圆同一块区域（预览图顶替头像） */
    preview_image_ = lv_image_create(screen);
    lv_obj_set_size(preview_image_, avatar_sz, avatar_sz);
    lv_image_set_inner_align(preview_image_, LV_IMAGE_ALIGN_CONTAIN);
    lv_obj_align(preview_image_, LV_ALIGN_TOP_RIGHT, -AvatarRight(), AvatarTop());
    lv_obj_add_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);

    /* Layer 1: Top bar —— 藏青状态栏（2026-11-03 设计稿）
     *
     * 实测规格：y 0..29（高 30px）、整宽、底色 #2A365B；
     * 左边「上午 08:30」，右边「wifi 图标 + 10月29日 周三」，两组各贴一侧。
     *
     * 与旧版的三处区别（旧版是 LV_OPA_50 半透明 + 时钟居中）：
     *   · 底色从"主区色 50% 透明"改成**实心藏青** —— 设计稿里状态栏与主区
     *     是两个分明的色块；半透明会让 #2A365B 和 #D3DBFC 混成一片灰蓝。
     *   · 高度**固定** 30px，不再是 LV_SIZE_CONTENT —— 内容一多就长高，
     *     会把下面的主区整体往下挤。
     *   · 时钟从左对齐（设计稿在左侧），不再居中。
     */
    top_bar_ = lv_obj_create(screen);
    lv_obj_set_size(top_bar_, LV_HOR_RES, StatusBarH());
    lv_obj_set_style_radius(top_bar_, 0, 0);
    lv_obj_set_style_bg_color(top_bar_, lv_color_hex(kColorStatusBar), 0);
    lv_obj_set_style_bg_opa(top_bar_, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(top_bar_, 0, 0);
    lv_obj_set_style_pad_all(top_bar_, 0, 0);
    lv_obj_set_style_pad_left(top_bar_, LV_HOR_RES * 8 / 320, 0);
    lv_obj_set_style_pad_right(top_bar_, LV_HOR_RES * 8 / 320, 0);
    lv_obj_set_flex_flow(top_bar_, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(top_bar_, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_scrollbar_mode(top_bar_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_clear_flag(top_bar_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_align(top_bar_, LV_ALIGN_TOP_MID, 0, 0);

    /* 左组：时钟（status_label_）+ 临时状态文案（notification_label_）。
     *
     * 复用 status_bar_ 这个成员当"左组容器"：它原本就是装这两个 label 的，
     * 只是以前铺满整屏、绝对定位、背景全透明。现在改成 flex 行里的一个
     * 自适应小盒子 —— 不用 LV_LAYOUT_NONE + 绝对定位，是因为那样两个 label
     * 会叠在同一位置，宽度还得各算一遍。 */
    status_bar_ = lv_obj_create(top_bar_);
    lv_obj_set_size(status_bar_, LV_HOR_RES * 34 / 100, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(status_bar_, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(status_bar_, 0, 0);
    lv_obj_set_style_pad_all(status_bar_, 0, 0);
    lv_obj_set_style_layout(status_bar_, LV_LAYOUT_NONE, 0);
    lv_obj_set_scrollbar_mode(status_bar_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_clear_flag(status_bar_, LV_OBJ_FLAG_SCROLLABLE);

    /* 时钟：左对齐、白字、超长打点（"上午 08:30" 实测约 95px，134px 的框
     * 足够；但状态文案可能很长，所以必须给个明确的截断模式，否则会撑破）。 */
    status_label_ = lv_label_create(status_bar_);
    lv_obj_set_width(status_label_, lv_pct(100));
    lv_label_set_long_mode(status_label_, LV_LABEL_LONG_DOT);
    lv_obj_set_style_text_align(status_label_, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_set_style_text_color(status_label_, lv_color_white(), 0);
    lv_obj_set_style_text_font(status_label_, text_font, 0);  /* 显式设，不靠继承 */
    lv_label_set_text(status_label_, Lang::Strings::INITIALIZING);
    lv_obj_align(status_label_, LV_ALIGN_LEFT_MID, 0, 0);

    /* 临时状态文案：与时钟**同位置**、同时只显示一个（ShowNotification 会把
     * status_label_ 藏起来，SetStatus 会把它放回来）。 */
    notification_label_ = lv_label_create(status_bar_);
    lv_obj_set_width(notification_label_, lv_pct(100));
    lv_label_set_long_mode(notification_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_set_style_text_align(notification_label_, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_set_style_text_color(notification_label_, lv_color_white(), 0);
    lv_obj_set_style_text_font(notification_label_, text_font, 0);
    lv_label_set_text(notification_label_, "");
    lv_obj_align(notification_label_, LV_ALIGN_LEFT_MID, 0, 0);
    lv_obj_add_flag(notification_label_, LV_OBJ_FLAG_HIDDEN);

    /* 右组：wifi / 静音 / 电量 / 日期（顺序即设计稿：图标在前、日期在后）
     *
     * ⚠️ 宽度**必须**是 flex_grow，不能是 LV_SIZE_CONTENT（2026-11-03 真机
     * 实测定案）。真机诊断日志：
     *     net_text=非空 net_w=25 net_h=23 right_w=0 right_x=304 bar_w=320
     * WiFi 图标文本设上了、label 本身也有 25x23，但它所在的 right_icons
     * 宽度是 **0** —— 整组被挤成一条线贴在右边缘，屏上什么都看不见。
     *
     * 根因：top_bar_ 是 LV_FLEX_ALIGN_SPACE_BETWEEN 的**行**，只有两个子对象
     * （左组 status_bar_ 定宽 34% + 右组 SIZE_CONTENT）。SIZE_CONTENT 的嵌套
     * flex 容器要等自己的子对象布局完才能算出宽度，而 SPACE_BETWEEN 在
     * 这一轮就要分配剩余空间 —— 量到 0 就按 0 分。而它内部子对象的总宽
     * （wifi 25 + 电量 29 + 日期 40%≈128 + 边距）其实远超 0。
     *
     * 用 flex_grow=1 而不是写死一个百分比：右组的宽度是"父内容宽 - 左组宽"
     * 这个确定值，与屏幕分辨率、左组 34% 的取值都不再耦合；LVGL 算 grow
     * 只做减法，不依赖子对象自己量出来的尺寸，所以不会重蹈 SIZE_CONTENT 的坑。
     * 组内仍是 LV_FLEX_ALIGN_END —— 图标贴着右边缘排。 */
    lv_obj_t* right_icons = lv_obj_create(top_bar_);
    lv_obj_set_size(right_icons, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_grow(right_icons, 1);
    lv_obj_set_style_bg_opa(right_icons, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(right_icons, 0, 0);
    lv_obj_set_style_pad_all(right_icons, 0, 0);
    lv_obj_set_flex_flow(right_icons, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(right_icons, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(right_icons, LV_OBJ_FLAG_SCROLLABLE);

    network_label_ = lv_label_create(right_icons);
    lv_label_set_text(network_label_, "");
    lv_obj_set_style_text_font(network_label_, icon_font, 0);
    lv_obj_set_style_text_color(network_label_, lv_color_white(), 0);

    mute_label_ = lv_label_create(right_icons);
    lv_label_set_text(mute_label_, "");
    lv_obj_set_style_text_font(mute_label_, icon_font, 0);
    lv_obj_set_style_text_color(mute_label_, lv_color_white(), 0);
    lv_obj_set_style_margin_left(mute_label_, lvgl_theme->spacing(2), 0);

    battery_label_ = lv_label_create(right_icons);
    lv_label_set_text(battery_label_, "");
    lv_obj_set_style_text_font(battery_label_, icon_font, 0);
    lv_obj_set_style_text_color(battery_label_, lv_color_white(), 0);
    lv_obj_set_style_margin_left(battery_label_, lvgl_theme->spacing(2), 0);

    /* 日期：「10月29日 周三」。文案由 SetStatusClock() 与时钟一起刷，
     * 这里只负责排版。宽度**必须**给死 —— 右侧组是 SIZE_CONTENT，
     * 日期又最长，不给宽度会让整条状态栏在"静音图标出现"时挤爆右边缘。 */
    date_label_ = lv_label_create(right_icons);
    lv_label_set_text(date_label_, "");
    lv_obj_set_width(date_label_, LV_HOR_RES * 40 / 100);
    lv_label_set_long_mode(date_label_, LV_LABEL_LONG_DOT);
    lv_obj_set_style_text_align(date_label_, LV_TEXT_ALIGN_RIGHT, 0);
    lv_obj_set_style_text_color(date_label_, lv_color_white(), 0);
    lv_obj_set_style_text_font(date_label_, text_font, 0);
    lv_obj_set_style_margin_left(date_label_, lvgl_theme->spacing(2), 0);

    /* Layer 3: bottom_bar_ —— 左列对话区（2026-11-03 设计稿）
     *
     * 设计稿的聆听/对话态是「左边一列文字 + 右边一个头像圆」的**左右分栏**：
     *   左列  x 18 起，宽到头像左边再留 8px（320 屏上是 183px）
     *   上行  用户刚说的话，蓝色 #4982FC
     *   下行  小安的回答，深色 #232F54
     *
     * 与 2026-10-20 那版「贴底 28% 高的居中字幕条」的区别：
     *   · 位置：贴底 → 主区左上（设计稿首行 y≈44）
     *   · 对齐：居中 → 左对齐（老人的视线从左往右扫，居中会让他找不到
     *     "从哪儿开始读"，而且每换一句起点都变）
     *   · 宽度：整屏 → 183px（右侧让给头像圆）
     *
     * ⚠️ 2026-11-04（用户第 1 条）：两段文字**不再共用一个容器**。
     *   `bottom_bar_` 从此只装设备字幕（chat_message_label_），用户话搬进
     *   同位置同尺寸的 `user_box_`，两个容器**互斥显示**（见 UpdateChatBoxes）。
     *   为什么必须这样：只要两段在同一个滚动容器里，只要滚动偏移不是精确的
     *   整页边界，就一定会同屏 —— 上一版靠"把用户话撑成整页整数倍 + 补尾部
     *   留白"去凑边界，任何一个环节算错（尤其是追加前量到的高度已被补过留白）
     *   就退回用户报的那个现象：第 2 页顶端露出用户话、设备字幕只剩一行半。
     *   拆成两个容器 + 互斥显示，是**结构性**地让它们不可能同屏。
     *
     * 成员名仍叫 bottom_bar_（基类清理逻辑与 SetHideSubtitle 都按这个名字
     * 引用），语义已变成「设备字幕容器」。
     */
    const int col_x = MainPadX();
    const int col_w = LV_HOR_RES - AvatarRight() - AvatarSize() - col_x
                      - LV_HOR_RES * 8 / 320;
    const int col_y = TextTop();
    const int col_h = LV_VER_RES - TextTop() - LV_VER_RES * 8 / 240;

    bottom_bar_ = lv_obj_create(screen);
    lv_obj_set_pos(bottom_bar_, col_x, col_y);
    lv_obj_set_size(bottom_bar_, col_w, col_h);
    lv_obj_set_style_radius(bottom_bar_, 0, 0);
    /* 透明底：主区底色 #D3DBFC 由 screen/container_ 提供。这里再刷一层
     * 同色不透明底会和圆角/边框叠出接缝，而且 SetTheme 里那段 50% 透明度
     * 的老逻辑会让整列发灰。 */
    lv_obj_set_style_bg_opa(bottom_bar_, LV_OPA_TRANSP, 0);
    lv_obj_set_style_text_color(bottom_bar_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_pad_all(bottom_bar_, 0, 0);
    lv_obj_set_style_border_width(bottom_bar_, 0, 0);
    /* 可**纵向**滚动：一页放不下时由 ChatPageStep() 按页滚（2026-11-03
     * 用户需求「一屏显示不完全就自动翻页」）。
     *
     * 三件事缺一不可，缺哪一件的表现都不一样：
     *   · SCROLLABLE 不清（以前这里是 clear_flag）—— 清掉就根本滚不动；
     *   · 滚动方向限死纵向 —— 不限的话长句子横向也能拖，老人会以为界面乱了；
     *   · scrollbar 必须 OFF —— 屏只有 320x240，右侧那条 4px 的滚动条会压掉
     *     半个汉字，而且老人不认识"滚动条"这个东西。
     * 注意这与 screen 那一层是**两回事**：screen 上禁滚动是为了待机跑马灯的
     * 两张图（它们的包围盒故意超出屏幕），见 SetupUI 开头的说明。 */
    lv_obj_set_scroll_dir(bottom_bar_, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(bottom_bar_, LV_SCROLLBAR_MODE_OFF);
    /* 容器里**只放一个** label（设备字幕），所以不需要 flex、也不需要
     * pad_row：上一版那 6px 行间距是"用户话 + 回答同容器"才需要的，现在
     * 两段各在自己的容器里，页高换算里也不该再出现第二段的高度。 */

    /* 小安的回答（深色）。SetChatMessage 的 assistant / system 都写这里。 */
    chat_message_label_ = lv_label_create(bottom_bar_);
    lv_obj_set_width(chat_message_label_, lv_pct(100));
    lv_label_set_long_mode(chat_message_label_, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_align(chat_message_label_, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_set_style_text_color(chat_message_label_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_text_font(chat_message_label_, chat_font, 0);  /* 显式设，不靠继承 */
    lv_obj_set_style_text_line_space(chat_message_label_, LV_VER_RES * 4 / 240, 0);
    lv_label_set_text(chat_message_label_, "");

    /* ---- user_box_：用户话的**独立**容器（2026-11-04 用户第 1 条）----
     *
     * 位置、尺寸、滚动方式与 bottom_bar_ **逐像素相同**（同一块左列区域），
     * 只是里面装的是另一个 label。两个容器互斥显示（UpdateChatBoxes），
     * 所以同一时刻屏上只有一段文字 —— 这是"用户字幕与设备字幕不可能同屏"
     * 的**结构性**保证，不再依赖"把高度凑成整页边界"那种算术。
     *
     * 为什么用户话（蓝）要单独一个容器，而不是继续和回答共用一个：
     *   共用时只能靠滚动偏移把两段分开，而偏移只要差一像素，第 2 页顶端就会
     *   露出上一段的最后一行；上一版为此补了"撑高 + 尾部留白"两层算术，
     *   仍然被"追加前量到的高度已被补过留白"打穿（见 ChatPageLayout 的说明）。
     *
     * 用户话空串时**隐藏整个容器**：留一个空 label 会让 flex/滚动余量多算
     * 一截，翻页时多出一次"什么都没变"的空白页。 */
    user_box_ = lv_obj_create(screen);
    lv_obj_set_pos(user_box_, col_x, col_y);
    lv_obj_set_size(user_box_, col_w, col_h);
    lv_obj_set_style_radius(user_box_, 0, 0);
    lv_obj_set_style_bg_opa(user_box_, LV_OPA_TRANSP, 0);
    lv_obj_set_style_text_color(user_box_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_pad_all(user_box_, 0, 0);
    lv_obj_set_style_border_width(user_box_, 0, 0);
    lv_obj_set_scroll_dir(user_box_, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(user_box_, LV_SCROLLBAR_MODE_OFF);

    /* 用户话（蓝）。 */
    chat_user_label_ = lv_label_create(user_box_);
    lv_obj_set_width(chat_user_label_, lv_pct(100));
    lv_label_set_long_mode(chat_user_label_, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_align(chat_user_label_, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_set_style_text_color(chat_user_label_, lv_color_hex(kColorTextUser), 0);
    lv_obj_set_style_text_font(chat_user_label_, chat_font, 0);
    lv_obj_set_style_text_line_space(chat_user_label_, LV_VER_RES * 4 / 240, 0);
    lv_label_set_text(chat_user_label_, "");
    lv_obj_add_flag(chat_user_label_, LV_OBJ_FLAG_HIDDEN);
    /* 初始两个容器都藏起来：开机时没有任何字幕，露出空容器没有意义。 */
    lv_obj_add_flag(user_box_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);

    low_battery_popup_ = lv_obj_create(screen);
    lv_obj_set_scrollbar_mode(low_battery_popup_, LV_SCROLLBAR_MODE_OFF);
    // 左下角 65% 宽：避开右下角 30% 图片区（2026-10-19 布局，原来 90% 居中会压图）
    lv_obj_set_size(low_battery_popup_, LV_HOR_RES * 0.65, text_font->line_height * 2);
    lv_obj_align(low_battery_popup_, LV_ALIGN_BOTTOM_LEFT, lvgl_theme->spacing(4), -lvgl_theme->spacing(4));
    lv_obj_set_style_bg_color(low_battery_popup_, lvgl_theme->low_battery_color(), 0);
    lv_obj_set_style_radius(low_battery_popup_, lvgl_theme->spacing(4), 0);
    
    low_battery_label_ = lv_label_create(low_battery_popup_);
    lv_label_set_text(low_battery_label_, Lang::Strings::BATTERY_NEED_CHARGE);
    lv_obj_set_style_text_color(low_battery_label_, lv_color_white(), 0);
    lv_obj_center(low_battery_label_);
    lv_obj_add_flag(low_battery_popup_, LV_OBJ_FLAG_HIDDEN);

    StartAntiBurnInTimer();
}

void LcdDisplay::SetPreviewImage(std::unique_ptr<LvglImage> image) {
    DisplayLockGuard lock(this);
    if (preview_image_ == nullptr) {
        ESP_LOGE(TAG, "Preview image is not initialized");
        return;
    }

    if (image == nullptr) {
        esp_timer_stop(preview_timer_);
        lv_obj_remove_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);
        preview_image_cached_.reset();
        if (gif_controller_) {
            gif_controller_->Start();
        }
        return;
    }

    preview_image_cached_ = std::move(image);
    auto img_dsc = preview_image_cached_->image_dsc();
    lv_image_set_src(preview_image_, img_dsc);
    // 缩放由 lv_image 的 LV_IMAGE_ALIGN_CONTAIN 自动完成（set_src 内部
    // update_align 按图片区 30%x30% 重算，等比缩放、永不溢出）——
    // 删掉了原先手算的 128*width_/w（那是在整屏居中的旧布局下按半屏宽算的）

    // Hide emoji_box_
    if (gif_controller_) {
        gif_controller_->Stop();
    }
    lv_obj_add_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);
    esp_timer_stop(preview_timer_);
    ESP_ERROR_CHECK(esp_timer_start_once(preview_timer_, PREVIEW_IMAGE_DURATION_MS * 1000));
}

/* ================= 对话分页（2026-11-03 起，2026-11-04 改成双容器） =================
 *
 * 用户原话（2026-11-03）：「对话状态中，用户说的话和 AI 说的话文字放在同一页，
 * 一屏显示不完全。把用户说的话和 AI 说的话放在不同的两页，如果一页显示不全，
 * 自动翻页」。
 * 用户原话（2026-11-04）：「多行的时候显示不完整，也没有把用户说的字幕和设备的
 * 字幕分到不同页面，字幕刷新率很低，完全跟不上语音」。
 * 用户原话（2026-11-04，第二版实测）：「字幕在第一屏显示的时候是分屏的，但是
 * 当播报的语音超出一屏，第二屏显示的时候就又变成了上面是用户的，下面是语音的
 * 字幕，而且只显示语音字幕的前一行半。」
 *
 * 为什么上一版"凑整页边界"必然失败（记下来，别再走回头路）：
 *   上一版两段文字在**同一个**滚动容器里，靠"把用户话撑成 page_h 的整数倍 +
 *   给最后一段补尾部留白 + 滚到 idx*page_h"去保证第 2 页顶端正好是回答的第一
 *   行。这是纯算术保证，任何一处偏差都会塌：SetChatMessage 里量"已有几行"时
 *   量的是 label 高度，而那个高度上一轮已被 ChatPageLayout 补过尾部留白 ⇒
 *   行数偏大、落点算到后面去；视口 188 与页高 168 差 20px 又让滚动上限夹住
 *   最后一页。现象就是用户描述的那一屏：顶端露出用户话，设备字幕只剩 1.5 行。
 *
 * 现在的结构（结构性保证，不再依赖算术）：
 *   · bottom_bar_ 只装 chat_message_label_（设备字幕）；
 *   · user_box_   只装 chat_user_label_（用户话），位置尺寸与 bottom_bar_ 相同；
 *   · 两者**互斥显示**（UpdateChatBoxes）：同一时刻屏上只有一段文字。
 *   · 分页按**各自容器**算：页数只用一个 label 的高度，滚动位置 = 页号 × 页高。
 *
 * 页高为什么取"整行倍数"：容器高 188px，行高 42px，只能放 4 行（168px）。
 * 按 188 滚，第二页顶端会从第 5 行的中间切开 —— 老人看到半截字。
 */

/* 每页停留多久。4 行 30px 汉字，老人逐行读大约 3~5 秒；给 6 秒留余量。
 * 太短等于催他，太长他会以为设备卡住了。 */
#define CHAT_PAGE_DWELL_MS  6000

int LcdDisplay::ChatLineH() {
    /* 字体行高随主题变（20px 小字库 line_height=25 / 30px 大字库 38），
     * 所以**每次现算**，不能缓存 —— 缓存的话换一次主题，分页就按旧行高切，
     * 表现是"页边界压着半行字"。 */
    const lv_font_t* f = nullptr;
    if (chat_message_label_ != nullptr) {
        f = lv_obj_get_style_text_font(chat_message_label_, LV_PART_MAIN);
    }
    const int lh = (f != nullptr) ? (int)f->line_height : 25;
    /* 行间距与创建 label 时的取值同源（LV_VER_RES * 4 / 240）。 */
    return lh + (LV_VER_RES * 4 / 240);
}

int LcdDisplay::ChatPageH() {
    if (bottom_bar_ == nullptr) return 0;
    /* 两个容器尺寸逐像素相同（见 SetupUI），用哪个量都一样。 */
    const int avail = (int)lv_obj_get_height(bottom_bar_);
    const int line_h = ChatLineH();
    if (avail <= 0 || line_h <= 0) return 0;
    int lines = avail / line_h;
    if (lines < 1) lines = 1;
    return lines * line_h;
}

/* 当前该分页的那个容器。
 *
 * 设备字幕优先：老人在等答案，回答一出就该占屏；用户话只在"还没有回答"的
 * 那段时间单独占屏（他刚说完、设备在想）。两者都空时返回 bottom_bar_（空
 * 容器，分页算法在上面退化成 1 页，无副作用）。 */
lv_obj_t* LcdDisplay::ChatActiveBox() {
    if (chat_message_label_ != nullptr) {
        const char* m = lv_label_get_text(chat_message_label_);
        if (m != nullptr && m[0] != '\0') return bottom_bar_;
    }
    if (chat_user_label_ != nullptr) {
        const char* u = lv_label_get_text(chat_user_label_);
        if (u != nullptr && u[0] != '\0') return user_box_;
    }
    return bottom_bar_;
}

/* 两个容器**互斥显示** —— 这是"用户字幕与设备字幕不可能同屏"的唯一保证。
 *
 * 三条分支，缺一不可：
 *   ① 有设备字幕 ⇒ 显示 bottom_bar_、隐藏 user_box_（设备字幕优先：老人正在
 *      等答案，用户话留在屏上会把答案挤走 —— 上一版的"同屏"就是这么来的）；
 *   ② 只有用户话 ⇒ 显示 user_box_、隐藏 bottom_bar_；
 *   ③ 都空 ⇒ 两个都藏（露出空容器等于屏上凭空多一块可滚区域）。
 * 另外 hide_subtitle_（设置项）为真时一律两个都藏 —— 否则"关了字幕"之后
 * 用户话还挂在屏上（SetHideSubtitle 的注释里记着这条）。 */
void LcdDisplay::UpdateChatBoxes() {
    if (bottom_bar_ == nullptr || user_box_ == nullptr) return;
    if (hide_subtitle_) {
        lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(user_box_, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    const char* u = (chat_user_label_ != nullptr) ? lv_label_get_text(chat_user_label_) : nullptr;
    const char* m = (chat_message_label_ != nullptr) ? lv_label_get_text(chat_message_label_) : nullptr;
    const bool has_user = (u != nullptr && u[0] != '\0');
    const bool has_msg = (m != nullptr && m[0] != '\0');
    if (has_msg) {
        lv_obj_remove_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(user_box_, LV_OBJ_FLAG_HIDDEN);
    } else if (has_user) {
        lv_obj_remove_flag(user_box_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(user_box_, LV_OBJ_FLAG_HIDDEN);
    }
}

void LcdDisplay::ChatPageLayout(int anchor_px) {
    if (bottom_bar_ == nullptr || user_box_ == nullptr) return;
    if (chat_user_label_ == nullptr || chat_message_label_ == nullptr) return;

    const int page_h = ChatPageH();
    if (page_h <= 0) return;

    /* 先定"这一屏给谁"，再给这个容器分页 —— 顺序不能反：容器还藏着的时候
     * 量出来的高度不可信（隐藏对象不参与布局刷新）。 */
    UpdateChatBoxes();
    if (hide_subtitle_) {
        if (chat_page_timer_ != nullptr) lv_timer_pause(chat_page_timer_);
        return;
    }

    lv_obj_t* box = ChatActiveBox();
    lv_obj_t* label = (box == user_box_) ? chat_user_label_ : chat_message_label_;
    if (box == nullptr || label == nullptr) return;

    /* 先回到自然高度再量 —— 上一轮它可能被补过尾部留白（见下面的 pad_target）。 */
    lv_obj_set_height(label, LV_SIZE_CONTENT);
    lv_obj_update_layout(box);

    /* 页数 + 尾部留白，必须按**同一个口径**算（这里踩过一次）。
     *
     * 视口 188px 比一页 168px 高 20px ⇒ LVGL 的滚动上限是
     * "内容高 - 视口高"，最后一页只要内容比它少 20px 就滚不到页边界，
     * 滚动位置被夹住，屏上就会露出上一页的尾巴。
     * 所以：页数按 (内容 - 20) 算，再给这一段补足这 20px 空白
     * （label 背景透明，屏上什么也看不见，只是把内容撑够）。
     * 补完的内容高恰好 = (pages-1)*page_h + 视口高 ⇒ 每个页起点都能精确滚到。
     *
     * ⚠️ 这里的 natural 只含**一个** label 的高度。上一版是 user_block + bot_h
     * （两段相加），那正是"同屏"的来源 —— 两段一旦在同一个容器里，无论怎么
     * 相加都只是把误差挪个位置。 */
    const int avail = (int)lv_obj_get_height(box);
    const int excess = (avail > page_h) ? (avail - page_h) : 0;
    const int natural = (int)lv_obj_get_height(label);
    int pages = 1;
    if (natural > 0) {
        pages = (natural - excess + page_h - 1) / page_h;
        if (pages < 1) pages = 1;
        const int pad_target = (pages - 1) * page_h + avail;
        if (pad_target > natural) {
            lv_obj_set_height(label, pad_target);
            lv_obj_update_layout(box);
        }
    }

    /* 滚动余量由 LVGL 现算：get_scroll_bottom() = 内容高 - 容器高（还能往下
     * 滚多少像素）。自己累加高度会在"换主题改行距"这些情况下算错。 */
    int max_scroll = (int)lv_obj_get_scroll_bottom(box);
    if (max_scroll < 0) max_scroll = 0;
    const int total = (int)lv_obj_get_height(box) + max_scroll;

    chat_page_count_ = pages;
    chat_page_h_ = page_h;
    chat_page_max_scroll_ = max_scroll;
    chat_page_box_ = box;

    /* 落点 = **本句第一行所在的那一页**，不是最后一页（2026-11-04 用户第 2、3 条）。
     *
     * 老实现是 `chat_page_index_ = pages - 1`：一句 30 字的回答刚追加完，屏就
     * 跳到末尾那页，而声音才刚开始念第一句 —— 老人看到的是"念的和写的对不上"，
     * 而且前面几行**再也没有机会出现**（follow 期间还不许翻页）。改成按
     * anchor_px 换算：语音念到哪一句，屏就从那一句的第一行开始，之后每
     * CHAT_PAGE_DWELL_MS 往下翻一页，翻到最后一页停住。
     *
     * anchor_px 是"本句第一行在该 label 内的 y"。容器里只有这一个 label，
     * 所以不再需要加"上一段占掉的高度"（user_block 已随双容器一起删除）。 */
    int idx = 0;
    if (anchor_px >= 0) {
        idx = anchor_px / page_h;
        if (idx >= pages) idx = pages - 1;   /* 越界只可能来自取整，夹住 */
        if (idx < 0) idx = 0;
    }
    chat_page_index_ = idx;
    int start_y = chat_page_index_ * page_h;
    if (start_y > max_scroll) start_y = max_scroll;   /* 最后一页滚不满 */
    lv_obj_scroll_to_y(box, start_y, LV_ANIM_OFF);

    if (pages > 1) {
        if (chat_page_timer_ == nullptr) {
            /* lv_timer 回调必须是裸函数指针，用 user_data 带回 this
             * （与防烧屏定时器同一写法）。 */
            chat_page_timer_ = lv_timer_create([](lv_timer_t* t) {
                static_cast<LcdDisplay*>(lv_timer_get_user_data(t))->ChatPageStep();
            }, CHAT_PAGE_DWELL_MS, this);
        }
        if (chat_page_timer_ != nullptr) {
            /* reset 让本页从"完整停留 6 秒"开始算：新字幕刚到就翻页，等于
             * 第一页根本没让人看。 */
            lv_timer_reset(chat_page_timer_);
            lv_timer_resume(chat_page_timer_);
        }
        ESP_LOGI(TAG, "对话分页：共 %d 页（页高 %d / 内容 %d / 视口 %d），停在第 %d 页%s",
                 pages, page_h, total, (int)lv_obj_get_height(box),
                 chat_page_index_ + 1,
                 (box == user_box_) ? "（用户话，设备字幕未到）" : "（设备字幕）");
    } else if (chat_page_timer_ != nullptr) {
        lv_timer_pause(chat_page_timer_);
    }
}

void LcdDisplay::ChatPageStep() {
    if (chat_page_box_ == nullptr || chat_page_count_ <= 1) {
        if (chat_page_timer_ != nullptr) lv_timer_pause(chat_page_timer_);
        return;
    }
    /* 这里**不许**再判"有字幕就早退"（2026-11-04）。
     *
     * 老实现是 `if (chat_follow_tail_) { ... return; }`：有新句期间翻页彻底
     * 停摆，而屏又被摆到最后一页 —— 老人永远只看到句子尾巴，前面的行再也没
     * 机会出现，这就是「多行显示不完整」的直接成因。
     * 现在定时器只管按页往前走：每来一句新字幕，ChatPageLayout 把落点重置到
     * 该句的起始页并 reset 本定时器；之后每 CHAT_PAGE_DWELL_MS 前进一页，
     * 走到最后一页停住。 */
    if (chat_page_index_ + 1 >= chat_page_count_) {
        /* 最后一页就**停住**，不循环、也不回第 1 页：
         *   · 老人正在读结论，翻走等于让他再也看不到；
         *   · 循环会一直重置 last_ui_change_tick_，防烧屏永远接管不了
         *     —— 屏上固定不动的高对比文字正是要防的那类残影。 */
        lv_timer_pause(chat_page_timer_);
        ESP_LOGI(TAG, "对话分页：已翻到最后一页（%d/%d），停住",
                 chat_page_index_ + 1, chat_page_count_);
        return;
    }
    chat_page_index_++;
    int y = chat_page_index_ * chat_page_h_;
    if (y > chat_page_max_scroll_) y = chat_page_max_scroll_;   /* 最后一页滚不满 */
    lv_obj_scroll_to_y(chat_page_box_, y, LV_ANIM_ON);
    /* 界面确实在变 ⇒ 推迟屏保。只在真正翻页时重置，停住之后不再动，
     * 于是 60 秒静止后防烧屏能正常接管。 */
    last_ui_change_tick_ = lv_tick_get();
    ESP_LOGI(TAG, "对话分页：翻到第 %d/%d 页（y=%d）",
             chat_page_index_ + 1, chat_page_count_, y);
}

void LcdDisplay::SetChatMessage(const char* role, const char* content) {
    /* 有新字幕 = 界面正在变化 ⇒ 重置防烧屏空闲计时（见 StartAntiBurnInTimer） */
    last_ui_change_tick_ = lv_tick_get();
    DisplayLockGuard lock(this);
    if (chat_message_label_ == nullptr) {
        ESP_LOGW("FontDiag", "SetChatMessage: chat_message_label_ is NULL!");
        return;
    }
    /* 2026-11-03 设计稿：对话态是「上行用户话（蓝）+ 下行回答（深色）」。
     *
     * 分工（见 lcd_display.h 的成员注释）：
     *   role == "user"  → chat_user_label_（蓝），并把上一轮回答清掉
     *   其余（assistant / system）→ chat_message_label_（深色），并清掉用户话
     *
     * 为什么 user 分支要清回答、回答分支要清用户话：
     *   · 用户刚问完、回答还没来 —— 屏上留着上一轮的回答，老人会以为
     *     那是这次的答案（"我问了头疼，它说该吃药了"）；
     *   · 回答来了、用户话还留着 —— 正好是设计稿那一幕，**保留**。
     *   所以只有 user 分支清回答，回答分支**不清**用户话。
     *
     * system 分支（PLEASE_WAIT / 报错 / 升级提示）与对话无关，两条都清，
     * 否则会出现「系统升级中 + 三分钟前那句无关的提问」同时挂着。 */
    const bool is_user = (role != nullptr && strcmp(role, "user") == 0);
    lv_obj_t* target = is_user ? chat_user_label_ : chat_message_label_;
    if (target == nullptr) {
        ESP_LOGW("FontDiag", "SetChatMessage: target label is NULL (role=%s)!",
                 role ? role : "(null)");
        return;
    }

    const char* text = (content != nullptr) ? content : "";
    /* ---- 字幕必须**累积**，不能每句覆盖（2026-11-03 用户实报）----
     *
     * 用户原话：「当前字幕还是一屏显示，显示不完整。不能自动换页显示或者
     * 滚动向上。」
     *
     * 根因不是分页算法，而是**数据根本没留在屏上**：服务端每念一句就发一条
     * `sentence_start`（application.cc:592-599），而这里每来一句都
     * `lv_label_set_text` 覆盖掉上一句。于是一段 5 句的回答，屏上永远只有
     * 最后一句 —— 分页自然永远不会触发（每句都只有一两行，装得下），
     * 老人却漏掉了前面 4 句。这也解释了"能翻页却没翻"：没有内容可翻。
     *
     * 现在的语义：
     *   · assistant：**追加**（换行分隔），整段回答留在屏上，由 ChatPageLayout
     *     按页翻；
     *   · user：覆盖（用户话只有一句，且它到达时本来就该清掉上一轮回答）；
     *   · system：覆盖（升级/报错/请稍候是瞬时状态，累积会变成一屏乱码）。
     *
     * 重复保护：工具轮里服务端可能把同一句重发一次，追加前先看末尾是不是
     * 已经就是这句 —— 是就不动，否则老人会看到同一句话两遍。
     */
    const bool is_system = (role != nullptr && strcmp(role, "system") == 0);
    /* 本句第一行在"设备字幕 label 内"的起始像素；-1 = 本次不是追加
     * （user / system 覆盖，或 assistant 的第一句），落点回第 1 页。 */
    int anchor_px = -1;
    if (!is_user && !is_system && text[0] != '\0') {
        const uint32_t now_tick = lv_tick_get();
        /* 距上一句超过 20 秒 ⇒ 这是**新的一轮**（上一轮没有用户话可清），
         * 覆盖而不是追加，否则两轮回答会拼成一段。 */
        const bool new_round = (chat_assistant_tick_ == 0) ||
                               ((uint32_t)(now_tick - chat_assistant_tick_) > 20000u);
        chat_assistant_tick_ = now_tick;
        const char* prev = new_round ? nullptr : lv_label_get_text(target);
        if (prev != nullptr && prev[0] != '\0') {
            const size_t prev_len = strlen(prev);
            const size_t add_len = strlen(text);
            const bool dup = (prev_len >= add_len) &&
                             (strcmp(prev + prev_len - add_len, text) == 0);
            if (dup) {
                /* 同一句重发（工具轮）⇒ 文本没变，分页状态也不许动：
                 * 再走一次 ChatPageLayout 会把屏弹回这一句的起始页，
                 * 老人正读到一半的字会被拽走。 */
                ESP_LOGI(TAG, "SetChatMessage: 重复句，跳过 role=%s",
                         role ? role : "(null)");
                return;
            }
            /* 追加前先量"已有内容占了几行" —— 新句的第一行就从那里开始，
             * 这就是"屏跟着语音走"的落点（2026-11-04 用户第 2 条需求）。
             *
             * ⚠️ 必须**先把这个 label 复位成 LV_SIZE_CONTENT** 再量。
             * ChatPageLayout 会给最后一段补尾部留白（把 label 撑到
             * (pages-1)*page_h + 视口高），量到的是**撑过的高度** ⇒ 行数偏大、
             * 落点算到后面去，屏停在本句后面那一页，于是第 2 页顶端露出上一段
             * 的尾巴、设备字幕只剩一行半 —— 正是用户 2026-11-04 报的那个现象。
             * 这一条是"落点永远偏"的根因，别为了省一次 update_layout 删掉。
             *
             * label 高度 = 行数*行高 - 行间距（最后一行后面没有行距），
             * 所以先补回一个行距再按整行取整；少补这 4px 会少算一行。 */
            lv_obj_set_height(chat_message_label_, LV_SIZE_CONTENT);
            lv_obj_update_layout(bottom_bar_);
            const int line_h = ChatLineH();
            const int line_space = LV_VER_RES * 4 / 240;
            const int prev_h = (int)lv_obj_get_height(chat_message_label_);
            const int prev_lines =
                (line_h > 0) ? ((prev_h + line_space) / line_h) : 0;
            anchor_px = prev_lines * line_h;
            std::string merged(prev);
            merged += "\n";
            merged += text;
            lv_label_set_text(target, merged.c_str());
        } else {
            lv_label_set_text(target, text);
            anchor_px = 0;   /* 第一句：落点就是 label 顶部 */
        }
    } else {
        lv_label_set_text(target, text);
    }
    /* 空串 ⇒ 隐藏：留空 label 会让容器里多出一行空白，看着像"屏幕上多了一条
     * 横线"。判据看的是 **label 当前的内容**而不是本次入参：assistant 走追加
     * 分支时，入参为空不代表屏上是空的。 */
    const char* shown = lv_label_get_text(target);
    if (shown == nullptr || shown[0] == '\0') {
        lv_obj_add_flag(target, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_remove_flag(target, LV_OBJ_FLAG_HIDDEN);
    }

    lv_obj_t* other = is_user ? chat_message_label_ : chat_user_label_;
    if (other != nullptr && !is_user) {
        /* 只有 system 才清用户话；assistant 要留着（见上面注释）。 */
        if (is_system) {
            lv_label_set_text(other, "");
            lv_obj_add_flag(other, LV_OBJ_FLAG_HIDDEN);
        }
    } else if (other != nullptr) {
        /* user 分支：清掉上一轮回答 */
        lv_label_set_text(other, "");
        lv_obj_add_flag(other, LV_OBJ_FLAG_HIDDEN);
    }

    /* 两个容器互斥显示（2026-11-04 用户第 1 条）：文字写完之后才能决定
     * "这一屏给谁" —— 判据就是两段文字此刻非不为空。 */
    UpdateChatBoxes();

    ESP_LOGI(TAG, "SetChatMessage role=%s len=%d", role ? role : "(null)",
             (int)strlen(text));
    /* 分页（2026-11-04 双容器版）：只给"当前显示的那个容器"分页，页数只看它
     * 自己那一个 label 的高度 —— 两段文字不再相加，也就不存在"凑边界"。
     * 必须在**文字写完 + 容器定好**之后量，否则量到的是上一轮的高度。
     *
     * anchor_px：assistant 追加时传"本句第一行的起始像素"，屏就停在这一句
     * 开头所在的那一页（语音念到哪、字从哪开始）；user / system 到达时传
     * 默认 -1，回第 1 页 —— 用户话是新一轮的第一行，系统提示是瞬时状态，
     * 停在后面几页反而会把它顶出可视区。 */
    ChatPageLayout(anchor_px);
}
#endif  /* CONFIG_USE_WECHAT_MESSAGE_STYLE —— 以下两个函数两个分支都要有 */

/* 普通状态文案：让出整条状态栏（临时藏日期、左段放宽），见 lcd_display.h 的说明。 */
void LcdDisplay::SetStatus(const char* status) {
    LvglDisplay::SetStatus(status);
    DisplayLockGuard lock(this);
    if (status_bar_ != nullptr) {
        lv_obj_set_width(status_bar_, LV_HOR_RES * 78 / 100);
    }
    if (date_label_ != nullptr) {
        lv_obj_add_flag(date_label_, LV_OBJ_FLAG_HIDDEN);
    }
}

/* 状态栏时钟 + 日期（2026-11-03 设计稿：左「上午 08:30」右「wifi 10月29日 周三」）。
 *
 * 为什么日期不放在 LvglDisplay::UpdateStatusBar 里一起刷：那个函数在
 * lvgl_display.cc，属于所有板卡共用的显示层，它不认识本类的 date_label_。
 * 而时钟本来就要经过一次虚函数调用（SetStatusClock），顺手把日期在这里
 * 刷掉，比再挂一个 1 秒定时器省一个 timer、也省掉"两个定时器不同步导致
 * 23:59 显示 00:00 却还是昨天日期"这类边界问题。 */
void LcdDisplay::SetStatusClock(const char* text) {
    /* 先走**基类**：写 status_label_、藏 notification_label_、更新
     * last_status_update_time_ —— 这三件事与 2026-11-03 之前逐字一致，
     * 不在本函数里重写一遍（否则时间戳会有两份口径）。
     * ⚠️ 这里刻意**不**调 LcdDisplay::SetStatus —— 那会把日期藏起来。 */
    LvglDisplay::SetStatus(text);

    if (date_label_ == nullptr) {
        return;
    }
    /* WiFi 图标（2026-11-03 真机反馈：状态栏没有 WiFi 图标）。
     *
     * 基类 LvglDisplay::UpdateStatusBar() 里本来有一段 10 秒一次的刷新，走
     * board.GetNetworkStateIcon() + network_icon_ 缓存。真机上图标是空的，
     * 而那段逻辑在 SetupUI 之前就可能跑过（那时 network_label_ 还是 nullptr），
     * 具体原因靠一次诊断日志定位。这里先保证**一定能显示**：与时钟同频
     * （同样 10 秒）主动刷一次，判据只看"图标变了没"，不看 label 建没建。
     *
     * 为什么值得单独兜一层：状态栏的 WiFi 图标是老人判断"能不能联网"的
     * 唯一依据。图标空着他只会觉得"设备坏了、连不上"。
     *
     * 取值放锁外：GetNetworkStateIcon() 会读 WiFi 驱动里的 RSSI，持着显示锁
     * 调驱动不是好习惯。 */
    const char* net_icon = Board::GetInstance().GetNetworkStateIcon();

    DisplayLockGuard lock(this);
    /* 把状态栏恢复成「时钟 34% + 图标日期」的设计稿比例 */
    if (status_bar_ != nullptr) {
        lv_obj_set_width(status_bar_, LV_HOR_RES * 34 / 100);
    }
    lv_obj_remove_flag(date_label_, LV_OBJ_FLAG_HIDDEN);

    /* WiFi 图标落地（取值在锁外，见函数开头）。
     *
     * ⚠️ 必须放在下面那段「系统时间没同步就 return」**之前**：开机后头十几秒
     * tm_year 还是 1970，那时正是老人看着状态栏问"怎么连不上"的时候 ——
     * 把图标更新放在 return 后面，等于在最需要它的那段时间不显示。 */
    if (network_label_ != nullptr && net_icon != nullptr &&
        network_icon_ != net_icon) {
        network_icon_ = net_icon;
        lv_label_set_text(network_label_, net_icon);
        ESP_LOGI(TAG, "状态栏 WiFi 图标已刷新（rssi 档位变化或首次设置）");
    }

    /* 一次性诊断（2026-11-03）：真机上 WiFi 图标看不见。
     *
     * 两种原因在屏上是**同一个现象**，靠猜分不出来：
     *   ① 文本压根没设（network_label_ 一直空）→ net_text=空
     *   ② 文本设了，但 label 被 flex 压成 0 宽 → net_text=非空 net_w=0
     * 所以一次把文本有无、label 宽高、父容器（right_icons）宽与 x、状态栏宽
     * 全打出来。只打一次：这条日志是排查用的，不该每 10 秒刷屏。 */
    {
        static bool diag_done = false;
        if (!diag_done && network_label_ != nullptr) {
            diag_done = true;
            lv_obj_t* parent = lv_obj_get_parent(network_label_);
            const char* t = lv_label_get_text(network_label_);
            ESP_LOGW(TAG, "状态栏诊断: net_text=%s net_w=%d net_h=%d "
                     "right_w=%d right_x=%d bar_w=%d",
                     (t != nullptr && t[0] != '\0') ? "非空" : "空",
                     (int)lv_obj_get_width(network_label_),
                     (int)lv_obj_get_height(network_label_),
                     parent != nullptr ? (int)lv_obj_get_width(parent) : -1,
                     parent != nullptr ? (int)lv_obj_get_x(parent) : -1,
                     top_bar_ != nullptr ? (int)lv_obj_get_width(top_bar_) : -1);
        }
    }

    time_t now = time(NULL);
    struct tm* tm = localtime(&now);
    if (tm == nullptr || tm->tm_year < 2025 - 1900) {
        return;   /* 系统时间还没同步：宁可留空，也不显示 1970 年 */
    }
    static const char* kWeek[] = {"周日", "周一", "周二", "周三", "周四", "周五", "周六"};
    int wd = tm->tm_wday;
    if (wd < 0 || wd > 6) {
        wd = 0;
    }
    char buf[32];
    snprintf(buf, sizeof(buf), "%d月%d日 %s", tm->tm_mon + 1, tm->tm_mday, kWeek[wd]);
    lv_label_set_text(date_label_, buf);
}

void LcdDisplay::SetEmotion(const char* emotion) {
    /* 任何主动表情切换都算"界面有变化" ⇒ 重置防烧屏的空闲计时。
     * 放在最前面：下面有多个 return 分支，漏掉哪个都会让轮播在
     * 对话进行中乱换图。 */
    last_ui_change_tick_ = lv_tick_get();

    // Stop any running GIF animation
    if (gif_controller_) {
        DisplayLockGuard lock(this);
        gif_controller_->Stop();
        gif_controller_.reset();
    }

    /* 待机跑马灯必须在换图**之前**停掉：它每帧都在改自己那个 image 的 x，
     * 而"有语境表情"和"待机指示灯"是互斥的两种语义 —— 同时出现在屏上，
     * 老人分不清设备是在待机还是在干活。滚动的 lv_anim 也要一起删，
     * 否则它继续每 16ms 改一个已经隐藏的对象的坐标，白烧 CPU。
     * 放在 emoji_image_ 判空之前：跑马灯和 emoji_image_ 是两块独立对象。 */
    {
        DisplayLockGuard lock(this);
        StopIdleMarquee();
    }

    if (emoji_image_ == nullptr) {
        return;
    }

    auto emoji_collection = static_cast<LvglTheme*>(current_theme_)->emoji_collection();
    auto image = emoji_collection != nullptr ? emoji_collection->GetEmojiImage(emotion) : nullptr;
    if (image == nullptr) {
        const char* utf8 = font_awesome_get_utf8(emotion);
        if (utf8 != nullptr && emoji_label_ != nullptr) {
            DisplayLockGuard lock(this);
            lv_label_set_text(emoji_label_, utf8);
            lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
        }
        return;
    }

    DisplayLockGuard lock(this);
    if (image->IsGif()) {
        // Create new GIF controller
        gif_controller_ = std::make_unique<LvglGif>(image->image_dsc());
        
        if (gif_controller_->IsLoaded()) {
            // Set up frame update callback
            gif_controller_->SetFrameCallback([this]() {
                lv_image_set_src(emoji_image_, gif_controller_->image_dsc());
            });
            
            // Set initial frame and start animation
            lv_image_set_src(emoji_image_, gif_controller_->image_dsc());
            gif_controller_->Start();
            
            // Show GIF, hide others
            lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
        } else {
            ESP_LOGE(TAG, "Failed to load GIF for emotion: %s", emotion);
            gif_controller_.reset();
        }
    } else {
        lv_image_set_src(emoji_image_, image->image_dsc());
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
    }

#if CONFIG_USE_WECHAT_MESSAGE_STYLE
    // In WeChat message style, if emotion is neutral, don't display it
    uint32_t child_count = lv_obj_get_child_cnt(content_);
    if (strcmp(emotion, "neutral") == 0 && child_count > 0) {
        // Stop GIF animation if running
        if (gif_controller_) {
            gif_controller_->Stop();
            gif_controller_.reset();
        }
        
        lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
    }
#endif
}

void LcdDisplay::StartAntiBurnInTimer() {
    if (anti_burn_timer_ != nullptr || emoji_image_ == nullptr) {
        return;
    }
    /* 待机跑马灯不需要预热：表情图在 assets 里，StartIdleMarquee() 每次
     * 现查现用（见那里的注释：缓存会因主题/挂载时序过期）。这里只起计时。 */
    last_ui_change_tick_ = lv_tick_get();

    /* 订阅设备状态：屏保只在待机态跑（见 AntiBurnInStep 开头的 2026-11-01
     * 事故注释）。放在这里而不是构造函数里，是因为本函数已有
     * `if (anti_burn_timer_ != nullptr || emoji_image_ == nullptr) return;`
     * 的幂等保护 —— 回调只会注册一次，不会因为 SetupUI 重跑而重复注册。
     *
     * 线程：PostStateChangeEvent() 走 esp_event 默认事件循环，回调在
     * sys_evt 任务里执行；AntiBurnInStep() 在 LVGL 定时器任务里执行。
     * 两个任务之间只共享一个 bool，所以成员声明为 volatile（见头文件）。
     * 不在这里做任何 LVGL 操作：事件循环任务里碰 LVGL 必须拿锁，
     * 而本回调要做的只是记一个标志位，没必要引入锁和潜在的锁序问题。
     *
     * 事件是异步投递的，标志位可能比真实状态晚一个事件循环周期。防烧屏
     * 20 秒才查一次、判据是 60 秒静止，这点延迟完全不影响语义。
     *
     * 回调里捕获的 this 不会悬空：LcdDisplay 与进程同生命周期（只在重启时
     * 销毁），而注销回调的路径根本不存在 —— 与 elder_bridge.cc 的用法一致。 */
    DeviceStateEventManager::GetInstance().RegisterStateChangeCallback(
        [this](DeviceState /*prev*/, DeviceState cur) {
            device_idle_ = (cur == kDeviceStateIdle);
        });

    /* lv_timer 回调必须是裸函数指针，所以用 user_data 带回 this。 */
    anti_burn_timer_ = lv_timer_create([](lv_timer_t* t) {
        static_cast<LcdDisplay*>(lv_timer_get_user_data(t))->AntiBurnInStep();
    }, ANTI_BURN_CHECK_MS, this);
    if (anti_burn_timer_ == nullptr) {
        ESP_LOGW(TAG, "防烧屏定时器创建失败（界面仍可用，只是不轮播）");
    }
}

void LcdDisplay::AntiBurnInStep() {
    /* ★ 第一道闸：非待机态一律不动屏。★
     *
     * 2026-11-01 真机事故：老人唤醒后不说话（或一轮对话播报完设备自动回到
     * 聆听态），屏幕中央换成了待机动态图案，而字幕还是「聆听中」、状态灯
     * 还是蓝色常亮 —— 三处互相打架。老人看到待机图案以为设备回待机了，
     * 喊唤醒词反而打断当前这轮，越急越乱。
     *
     * 根因就是这个函数**只看界面静止了多久，不看设备在干什么**：本机
     * CONFIG_USE_DEVICE_AEC=y ⇒ 走 kListeningModeRealtime 连续对话，一轮
     * 说完自动回 kDeviceStateListening，而这一态唤醒词是关的；老人一沉默
     * 就停在那儿，直到服务端 120 秒无有效内容才下发待机。这 120 秒里界面
     * 静止满 60 秒，于是防烧屏把待机指示灯播了上去。
     *
     * 为什么必须在这里判、不能只在 PlayIdleAnimation() 里播：待机指示灯的
     * 产品语义是「设备在待机、可以喊我」（2026-10-22 用户原话：「进入待机
     * 状态强制切换到待机动态图案，以便区分硬件是否在监听声音，让用户知道
     * 是否需要唤醒词进行唤醒」；2026-11-03 起这个图案由待机跑马灯承担）。
     * 非待机态播它 = 对老人撒谎，属于功能错误而不是视觉瑕疵，所以判据必须
     * 挡在所有播图路径的最前面（含下面的静态兜底轮播：聆听中把表情图换成
     * 随机一张，同样会让人以为状态变了）。
     *
     * 为什么不能只靠 PlayIdleAnimation()、把这条兜底删掉：待机态下老人
     * 中途说一句话，SetEmotion() 会停掉跑马灯并换成语境图标；如果这句话
     * 没有让状态离开待机（识别为空/被丢弃），屏上就再没有待机指示灯了 ——
     * 而「有没有它在滚」是老人判断"要不要喊唤醒词"的唯一依据。所以待机态
     * 仍然需要防烧屏这条兜底把它接回来；非待机态则必须一步都不许动。
     *
     * 代价：聆听/播报态长时间静止确实有烧屏风险。但这两个态本来就有
     * 字幕、状态栏、灯效在变，且它们是"正在工作"的短时态；把待机指示灯
     * 播到工作态上换来的误导，远比残影严重。 */
    if (!device_idle_) {
        return;
    }

    /* 界面刚刚变过（有人在对话/收到通知）⇒ 现在换图就是捣乱，跳过。
     * 只在真正长时间静止时才轮换。 */
    if (lv_tick_elaps(last_ui_change_tick_) < ANTI_BURN_IDLE_MS) {
        return;
    }

    /* 空闲屏保 = **一直滚着的待机跑马灯**。
     *
     * 语义（2026-10-20 用户拍板，2026-11-03 从"熊猫动图"换成"跑马灯"）：
     * 待机就一直播、无限循环，像素一直在变（这才真防烧屏）；有人说话时
     * SetEmotion() 会先停掉它，立刻回到语境对应的静态图标。
     * 所以这里只需要「没在跑就开跑」，跑着就什么都不做。
     *
     * ⚠️ idle_marquee_running_ 的读必须**持锁**：它由 SetEmotion /
     * PlayIdleAnimation（应用任务、协议任务）写，本函数跑在 LVGL 定时器
     * 任务里。不加锁就是跨任务读一个普通 bool —— 与 device_idle_ 不同，
     * 这里不打算为它引入 volatile（那只是让读不被缓存，不解决可见性），
     * 直接进锁最省事：20 秒一次，代价可以忽略。 */
    {
        DisplayLockGuard lock(this);
        /* 全屏提醒正挂着 ⇒ 本函数一步都不许动（2026-11-03）。
         *
         * 提醒最长挂 4 小时，这期间设备一直是待机、界面也不再变化，所以
         * device_idle_ 与 60 秒静止这两道闸门都拦不住它，每 20 秒照常进来。
         * 不在这里挡住会有两件事：
         *   1) 把待机跑马灯重新拉起来。两张图是 screen 的直接子对象，建得比
         *      reminder_overlay_ 晚就会画在卡片**上面** —— 老人看到"提醒卡片
         *      上飘着两个表情"。StartIdleMarquee() 里也拦了，但那是最后一道，
         *      挡不住下面那条静态兜底轮播；
         *   2) 下面那条静态兜底会调 SetEmotion() 换一次表情。它被卡片完全
         *      盖住看不见，代价却照付：SetEmotion 里 lv_image_set_src +
         *      取消 HIDDEN ⇒ 被遮住的那块图片区仍然要被标脏、重绘一遍
         *      （LVGL 没有遮挡剔除）；顺带还把 emoji_box_ 重新取消隐藏 ——
         *      现在靠"卡片尺寸恰好盖住它"才看不见，这是隐式前提，不该留。
         *      触发频率约 60 秒一次（不是定时器的 20 秒：SetEmotion 会把
         *      last_ui_change_tick_ 重置，本函数下次还得重新等满 60 秒），
         *      4 小时约 240 次。
         *      ⚠️ 这里**不是** PNG 解码的代价：表情 2026-11-02 起是 cbin
         *      裸 RGB565A8、零解码。assets.cc 里那条 "CONFIG_LV_CACHE_DEF_SIZE=0
         *      ⇒ 每次重绘都 lodepng 重解" 说的是**旧 PNG 路径**，别套过来。
         *
         * 判据用 overlay 的**可见性**，不用"有没有待处理的提醒"：这张卡片
         * 的显隐只由 ShowReminder / HideReminder 改，两者都持同一把锁。 */
        if (reminder_overlay_ != nullptr &&
            !lv_obj_has_flag(reminder_overlay_, LV_OBJ_FLAG_HIDDEN)) {
            return;
        }
        if (idle_marquee_running_) {
            return;
        }
        if (StartIdleMarquee()) {
            ESP_LOGI(TAG, "空闲屏保：静止 %u 秒，待机跑马灯开播（%u 张表情）",
                     (unsigned)(lv_tick_elaps(last_ui_change_tick_) / 1000),
                     (unsigned)idle_marquee_ready_.size());
            return;
        }
    }

    auto* theme = static_cast<LvglTheme*>(current_theme_);
    auto collection = theme != nullptr ? theme->emoji_collection() : nullptr;
    if (collection == nullptr) {
        return;
    }
    auto names = collection->EnumNames();
    if (names.size() < 2) {
        return;
    }

    /* 最多试 8 次：要避开"和上次一样"以及 GIF（GIF 会一直播、反而更费电，
     * 而且 lv_image_set_src 每帧刷新对防烧屏没意义）。 */
    for (int attempt = 0; attempt < 8; ++attempt) {
        const std::string& candidate = names[esp_random() % names.size()];
        if (candidate == anti_burn_last_emoji_) {
            continue;
        }
        if (collection->GetEmojiImage(candidate.c_str()) == nullptr) {
            continue;   /* assets 里没这张（名字不在 cbin 清单里） */
        }
        anti_burn_last_emoji_ = candidate;
        /* 走到这里说明跑马灯起不来（一个表情都取不到图），退回静态轮换，
         * 至少还能防烧屏。这条分支正常情况下走不到。 */
        ESP_LOGW(TAG, "防烧屏兜底：跑马灯起不来，静态表情轮换为 \"%s\"",
                 candidate.c_str());
        SetEmotion(candidate.c_str());   /* 内部会重置 last_ui_change_tick_ */
        return;
    }
}

// >>> BEGIN elder-idle-marquee (2026-11-03：待机跑马灯顶替熊猫) >>>
/* 待机跑马灯 —— 本次改动的全部实现都在这一段。
 *
 * 需求（2026-11-03 用户原话）：「待机熊猫和跑马灯放在同一个 commit 里删，
 * 跑马灯顶上这个位置。」即：删掉编译进 .rodata 的熊猫帧动画（269,568 字节），
 * 由跑马灯占用它原来那块图片区（emoji_box_，320x240 屏上是 134x110），
 * 继续承担「设备活着、可以喊我」这个唯一视觉信号（2026-10-22 用户对熊猫的
 * 语义定义，见 PlayIdleAnimation 的注释）。
 *
 * 形态：图片区里**一个** lv_image，lv_anim 驱动 lv_obj_set_x 从右边缘外
 * 匀速左移；整张移出左边缘后 lv_image_set_src 换下一张、x 复位到右边缘外。
 * 不建 N 个 image 拼长条：屏幕只够同时看到一张，多建的每个对象都要参与
 * 重绘扫描，内存和重绘量都是 N 倍。
 *
 * 为什么用 assets 里的 cbin 表情、不另做 PNG/GIF：本机
 * CONFIG_LV_CACHE_DEF_SIZE=0（图片缓存关闭），PNG 每帧都要 lodepng 重解，
 * 逐帧滚动会把 LVGL 任务拖死 —— 这正是 2026-11-02 把表情从 PNG 换成
 * cbin（裸 RGB565A8，零解码）的原因，跑马灯直接复用这条既有取图路径
 * （LvglTheme::emoji_collection()->GetEmojiImage）。 */

namespace {

/* 待机态滚哪些表情。
 *
 * 只放中性/友好的一批，**不用全部 21 个**：angry / shocked / crying / sad
 * 这类负面表情在待机态循环出现，会让老人以为设备在生气、或者家里出了事。
 * 待机指示灯是「状态指示」不是「装饰」，它的每一个画面都会被当成设备在
 * 表达情绪 —— 筛选标准就是「老人看了不会往坏处想」。
 *
 * 2026-11-03 把 thinking 换成 loving：待机态滚出「思考中」是自相矛盾的
 * 假信号 —— 设备什么都没在处理，老人却看见它在想，会以为自己的话还没被
 * 应答、再喊一遍把这次请求打断（与 2026-11-01 那次「非待机态播待机灯」
 * 是同一类错：指示灯说了它没在做的事）。loving 是正面的陪伴感，与
 * 「小安在待机、可以喊我」一致。
 *
 * 名字必须与 main/assets/emoji/fluent3d_128/ 下的文件名**逐字一致**
 * （assets.cc 按这个名字查 cbin，写错就是取不到图，会被下面的校验跳掉）。
 * 顺序按「平静 → 有点俏皮」排，滚起来观感是渐进的，不会忽喜忽悲。 */
constexpr const char* kIdleMarqueeEmoji[] = {
    "neutral", "relaxed", "happy", "loving", "winking", "cool", "sleepy",
};

/* 滚动速度 55 px/s（2026-11-03 轨道改成整屏后从 35 上调）。
 *
 * 上限由「看得清」定：一张图从屏幕右缘外到左缘外要走 320+60=380px，
 * 再快（>75px/s）老人只看到一团颜色在动，认不出是什么表情。
 * 下限由「别以为卡住」定：慢于 ~40px/s 时全程要 9 秒以上，屏上大部分时间
 * 是空的，观感接近静止，起不到「设备活着」的信号作用。
 * 取 55 ⇒ 单张过屏 380/55 ≈ 6.9 秒，7 张一轮约 48 秒，与防烧屏的 60 秒
 * 量级一致（一轮下来整块主区的像素全换过）。
 *
 * ⚠️ 旧值 35 是按「图片区 134px 宽」算的（单张 7 秒）；轨道变成 320px 之后
 * 沿用 35 会让单张走 10.9 秒，屏上空窗期太长。 */
constexpr int32_t kIdleMarqueeSpeedPxPerSec = 55;

}  // namespace

void LcdDisplay::IdleMarqueeExecCb(void* var, int32_t x) {
    auto* self = static_cast<LcdDisplay*>(var);
    if (self == nullptr || self->idle_marquee_image_ == nullptr) {
        return;
    }
    /* 一轮滚完 ⇒ 换下一张。判据是「x 变大了」：动画只往左走，x 变大只可能
     * 发生在 repeat 复位回起点那一刻（lv_anim 在 repeat 时把 act_time 归零，
     * path 重新算出 start_value）。
     *
     * 不用 lv_anim 的 completed_cb：它只在 repeat_count 归零时回调
     * （lv_anim.c 的 anim_completed_handler），而这里是无限循环，永远等不到。
     * 也不用 start_cb：repeat 不走 start_cb，只在首次启动时调。 */
    if (x > self->idle_marquee_last_x_) {
        self->AdvanceIdleMarqueeImage(self->idle_marquee_image_);
    }
    self->idle_marquee_last_x_ = x;
    lv_obj_set_x(self->idle_marquee_image_, x);

    /* 第二张图**错开半个行程**：设计稿（mock-3）主区里同时能看到 3~4 个圆，
     * 而只放一张的话，任何时刻屏上只有一个 60px 的圆漂在 320px 的空场里，
     * 观感是"有个东西在飘"而不是"表情依次移过"。
     *
     * 用同一个 lv_anim 驱动两张、而不是给第二张另起一条动画：lv_anim_start
     * 会 remove_concurrent_anims（同 var + 同 exec_cb），同一对 (this, cb)
     * 只能存在一条；要两条就得再写一个 exec_cb，还要保证两条动画的相位
     * 永远不漂移。一个回调里算两个 x 最省事，也天然同步。
     *
     * 越界回绕：x2 = x + half，x 从 LV_HOR_RES 走到 -img_w，所以 x2 会跑到
     * 屏幕右侧之外 —— 减一个 track 把它接回左缘外，视觉上就是"前一张还在
     * 屏中、后一张刚从右缘进来"。 */
    if (self->idle_marquee_image2_ == nullptr) {
        return;
    }
    const int32_t track = LV_HOR_RES + self->idle_marquee_size_;
    int32_t x2 = x + track / 2;
    const bool wrapped = (x2 > LV_HOR_RES);
    if (wrapped) {
        x2 -= track;
    }
    /* ⚠️ 第二张**自己**走到右缘外的那一刻才换图，绝不跟第一张共用一个时刻。
     *
     * 两张相位差半程 ⇒ 第一张回绕（x 从 -img_w 一下跳回 LV_HOR_RES）的那一帧，
     * 第二张正好停在屏幕中间（x2 = x + half ≈ half - img_w）。跟着第一张一起
     * 换 src，老人看到的就是"屏中央那张脸突然变成了另一个表情"——
     * 而换图本来的含义是"上一张已经走出去了，新的从右边进来"，全程该发生在屏外。
     *
     * wrapped 由 true 变 false 就是"刚从右缘外接回来"的那一帧（x 跨过
     * LV_HOR_RES - track/2），此刻 x2 == LV_HOR_RES ⇒ 整张在屏外，换图看不见。 */
    if (self->idle_marquee_image2_wrapped_ && !wrapped) {
        self->AdvanceIdleMarqueeImage(self->idle_marquee_image2_);
    }
    self->idle_marquee_image2_wrapped_ = wrapped;
    lv_obj_set_x(self->idle_marquee_image2_, x2);
}

void LcdDisplay::AdvanceIdleMarqueeImage(lv_obj_t* target) {
    if (target == nullptr || idle_marquee_ready_.empty()) {
        return;
    }
    auto* theme = static_cast<LvglTheme*>(current_theme_);
    auto collection = theme != nullptr ? theme->emoji_collection() : nullptr;
    if (collection == nullptr) {
        return;
    }
    /* 理论上每个名字在 StartIdleMarquee() 时都校验过；但主题/资源包可能
     * 中途换过，所以这里仍然逐个试。取不到就跳过继续下一张 —— 不显示空白，
     * 也不让 src 停在一个可能已经失效的 dsc 上（旧图会保留到换成功为止）。 */
    for (size_t tries = 0; tries < idle_marquee_ready_.size(); ++tries) {
        const char* name = idle_marquee_ready_[idle_marquee_next_];
        idle_marquee_next_ = (idle_marquee_next_ + 1) % idle_marquee_ready_.size();
        const LvglImage* image = collection->GetEmojiImage(name);
        if (image == nullptr) {
            ESP_LOGW(TAG, "待机跑马灯：表情 \"%s\" 取不到图，跳过", name);
            continue;
        }
        lv_image_set_src(target, image->image_dsc());
        return;
    }
}

bool LcdDisplay::StartIdleMarquee() {
    if (idle_marquee_running_) {
        return true;
    }
    /* 全屏提醒正挂着 ⇒ 绝不开播。
     *
     * 为什么必须在这里拦（2026-11-03）：AntiBurnInStep 每 20 秒检查一次
     * 「待机 + 60 秒无 UI 变化就开播」，而提醒挂 4 小时期间设备一直是待机、
     * 界面也不变 —— 它会在提醒挂上 60 秒后把跑马灯重新拉起来。
     * 旧实现的图挂在 emoji_box_ 底下，只是白烧 CPU（被 overlay 盖住看不见）；
     * 现在两张图是 screen 的直接子对象，一旦建得比 reminder_overlay_ 晚
     * （提醒先来、第一次待机后到），就会画在卡片**上面** ——
     * 老人看到"提醒卡片上飘着两个表情"。 */
    if (reminder_overlay_ != nullptr &&
        !lv_obj_has_flag(reminder_overlay_, LV_OBJ_FLAG_HIDDEN)) {
        return false;
    }
    if (emoji_box_ == nullptr) {
        ESP_LOGW(TAG, "待机跑马灯：图片区还没建好，不启动");
        return false;
    }
    auto* theme = static_cast<LvglTheme*>(current_theme_);
    auto collection = theme != nullptr ? theme->emoji_collection() : nullptr;
    if (collection == nullptr) {
        ESP_LOGW(TAG, "待机跑马灯：没有表情集合（assets 分区没挂上？），不启动");
        return false;
    }

    /* 每次启动都重新校验可用性，不缓存结论：assets 分区挂载晚于 SetupUI，
     * 主题也可能换过，缓存会拿到过期结果。7 次 map 查找，代价可忽略。 */
    idle_marquee_ready_.clear();
    for (const char* name : kIdleMarqueeEmoji) {
        if (collection->GetEmojiImage(name) == nullptr) {
            /* 缺图必须点名报出来。静默跳过会让「跑马灯只滚 3 张」这种问题
             * 永远查不出来 —— 屏上看着"在动"，只是少了几张，没人会怀疑。 */
            ESP_LOGW(TAG, "待机跑马灯：表情 \"%s\" 取不到图，跳过", name);
            continue;
        }
        idle_marquee_ready_.push_back(name);
    }
    if (idle_marquee_ready_.empty()) {
        ESP_LOGE(TAG, "待机跑马灯：一个表情都取不到，不启动（屏上保持静态表情）");
        return false;
    }

    /* 尺寸与轨道（2026-11-03 设计稿）：表情直径 60px（mock-3 实测 60x60），
     * 轨道是**整屏宽度**，纵向居中于主区（状态栏以下）。
     *
     * ⚠️ 这里正是 2026-11-03 用户实报「没有从全屏右侧进入、左侧出」的正解：
     * 旧代码把轨道取成 emoji_box_ 的宽（320x240 上是 134px），图片又挂在
     * emoji_box_ 底下，所以只在屏幕中央那个小方块里滚 —— 观感是"中间有张
     * 图在滑"，而不是"表情从右边进来、从左边出去"。
     * 现在父对象是 screen、轨道是 LV_HOR_RES。 */
    const int32_t img_sz = MarqueeSize();
    if (img_sz <= 0) {
        ESP_LOGW(TAG, "待机跑马灯：屏幕尺寸异常（%dx%d），不启动",
                 (int)LV_HOR_RES, (int)LV_VER_RES);
        return false;
    }
    const int32_t main_h = LV_VER_RES - StatusBarH();
    const int32_t img_y = StatusBarH() + (main_h - img_sz) / 2;

    if (idle_marquee_image_ == nullptr) {
        /* 父对象是 screen 而不是 emoji_box_：轨道要跨整屏。
         * 只建一次，之后靠显隐复用：LVGL 对象创建在 PSRAM 碎片化时可能失败，
         * 而这块是待机态反复要用的东西。 */
        idle_marquee_image_ = lv_image_create(lv_screen_active());
        lv_obj_set_style_pad_all(idle_marquee_image_, 0, 0);
        lv_obj_set_style_border_width(idle_marquee_image_, 0, 0);
        lv_image_set_inner_align(idle_marquee_image_, LV_IMAGE_ALIGN_CONTAIN);
        lv_obj_add_flag(idle_marquee_image_, LV_OBJ_FLAG_HIDDEN);
    }
    if (idle_marquee_image2_ == nullptr) {
        idle_marquee_image2_ = lv_image_create(lv_screen_active());
        lv_obj_set_style_pad_all(idle_marquee_image2_, 0, 0);
        lv_obj_set_style_border_width(idle_marquee_image2_, 0, 0);
        lv_image_set_inner_align(idle_marquee_image2_, LV_IMAGE_ALIGN_CONTAIN);
        lv_obj_add_flag(idle_marquee_image2_, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_set_size(idle_marquee_image_, img_sz, img_sz);
    lv_obj_set_y(idle_marquee_image_, img_y);
    lv_obj_set_size(idle_marquee_image2_, img_sz, img_sz);
    lv_obj_set_y(idle_marquee_image2_, img_y);
    idle_marquee_size_ = img_sz;

    idle_marquee_next_ = 0;
    idle_marquee_image2_wrapped_ = false;
    const LvglImage* first = collection->GetEmojiImage(idle_marquee_ready_[0]);
    if (first == nullptr) {
        /* 上面刚校验过，走到这里说明这一瞬间主题换了 —— 交给下一轮兜底 */
        ESP_LOGW(TAG, "待机跑马灯：首帧取图失败，不启动");
        return false;
    }
    lv_image_set_src(idle_marquee_image_, first->image_dsc());
    idle_marquee_next_ = 1 % idle_marquee_ready_.size();

    /* 第二张的初始表情取列表里的下一张；只有一张可用时就是同一张
     * （此时两张错开半个行程仍然有"依次移过"的观感，只是图案相同）。 */
    {
        const LvglImage* second = collection->GetEmojiImage(
            idle_marquee_ready_[idle_marquee_next_]);
        if (second == nullptr) {
            second = first;
        }
        lv_image_set_src(idle_marquee_image2_, second->image_dsc());
        idle_marquee_next_ = (idle_marquee_next_ + 1) % idle_marquee_ready_.size();
    }

    if (emoji_image_ != nullptr) {
        lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
    }
    if (emoji_label_ != nullptr) {
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
    }
    /* 头像圆整块藏起来：设计稿的待机态没有那个蓝紫圆，只有滚过的表情。
     * 不藏的话右边缘会常驻一个空心的蓝色圆 —— 老人会以为"屏幕坏了、
     * 头像变空白了"。 */
    if (emoji_box_ != nullptr) {
        lv_obj_add_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_remove_flag(idle_marquee_image_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(idle_marquee_image2_, LV_OBJ_FLAG_HIDDEN);

    /* x 从屏幕右缘之外开始（对象左边缘 = LV_HOR_RES ⇒ 整张在屏外），
     * 匀速左移到 -img_w（对象右边缘 = 0 ⇒ 整张移出左边缘）。一条 lv_anim
     * 用 repeat 无限循环，每轮回起点时 x 由动画自己复位。
     *
     * var 传 this 而不是 image：exec 回调里要拿 LcdDisplay 的成员（ready 列表、
     * 下一张下标），而 lv_anim 的 exec_cb 只给一个 void*。顺带获得一个好处：
     * lv_anim_start 会 remove_concurrent_anims（同 var + 同 exec_cb），
     * 所以本函数被重入也不会同时跑两条动画。 */
    const int32_t img_w = img_sz;
    const int32_t track = LV_HOR_RES + img_w;   /* 右缘外 → 左缘外，全程 */
    const uint32_t duration_ms =
        (uint32_t)((int64_t)track * 1000 / kIdleMarqueeSpeedPxPerSec);

    /* early_apply 默认开 ⇒ lv_anim_start 会立刻用 start_value 调一次回调。
     * 所以 last_x_ 必须在 start **之前**置成起点，否则首次回调会被当成
     * 「x 变大」而白白换掉一张图。 */
    idle_marquee_last_x_ = LV_HOR_RES;

    lv_anim_t anim;
    lv_anim_init(&anim);
    lv_anim_set_var(&anim, this);
    lv_anim_set_exec_cb(&anim, IdleMarqueeExecCb);
    lv_anim_set_values(&anim, LV_HOR_RES, -img_w);
    lv_anim_set_duration(&anim, duration_ms);
    lv_anim_set_path_cb(&anim, lv_anim_path_linear);
    lv_anim_set_repeat_count(&anim, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_repeat_delay(&anim, 0);
    if (lv_anim_start(&anim) == nullptr) {
        ESP_LOGW(TAG, "待机跑马灯：动画创建失败，退回静态表情");
        lv_obj_add_flag(idle_marquee_image_, LV_OBJ_FLAG_HIDDEN);
        return false;
    }

    idle_marquee_running_ = true;
    return true;
}

void LcdDisplay::StopIdleMarquee() {
    /* lv_anim_delete 按 (var, exec_cb) 匹配 —— 与 StartIdleMarquee 里注册的
     * 那一对逐字一致，否则删不掉，滚动会在图片隐藏后继续跑（白烧 CPU，
     * 且下次启动会出现两条动画抢同一个 x）。 */
    lv_anim_delete(this, IdleMarqueeExecCb);
    idle_marquee_running_ = false;
    if (idle_marquee_image_ != nullptr) {
        lv_obj_add_flag(idle_marquee_image_, LV_OBJ_FLAG_HIDDEN);
        /* 断开 src 引用：主题换过之后旧 collection 里的 dsc 会被 delete，
         * 留着野指针比"图暂时不显示"危险得多。 */
        lv_image_set_src(idle_marquee_image_, nullptr);
    }
    /* 把常规表情图放回来：SetEmotion 紧接着会自己设新 src 并显式去掉隐藏
     * 标志，但 SetTheme / ShowReminder 这两条路没有 —— 不补这一步，换主题
     * 之后图片区会一直空着，直到下一次 SetEmotion（老人看到的是"屏幕坏了"）。 */
    if (emoji_image_ != nullptr && lv_image_get_src(emoji_image_) != nullptr) {
        lv_obj_remove_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
    }
    /* 头像圆（emoji_box_）在 StartIdleMarquee 里被整块藏起来了，这里放回来。
     * 但**预览图正在显示**时不能放 —— SetPreviewImage 是「藏头像、显预览」，
     * 这时候把头像放回来会盖住刚设的预览图（那 5 秒里老人看到的仍是旧表情）。 */
    if (emoji_box_ != nullptr) {
        const bool preview_visible = (preview_image_ != nullptr) &&
                                     !lv_obj_has_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);
        if (!preview_visible) {
            lv_obj_remove_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
        }
    }
    /* 第二张也要藏起来并断开 src：只藏第一张的话，屏上会永远留着半个
     * 圆停在它最后一次算出来的 x 上 —— 表现为"待机指示灯走了，但屏幕上
     * 还粘着一个表情"，而且它盖在所有内容上面（是 screen 的直接子对象）。 */
    if (idle_marquee_image2_ != nullptr) {
        lv_obj_add_flag(idle_marquee_image2_, LV_OBJ_FLAG_HIDDEN);
        lv_image_set_src(idle_marquee_image2_, nullptr);
    }
    idle_marquee_last_x_ = 0;
    idle_marquee_next_ = 0;
    idle_marquee_image2_wrapped_ = false;
}

/* 进入待机：立即开播待机跑马灯，**不**等防烧屏的 60 秒。
 *
 * 需求来源（2026-10-22 用户原话）：「进入待机状态强制切换到待机动态图案，
 * 以便区分硬件是否在监听声音，让用户知道是否需要唤醒词进行唤醒。」
 * 2026-11-03 用户改为：待机熊猫删掉，这个位置放跑马灯。
 *
 * 为什么不能靠 AntiBurnInStep()：那条路径开头有
 *   if (lv_tick_elaps(last_ui_change_tick_) < ANTI_BURN_IDLE_MS) return;
 * 界面刚变过（SetEmotion("neutral") 正是界面变化）之后的 60 秒内一律
 * 跳过，而"进待机"恰好每次都紧跟一次 SetEmotion —— 跑马灯永远起不来。
 *
 * 纪律与 SetEmotion 相同：一进函数就重置防烧屏计时。界面确实变过了
 * （静态表情 -> 跑马灯），不重置的话轮播的「60 秒无操作」判据会从
 * 错误的起点算起。
 *
 * 跑马灯起不来（表情资源取不到）时只打 WARN 退回静态表情，**不调
 * SetEmotion**：静态表情此刻已在屏上，没有切换的必要，而 SetEmotion 会
 * 再重置一次计时、打一轮多余的日志。
 *
 * 只在待机态被调用：两个调用点（elder_bridge.cc 的 OnDeviceStateChanged
 * 的 cur == kDeviceStateIdle 分支、elder_enter_standby 里 SetDeviceState(Idle)
 * 之后）都成立，所以函数体内不再重复判 device_idle_ —— 那个标志由事件循环
 * 异步写入，进待机的瞬间可能还是 false，拿它当闸会把"立即播"直接掐死。 */
void LcdDisplay::PlayIdleAnimation() {
    /* 界面变化 ⇒ 重置防烧屏空闲计时（见 StartAntiBurnInTimer）。 */
    last_ui_change_tick_ = lv_tick_get();

    /* 先把可能正在播的 GIF 表情停掉，再上跑马灯。
     *
     * gif_controller_ 的帧回调和跑马灯都在改写图片区，两个同时跑就是
     * SetEmotion 注释里写的「表情闪一下就变了」甚至花屏。SetEmotion 每次
     * 都会清 gif_controller_，进待机的主路径（状态变化事件）必经 SetEmotion，
     * 通常这里已经是空的；但 elder_bridge 在「设备已经是 idle 又说了一次
     * 待机」时也会直接调本函数，那条路径没有 SetEmotion 兜底，所以必须自己清。 */
    DisplayLockGuard lock(this);
    if (gif_controller_) {
        gif_controller_->Stop();
        gif_controller_.reset();
    }

    if (StartIdleMarquee()) {
        ESP_LOGI(TAG, "待机屏保：立即开播待机跑马灯（%u 张表情，%d px/s）",
                 (unsigned)idle_marquee_ready_.size(),
                 (int)kIdleMarqueeSpeedPxPerSec);
        return;
    }
    ESP_LOGW(TAG, "待机屏保：跑马灯起不来，保持静态表情（防烧屏轮播仍会尝试）");
}
// <<< END elder-idle-marquee <<<

void LcdDisplay::SetTheme(Theme* theme) {
    DisplayLockGuard lock(this);

    /* 换主题会换掉 emoji_collection（assets 资源包里的 LvglCBinImage 会被
     * delete）。跑马灯手里攥着旧 dsc 的 src，不停掉就是野指针 —— 先停，
     * StopIdleMarquee() 里会把 src 一并断开。停掉之后如果设备仍在待机，
     * AntiBurnInStep 的兜底会在下一个 20 秒周期把它重新拉起来。 */
    StopIdleMarquee();

    auto lvgl_theme = static_cast<LvglTheme*>(theme);
    
    // Get the active screen
    lv_obj_t* screen = lv_screen_active();

    // Set font
    auto text_font = lvgl_theme->text_font()->font();
    auto icon_font = lvgl_theme->icon_font()->font();
    auto large_icon_font = lvgl_theme->large_icon_font()->font();

    /* 对话区字号：与 SetupUI 同一判据（大号字体缺失时回落常规字号）。
     * 这里**必须**重算一遍 —— SetTheme 是「开机首次 Apply」和「换主题」的
     * 共同入口，而 SetupUI 跑在 Assets::Apply() 之前，那时 theme 里还是
     * 编译进固件的小字库。漏了这一段，开机后对话区会一直是 20px，
     * 只有手动换一次主题才变大。 */
    auto chat_font = text_font;
    if (lvgl_theme->large_text_font() != nullptr &&
        lvgl_theme->large_text_font()->font() != nullptr) {
        chat_font = lvgl_theme->large_text_font()->font();
    }

    /* 2026-10-23 诊断：SetTheme 是把 theme 的字体刷到各 label 的唯一
     * 入口。打出来的指针/lh 与 Assets::Apply 创建字体时的快照对照：
     * 三个指针一致而 label 仍高 36 ⇒ 字体对象运行时被改写；
     * 指针不一致 ⇒ 字体刷到了别的 theme 对象上。 */
    ESP_LOGW("FontDiag", "SetTheme theme=%p text_font=%p lh=%d bl=%d",
             (const void*)lvgl_theme, (const void*)text_font,
             text_font ? text_font->line_height : -1,
             text_font ? text_font->base_line : -1);

    if (text_font->line_height >= 40) {
        lv_obj_set_style_text_font(mute_label_, large_icon_font, 0);
        lv_obj_set_style_text_font(battery_label_, large_icon_font, 0);
        lv_obj_set_style_text_font(network_label_, large_icon_font, 0);
    } else {
        lv_obj_set_style_text_font(mute_label_, icon_font, 0);
        lv_obj_set_style_text_font(battery_label_, icon_font, 0);
        lv_obj_set_style_text_font(network_label_, icon_font, 0);
    }

    // Set parent text color
    lv_obj_set_style_text_font(screen, text_font, 0);
    lv_obj_set_style_text_color(screen, lvgl_theme->text_color(), 0);

    /* ⚠️ 字幕/状态标签必须在这里补一次字体（2026-10-22 真机事故）
     *
     * 时序：SetupUI() 在 Application 启动早期跑，那时 assets 分区还没挂载
     * （Assets::Apply() 在 application.cc:182 才执行），theme 里是**编译进
     * 固件的** font_puhui_basic_20_4 —— 只有 788 个字形，基本只覆盖
     * assets/locales/<lang>/language.json 里的界面文案。
     * （这里刻意不写 glob 通配的星号斜杠：那个两字符组合会把本段块注释
     *   提前闭合，后面的中文标点全被当成代码 —— 2026-10-22 CI 就是这么
     *   炸的：extended character 。is not valid in an identifier。）
     *
     * 而这两个 label 在 SetupUI 里是**显式**设字体的（不靠继承），所以
     * Assets::Apply() 之后 SetTheme() 只改 screen 的字体，改不到它们：
     * 屏幕表现为「字幕里大部分汉字不显示，只剩 不/用/了/有/时/点/度 这类
     * 恰好出现在界面文案里的字」，真机截图 2026-10-22 已实证
     * （服务端原文「明天平顶山少云，十二到十九度…」→ 屏上只剩「少，到度，点。」）。
     *
     * 修在这里而不是把 SetupUI 挪到 assets 之后：SetTheme 是主题切换/资源
     * 热更新的统一入口，补在这里对「开机首次加载」和「后续换主题」两条路
     * 都成立，也不会打乱 SetupUI 与 LVGL 初始化的先后依赖。 */
    if (chat_message_label_ != nullptr) {
        lv_obj_set_style_text_font(chat_message_label_, chat_font, 0);
    }
    if (status_label_ != nullptr) {
        lv_obj_set_style_text_font(status_label_, text_font, 0);
    }
    if (notification_label_ != nullptr) {
        lv_obj_set_style_text_font(notification_label_, text_font, 0);
    }
    /* 全屏提醒的卡片是**运行时懒创建**的：换主题时它可能已经存在
     * （提醒挂着、用户又切了主题），也可能还没建（下次 ShowReminder 会用
     * 新主题的字体新建）。两条路都要覆盖，否则就是上一段注释里那种
     * 「改了字体但屏上不变」—— 提醒卡片恰恰是最不能缺字的地方。
     *
     * ⚠️ 2026-11-03 起提醒不再是单个 reminder_label_，而是
     * 标题 / 明细 / 提示 / 按钮白字 四个 label，**四个都要补**。
     * 漏掉哪个，那一行就退回只有 788 字形的编译期内置字库，
     * 屏上表现是"这一行大部分汉字不见了"。 */
    if (reminder_title_ != nullptr) {
        lv_obj_set_style_text_font(reminder_title_, text_font, 0);
        lv_obj_set_style_text_color(reminder_title_, lv_color_hex(kColorTextDark), 0);
    }
    if (reminder_items_ != nullptr) {
        lv_obj_set_style_text_font(reminder_items_, text_font, 0);
        lv_obj_set_style_text_color(reminder_items_, lv_color_hex(kColorTextDark), 0);
    }
    if (reminder_hint_ != nullptr) {
        lv_obj_set_style_text_font(reminder_hint_, text_font, 0);
    }
    if (reminder_button_label_ != nullptr) {
        lv_obj_set_style_text_font(reminder_button_label_, text_font, 0);
    }
    /* 2026-11-03 新增的两个对话/状态栏 label 同样必须补字体 */
    if (chat_user_label_ != nullptr) {
        lv_obj_set_style_text_font(chat_user_label_, chat_font, 0);
    }
    /* 换主题 ⇒ 行高可能从 25 变成 38，页高必须重算（ChatLineH 每次现算，
     * 但已经算好的 chat_page_h_ / 页数是上一套字体的）。不重算的表现是
     * "换一次主题，自动翻页就按旧行高切，页边界压着半行字"。
     * 重算顺带把滚动位置复位到第 1 页 —— 换主题时老人本来也要重看一遍。 */
    if (bottom_bar_ != nullptr && chat_user_label_ != nullptr) {
        ChatPageLayout();
    }
    if (date_label_ != nullptr) {
        lv_obj_set_style_text_font(date_label_, text_font, 0);
    }
    if (reminder_overlay_ != nullptr) {
        lv_obj_set_style_bg_color(reminder_overlay_,
                                  lvgl_theme->background_color(), 0);
    }

    // Set background image
    if (lvgl_theme->background_image() != nullptr) {
        lv_obj_set_style_bg_image_src(container_, lvgl_theme->background_image()->image_dsc(), 0);
    } else {
        lv_obj_set_style_bg_image_src(container_, nullptr, 0);
        lv_obj_set_style_bg_color(container_, lvgl_theme->background_color(), 0);
    }
    
    /* 状态栏底色（2026-11-03 设计稿）：实心藏青。
     *
     * 旧代码这里是「主题底色 + 50% 不透明度」。改主题色之后如果不动这里，
     * 换一次主题就会把藏青状态栏刷成半透明的 #D3DBFC —— 与主区混成一片，
     * 设计稿里那条分界线就没了。 */
    if (top_bar_ != nullptr) {
        lv_obj_set_style_bg_opa(top_bar_, LV_OPA_COVER, 0);
        lv_obj_set_style_bg_color(top_bar_, lv_color_hex(kColorStatusBar), 0);
    }

    /* 状态栏里的文字/图标一律白色（藏青底上深色字看不清）。
     * 2026-11-03 之前这里刷的是 theme->text_color()（现在是 #232F54），
     * 在藏青底上等于"黑字黑底"，屏上只剩几个模糊的轮廓。 */
    lv_obj_set_style_text_color(network_label_, lv_color_white(), 0);
    lv_obj_set_style_text_color(status_label_, lv_color_white(), 0);
    lv_obj_set_style_text_color(notification_label_, lv_color_white(), 0);
    lv_obj_set_style_text_color(mute_label_, lv_color_white(), 0);
    lv_obj_set_style_text_color(battery_label_, lv_color_white(), 0);
    if (date_label_ != nullptr) {
        lv_obj_set_style_text_color(date_label_, lv_color_white(), 0);
    }
    lv_obj_set_style_text_color(emoji_label_, lvgl_theme->text_color(), 0);

    // If we have the chat message style, update all message bubbles
#if CONFIG_USE_WECHAT_MESSAGE_STYLE
    // Set content background opacity
    lv_obj_set_style_bg_opa(content_, LV_OPA_TRANSP, 0);

    // Iterate through all children of content (message containers or bubbles)
    uint32_t child_count = lv_obj_get_child_cnt(content_);
    for (uint32_t i = 0; i < child_count; i++) {
        lv_obj_t* obj = lv_obj_get_child(content_, i);
        if (obj == nullptr) continue;
        
        lv_obj_t* bubble = nullptr;
        
        // Check if this object is a container or bubble
        // If it's a container (user or system message), get its child as bubble
        // If it's a bubble (assistant message), use it directly
        if (lv_obj_get_child_cnt(obj) > 0) {
            // Might be a container, check if it's a user or system message container
            // User and system message containers are transparent
            lv_opa_t bg_opa = lv_obj_get_style_bg_opa(obj, 0);
            if (bg_opa == LV_OPA_TRANSP) {
                // This is a user or system message container
                bubble = lv_obj_get_child(obj, 0);
            } else {
                // This might be an assistant message bubble itself
                bubble = obj;
            }
        } else {
            // No child elements, might be other UI elements, skip
            continue;
        }
        
        if (bubble == nullptr) continue;
        
        // Use saved user data to identify bubble type
        void* bubble_type_ptr = lv_obj_get_user_data(bubble);
        if (bubble_type_ptr != nullptr) {
            const char* bubble_type = static_cast<const char*>(bubble_type_ptr);
            
            // Apply correct color based on bubble type
            if (strcmp(bubble_type, "user") == 0) {
                lv_obj_set_style_bg_color(bubble, lvgl_theme->user_bubble_color(), 0);
            } else if (strcmp(bubble_type, "assistant") == 0) {
                lv_obj_set_style_bg_color(bubble, lvgl_theme->assistant_bubble_color(), 0); 
            } else if (strcmp(bubble_type, "system") == 0) {
                lv_obj_set_style_bg_color(bubble, lvgl_theme->system_bubble_color(), 0);
            } else if (strcmp(bubble_type, "image") == 0) {
                lv_obj_set_style_bg_color(bubble, lvgl_theme->system_bubble_color(), 0);
            }
            
            // Update border color
            lv_obj_set_style_border_color(bubble, lvgl_theme->border_color(), 0);
            
            // Update text color for the message
            if (lv_obj_get_child_cnt(bubble) > 0) {
                lv_obj_t* text = lv_obj_get_child(bubble, 0);
                if (text != nullptr) {
                    // Set text color based on bubble type
                    if (strcmp(bubble_type, "system") == 0) {
                        lv_obj_set_style_text_color(text, lvgl_theme->system_text_color(), 0);
                    } else {
                        lv_obj_set_style_text_color(text, lvgl_theme->text_color(), 0);
                    }
                }
            }
        } else {
            ESP_LOGW(TAG, "child[%lu] Bubble type is not found", i);
        }
    }
#else
    // Simple UI mode - just update the main chat message
    if (chat_message_label_ != nullptr) {
        lv_obj_set_style_text_color(chat_message_label_, lvgl_theme->text_color(), 0);
    }
    if (chat_user_label_ != nullptr) {
        lv_obj_set_style_text_color(chat_user_label_, lv_color_hex(kColorTextUser), 0);
    }
    
    if (emoji_label_ != nullptr) {
        lv_obj_set_style_text_color(emoji_label_, lvgl_theme->text_color(), 0);
    }
    
    /* 左列对话区（bottom_bar_ / user_box_）保持**全透明**：主区底色由
     * screen/container_ 提供。旧代码这里是「主题底色 + 50% 不透明度」，换到
     * #D3DBFC 之后会变成一层灰蓝色的半透明块盖在同样颜色的底上，视觉上是
     * "文字背后有一块更深的方框"—— 设计稿里没有这个框。
     * 两个容器是同一块区域的两种内容，样式必须一起刷，漏一个就是"换主题后
     * 用户话那一屏多出一个色块"。 */
    if (bottom_bar_ != nullptr) {
        lv_obj_set_style_bg_opa(bottom_bar_, LV_OPA_TRANSP, 0);
    }
    if (user_box_ != nullptr) {
        lv_obj_set_style_bg_opa(user_box_, LV_OPA_TRANSP, 0);
    }
#endif
    
    // Update low battery popup
    lv_obj_set_style_bg_color(low_battery_popup_, lvgl_theme->low_battery_color(), 0);

    // No errors occurred. Save theme to settings
    Display::SetTheme(lvgl_theme);
}

void LcdDisplay::SetHideSubtitle(bool hide) {
    DisplayLockGuard lock(this);
    hide_subtitle_ = hide;

    // Immediately update UI visibility based on the setting
    if (hide) {
        /* ⚠️ 两个容器都要藏（2026-11-04）。只藏 bottom_bar_ 的话，用户话
         * （user_box_）还挂在屏上 —— 老人以为"字幕没关掉"，或者更糟：他刚说的
         * 那句话一直留在屏上，看起来像设备卡住了。 */
        if (bottom_bar_ != nullptr) {
            lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
        }
        if (user_box_ != nullptr) {
            lv_obj_add_flag(user_box_, LV_OBJ_FLAG_HIDDEN);
        }
        /* 藏了字幕就必须停分页：定时器还在滚一个看不见的容器，白白
         * 重置 last_ui_change_tick_ ⇒ 防烧屏永远接管不了。 */
        if (chat_page_timer_ != nullptr) lv_timer_pause(chat_page_timer_);
    } else {
        /* 放开时按内容重新决定谁可见（两者仍然互斥），再重排一次页 ——
         * 藏字幕期间来过的字可能已经超一屏了。 */
        UpdateChatBoxes();
        ChatPageLayout();
    }
}

// >>> BEGIN elder-reminder-fullscreen (2026-10-22 需求 8) >>>
/* 全屏提醒 —— 2026-11-03 按设计稿改成**结构化卡片**。
 *
 * 设计稿（deliverables/gstack/design/mock-1-reminder.png）实测：
 *   状态栏保留在顶部（藏青，左时钟右日期），卡片只占状态栏以下的 210px；
 *   左列：红色药丸图标 + 标题「该吃药了」 / 明细行 / 橙色提示「请及时服用」
 *         底部橙色圆角按钮「长按机身顶部大按键确认」
 *   右侧：90x90 头像圆（与对话态同一位置、同一尺寸）
 *
 * 为什么**必须不透明**：用户原话是「屏幕的图片不再显示」—— 半透明会让
 * 底下的表情图透出来，看起来像"提醒叠在屏保上"，老人第一反应是
 * "设备坏了"，而不是"有事要做"。
 *
 * ⚠️ 字号：2026-10-22 第一版用 transform_scale 把 20px 放大 2 倍，真机
 * **整屏纯白、一个字都没有**（详见下面 label 创建处的注释）。现在仍是
 * 原生 20px。要真正的大字，正确做法是往 assets 分区加一套 30px 的 cbin
 * 字库（分区 8MB、已用 2.57MB，余量充足），而不是缩放。
 *
 * ⚠️ 文案契约（与 reminder.c 的 elder_reminder_screen_text() 一一对应）：
 *     第 0 行（第一个非空行）  标题
 *     以「请」开头的行          橙色提示语
 *     其余行                    明细，**逐行**列出（放不下时压行距、再滚动，
 *                               不裁字 —— 见下面"明细行数 → 高度"那段）
 *   解析放在显示层、而不是让 reminder.c 传结构体：elder_ui_hooks_t 只能在
 *   末尾追加，改签名要同时动 elder 组件、xiaozhi 的 merge 副本和两处调用点；
 *   而这段文案本来就是"给人看的一小段话"，按行拆是最小改动。
 *   改一边必须改另一边 —— 只改一边的表现是"提示语跑到明细里去了"。
 */
void LcdDisplay::ShowReminder(const char* text) {
    DisplayLockGuard lock(this);
    /* 全屏提醒是最高优先级的界面内容（吃药/喝水这类医疗级提示）。
     * 跑马灯虽然被 overlay 整块盖住看不见，但动画照样每 16ms 改一次坐标、
     * 让 LVGL 每帧重绘一块被遮住的区域 —— 白耗电，且提醒挂 4 小时。
     * 停掉它；提醒撤掉后如果仍是待机态，AntiBurnInStep 会把它接回来。 */
    StopIdleMarquee();
    auto lvgl_theme = static_cast<LvglTheme*>(current_theme_);
    auto screen = lv_screen_active();
    if (screen == nullptr || lvgl_theme == nullptr) {
        return;
    }

    const int bar_h = StatusBarH();
    const int card_h = LV_VER_RES - bar_h;
    const int col_x = MainPadX();
    const int col_w = LV_HOR_RES - AvatarRight() - AvatarSize() - col_x
                      - LV_HOR_RES * 8 / 320;
    const auto text_font = lvgl_theme->text_font() != nullptr
                               ? lvgl_theme->text_font()->font()
                               : nullptr;

    if (reminder_overlay_ == nullptr) {
        /* 卡片只占状态栏以下：设计稿里状态栏（时钟/日期）是**保留**的，
         * 老人正看着提醒时仍然需要知道现在几点。盖住整屏会让时钟消失，
         * 而这条提醒最长会挂 4 小时。 */
        reminder_overlay_ = lv_obj_create(screen);
        lv_obj_set_size(reminder_overlay_, LV_HOR_RES, card_h);
        lv_obj_set_pos(reminder_overlay_, 0, bar_h);
        lv_obj_set_style_radius(reminder_overlay_, 0, 0);
        lv_obj_set_style_border_width(reminder_overlay_, 0, 0);
        lv_obj_set_style_pad_all(reminder_overlay_, 0, 0);
        lv_obj_set_style_bg_color(reminder_overlay_, lv_color_hex(kColorMainBg), 0);
        lv_obj_set_style_bg_opa(reminder_overlay_, LV_OPA_COVER, 0);
        lv_obj_clear_flag(reminder_overlay_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_style_layout(reminder_overlay_, LV_LAYOUT_NONE, 0);

        /* ---- 标题行：红色药丸图标 + 标题 ----
         * 药丸用 lv_obj 画（12x22 圆角矩形），不用 FontAwesome：
         * font_awesome_20_4 里**没有**药丸/胶囊字形（只有 bell /
         * triangle_exclamation 这类），拿感叹号当药丸只会更让人看不懂。 */
        lv_obj_t* title_row = lv_obj_create(reminder_overlay_);
        lv_obj_set_pos(title_row, col_x, LV_VER_RES * 12 / 240);
        lv_obj_set_size(title_row, col_w, LV_SIZE_CONTENT);
        lv_obj_set_style_bg_opa(title_row, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(title_row, 0, 0);
        lv_obj_set_style_pad_all(title_row, 0, 0);
        lv_obj_set_style_pad_column(title_row, LV_HOR_RES * 6 / 320, 0);
        lv_obj_set_flex_flow(title_row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(title_row, LV_FLEX_ALIGN_START,
                              LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_clear_flag(title_row, LV_OBJ_FLAG_SCROLLABLE);

        lv_obj_t* pill = lv_obj_create(title_row);
        lv_obj_set_size(pill, LV_HOR_RES * 12 / 320, LV_VER_RES * 22 / 240);
        lv_obj_set_style_radius(pill, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(pill, lv_color_hex(kColorPillRed), 0);
        lv_obj_set_style_bg_opa(pill, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(pill, 0, 0);
        lv_obj_set_style_pad_all(pill, 0, 0);
        lv_obj_clear_flag(pill, LV_OBJ_FLAG_SCROLLABLE);

        reminder_title_ = lv_label_create(title_row);
        lv_obj_set_width(reminder_title_, col_w - LV_HOR_RES * 18 / 320);
        lv_label_set_long_mode(reminder_title_, LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_align(reminder_title_, LV_TEXT_ALIGN_LEFT, 0);
        lv_obj_set_style_text_color(reminder_title_, lv_color_hex(kColorTextDark), 0);
        if (text_font != nullptr) {
            lv_obj_set_style_text_font(reminder_title_, text_font, 0);
        }
        lv_label_set_text(reminder_title_, "");

        /* ---- 明细：多行左对齐 ----
         * 2026-11-03 用户需求：「如果累积的有多条提醒，则同时显示在一屏上」。
         * 原来是固定 88px 高、行距 4px ⇒ 30px 字体只能放 2 行，第 3 条起被裁掉；
         * 而橙色提示语又从下面压上来（BOTTOM -52），实际只剩 1.7 行。
         *
         * 现在按**可用带宽**算：从标题下沿到橙色按钮上沿，行距从 0 起按需
         * 压到 -6，压到底仍放不下就纵向滚动（见 ShowReminder 里那段说明）。
         * 320x240 上是 (166-52)/38 = 3 行 —— 正好覆盖"同分钟三条"这个真实场景
         * （降压药 + 降糖药 + 喝水）。行数不写死，换字体/换屏自动跟着变。
         *
         * ⚠️ 必须是 LONG_WRAP 而不是 LONG_DOT（2026-11-04 用户第 3 条）：
         * 药名/剂量是家属自由输入，一行写不下时要**折行**，不能像 LONG_DOT
         * 那样在末尾打点 —— 那正是"第二种药只显示了前几个字"的观感来源。 */
        reminder_items_ = lv_label_create(reminder_overlay_);
        lv_obj_set_pos(reminder_items_, col_x, LV_VER_RES * 52 / 240);
        lv_obj_set_width(reminder_items_, col_w);
        lv_obj_set_height(reminder_items_, LV_VER_RES * 88 / 240);   /* 创建时占位，下面按实测行高重设 */
        lv_label_set_long_mode(reminder_items_, LV_LABEL_LONG_WRAP);
        lv_obj_set_style_text_align(reminder_items_, LV_TEXT_ALIGN_LEFT, 0);
        lv_obj_set_style_text_color(reminder_items_, lv_color_hex(kColorTextDark), 0);
        /* 行距初值 0：明细是"一行一件事"，靠换行分行，默认不加额外留白。
         * 放不下时 ShowReminder 会临时压成负值（见那里的 ①②③）。 */
        lv_obj_set_style_text_line_space(reminder_items_, 0, 0);
        if (text_font != nullptr) {
            lv_obj_set_style_text_font(reminder_items_, text_font, 0);
        }
        lv_label_set_text(reminder_items_, "");

        /* ---- 橙色提示语（贴底往上数第三个） ---- */
        reminder_hint_ = lv_label_create(reminder_overlay_);
        lv_obj_set_width(reminder_hint_, col_w);
        lv_label_set_long_mode(reminder_hint_, LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_align(reminder_hint_, LV_TEXT_ALIGN_LEFT, 0);
        lv_obj_set_style_text_color(reminder_hint_, lv_color_hex(kColorAccent), 0);
        if (text_font != nullptr) {
            lv_obj_set_style_text_font(reminder_hint_, text_font, 0);
        }
        lv_label_set_text(reminder_hint_, "");
        lv_obj_align(reminder_hint_, LV_ALIGN_BOTTOM_LEFT, col_x, -LV_VER_RES * 52 / 240);

        /* ---- 橙色圆角按钮 ----
         * 文案是**界面常量**不是数据：设计稿这一行说的是"怎么操作这台机器"，
         * 与具体是哪条提醒无关，所以写在显示层，不由 reminder.c 下发。
         * 与 2026-10-29 定下的口径一致（主行动必须是"长按按钮"，
         * 不能写"说一句"—— 待机态不上传音频，说话不会有反应）。 */
        reminder_button_ = lv_obj_create(reminder_overlay_);
        lv_obj_set_size(reminder_button_, LV_HOR_RES - LV_HOR_RES * 84 / 320,
                        LV_VER_RES * 28 / 240);
        lv_obj_align(reminder_button_, LV_ALIGN_BOTTOM_MID, 0, -LV_VER_RES * 16 / 240);
        lv_obj_set_style_radius(reminder_button_, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(reminder_button_, lv_color_hex(kColorAccent), 0);
        lv_obj_set_style_bg_opa(reminder_button_, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(reminder_button_, 0, 0);
        lv_obj_set_style_pad_all(reminder_button_, 0, 0);
        lv_obj_clear_flag(reminder_button_, LV_OBJ_FLAG_SCROLLABLE);

        reminder_button_label_ = lv_label_create(reminder_button_);
        lv_label_set_text(reminder_button_label_, "长按机身顶部大按键确认");
        lv_obj_set_width(reminder_button_label_, lv_pct(100));
        lv_label_set_long_mode(reminder_button_label_, LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_align(reminder_button_label_, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_color(reminder_button_label_, lv_color_white(), 0);
        if (text_font != nullptr) {
            lv_obj_set_style_text_font(reminder_button_label_, text_font, 0);
        }
        lv_obj_center(reminder_button_label_);

        /* ---- 右侧头像圆：与对话态同一位置、同一尺寸 ---- */
        reminder_avatar_ = lv_obj_create(reminder_overlay_);
        lv_obj_set_size(reminder_avatar_, AvatarSize(), AvatarSize());
        lv_obj_set_pos(reminder_avatar_, LV_HOR_RES - AvatarRight() - AvatarSize(),
                       AvatarTop() - bar_h);
        lv_obj_set_style_radius(reminder_avatar_, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_clip_corner(reminder_avatar_, true, 0);
        lv_obj_set_style_bg_color(reminder_avatar_, lv_color_hex(kColorAvatar), 0);
        lv_obj_set_style_bg_opa(reminder_avatar_, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(reminder_avatar_, 0, 0);
        lv_obj_set_style_pad_all(reminder_avatar_, 0, 0);
        lv_obj_clear_flag(reminder_avatar_, LV_OBJ_FLAG_SCROLLABLE);

        lv_obj_t* avatar_img = lv_image_create(reminder_avatar_);
        lv_obj_set_size(avatar_img, lv_pct(100), lv_pct(100));
        lv_image_set_inner_align(avatar_img, LV_IMAGE_ALIGN_CONTAIN);
        /* 头像内容跟随当前表情（提醒态是 SetEmotion("shocked") 设的），
         * 每次 ShowReminder 都会重新同步一次 src —— 见函数末尾。 */
        lv_image_set_src(avatar_img, nullptr);
    }

    /* ---- 拆行 ---- */
    char title_buf[96] = {0};
    char items_buf[256] = {0};
    char hint_buf[96] = {0};
    const char* p = (text != nullptr) ? text : "";
    int line_no = 0;
    while (*p != '\0') {
        const char* nl = strchr(p, '\n');
        size_t len = (nl != nullptr) ? (size_t)(nl - p) : strlen(p);
        char line[96];
        if (len >= sizeof(line)) {
            len = sizeof(line) - 1;
        }
        memcpy(line, p, len);
        line[len] = '\0';
        /* 去掉行尾的 \r / 空格（文案来自固件，但仍可能带 Windows 换行） */
        while (len > 0 && (line[len - 1] == '\r' || line[len - 1] == ' ')) {
            line[--len] = '\0';
        }
        if (len > 0) {
            if (line_no == 0) {
                snprintf(title_buf, sizeof(title_buf), "%s", line);
            } else if (strncmp(line, "请", strlen("请")) == 0) {
                /* ⚠️ 这里必须用 strncmp 而不是 `line[0] == '请'`：
                 * 汉字在 UTF-8 里占 3 字节，`line[0]` 只是首字节 0xE8，
                 * 而 '请' 是多字节字符常量（值实现定义）。写成后者永远不会
                 * 成立 —— 表现是"橙色提示语跑到明细行里去了"，而明细行
                 * 用的是深色，屏上看起来就是"提示语颜色不对"，
                 * 一眼看不出是判断写错。 */
                snprintf(hint_buf, sizeof(hint_buf), "%s", line);
            } else if (items_buf[0] == '\0') {
                snprintf(items_buf, sizeof(items_buf), "%s", line);
            } else if (strlen(items_buf) + len + 2 <= sizeof(items_buf)) {
                strncat(items_buf, "\n", sizeof(items_buf) - strlen(items_buf) - 1);
                strncat(items_buf, line, sizeof(items_buf) - strlen(items_buf) - 1);
            }
            line_no++;
        }
        if (nl == nullptr) {
            break;
        }
        p = nl + 1;
    }

    lv_label_set_text(reminder_title_, title_buf);
    lv_label_set_text(reminder_items_, items_buf);
    lv_label_set_text(reminder_hint_, hint_buf);
    if (items_buf[0] == '\0') {
        lv_obj_add_flag(reminder_items_, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_remove_flag(reminder_items_, LV_OBJ_FLAG_HIDDEN);
    }
    /* 明细行数 → 高度（2026-11-04 用户第 3 条：**换行不裁字**）。
     *
     * 用户原话：「两种药是按照一行显示的，所以第二种药只显示了前几个字。」
     * 根因是文案层把多味药拼成了一行（见 reminder.c 的 elder_reminder_screen_text），
     * 183px / 30px 字号一行只放 6 个汉字，第二味药自然只剩前几个字。文案层已
     * 改成"每味药一行"，这里负责**把每一行都显示出来**：
     *
     *   ① 明细 label 是 LV_LABEL_LONG_WRAP（见创建处）—— 一行太长时换行，
     *      不是用 LONG_DOT 在末尾打点（那才是"裁字"）；
     *   ② 放不下时**优先压 text_line_space**（最多压到 -6px：30px 字形本身
     *      只占 30px，行高 38 里的 8px 是留白，压掉几像素不会让字叠在一起）；
     *   ③ 压到底仍放不下 ⇒ 该区域纵向滚动，行一行都不少（宁可老人滚一下，
     *      也不能让他看到半截药名 —— 吃错药的代价远大于多滚一次）。
     * 上一版是 `if (item_lines > fit_lines) item_lines = fit_lines;` 直接截断，
     * 表现正是"第二种药只显示了前几个字"。 */
    int item_lines = 0;
    if (items_buf[0] != '\0') {
        item_lines = 1;
        for (const char* q = items_buf; *q != '\0'; q++) {
            if (*q == '\n') item_lines++;
        }
    }
    const int item_line_h = (text_font != nullptr)
                                ? (int)text_font->line_height
                                : (LV_VER_RES * 38 / 240);
    const int items_top = LV_VER_RES * 52 / 240;
    const int items_bottom = card_h - LV_VER_RES * 44 / 240;   /* 橙色按钮上沿 */
    const int items_avail = items_bottom - items_top;
    /* 每次进来先把行距复位：上一次可能压过，不复位就会一直累积。 */
    int item_line_space = 0;
    lv_obj_set_style_text_line_space(reminder_items_, item_line_space, 0);
    if (item_lines > 0 && item_line_h > 0 && items_avail > 0) {
        /* ① 先按原始行距试；② 放不下就一行一行地压行距（最多 -6）。 */
        int fit_lines = items_avail / item_line_h;
        for (int ls = -1; fit_lines < item_lines && ls >= -6; ls--) {
            const int h = item_line_h + ls;
            if (h <= 0) break;
            fit_lines = items_avail / h;
            item_line_space = ls;
        }
        if (fit_lines < item_lines) {
            item_line_space = 0;      /* 压到底也没用，回到原始行距再滚动 */
            fit_lines = items_avail / item_line_h;
        }
        lv_obj_set_style_text_line_space(reminder_items_, item_line_space, 0);
        const int row_h = item_line_h + item_line_space;
        const int need_h = item_lines * row_h;
        if (need_h <= items_avail) {
            /* 全部放得下：高度贴内容，不滚动（滚动条/可滚标志一起清掉，
             * 否则 LVGL 会为"内容刚好等于视口"的容器留一个滚动状态）。 */
            lv_obj_set_height(reminder_items_, need_h);
            lv_obj_clear_flag(reminder_items_, LV_OBJ_FLAG_SCROLLABLE);
        } else {
            /* 压到底仍放不下：区域固定为可用高度，**纵向滚动**，一行不裁。 */
            lv_obj_set_height(reminder_items_, items_avail);
            lv_obj_add_flag(reminder_items_, LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_set_scroll_dir(reminder_items_, LV_DIR_VER);
            lv_obj_set_scrollbar_mode(reminder_items_, LV_SCROLLBAR_MODE_OFF);
            lv_obj_scroll_to_y(reminder_items_, 0, LV_ANIM_OFF);
        }
    }

    /* 橙色提示语只在一行明细时出现（2026-11-03）：
     * 它固定在 BOTTOM -52（y≈120），与 2 行以上的明细是**同一块**空间。
     * 两条以上提醒时，明细比"请及时服用"重要得多 —— 让位给明细。
     * 提示语的内容也没有丢：橙色按钮上就写着"长按…确认"。 */
    const bool show_hint = (hint_buf[0] != '\0') && (item_lines <= 1);
    if (show_hint) {
        lv_obj_remove_flag(reminder_hint_, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(reminder_hint_, LV_OBJ_FLAG_HIDDEN);
    }

    /* 头像跟着当前表情走：emoji_image_ 的 src 就是"小安现在的脸"
     * （提醒态由 elder_bridge 设成 shocked）。直接拷 src 而不是再查一次
     * 表情集合 —— 查一次要判空、要处理主题换过的情况，而这里只要"和
     * 头像圆上那张一样"就够了。
     *
     * 兜底：emoji_image_ 还没有 src（比如开机后第一条提醒来得比 SetEmotion
     * 早）时按名字取 shocked。不留这一步的话，卡片右侧会是一个**空白的蓝圆**，
     * 老人看到的是"小安的脸没了"，比没有头像更吓人。 */
    if (reminder_avatar_ != nullptr) {
        lv_obj_t* avatar_img = lv_obj_get_child(reminder_avatar_, 0);
        if (avatar_img != nullptr) {
            const void* src = (emoji_image_ != nullptr)
                                  ? lv_image_get_src(emoji_image_)
                                  : nullptr;
            if (src == nullptr) {
                auto* th = static_cast<LvglTheme*>(current_theme_);
                /* emoji_collection() 返回 shared_ptr（按值），不能写 auto* —— 
                 * 那是「从三元表达式推导指针」，会报 unable to deduce。 */
                auto col = (th != nullptr) ? th->emoji_collection() : nullptr;
                const LvglImage* fb = (col != nullptr)
                                          ? col->GetEmojiImage("shocked")
                                          : nullptr;
                if (fb != nullptr) {
                    src = fb->image_dsc();
                }
            }
            if (src != nullptr) {
                lv_image_set_src(avatar_img, src);
            }
        }
    }

    lv_obj_remove_flag(reminder_overlay_, LV_OBJ_FLAG_HIDDEN);
    /* 每次都要提到最前：期间可能新建了别的对象（通知、预览图），
     * 后建的对象会盖在 overlay 上面，表现是"字被吃掉一半"。 */
    lv_obj_move_foreground(reminder_overlay_);
    ESP_LOGI(TAG, "全屏提醒卡片：标题=%s 明细=%d 行 提示=%s",
             title_buf, line_no - 1, hint_buf);
}

void LcdDisplay::HideReminder() {
    DisplayLockGuard lock(this);
    if (reminder_overlay_ == nullptr) {
        return;
    }
    /* 只隐藏、不删除：删除后下次要重建（LVGL 对象创建在 PSRAM 碎片化时
     * 会失败），而这块 overlay 常驻只占一个对象 + 一个 label。 */
    lv_obj_add_flag(reminder_overlay_, LV_OBJ_FLAG_HIDDEN);
}
// <<< END elder-reminder-fullscreen <<<
