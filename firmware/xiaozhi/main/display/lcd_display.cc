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

void LcdDisplay::InitializeLcdThemes() {
    auto text_font = std::make_shared<LvglBuiltInFont>(&BUILTIN_TEXT_FONT);
    auto icon_font = std::make_shared<LvglBuiltInFont>(&BUILTIN_ICON_FONT);
    auto large_icon_font = std::make_shared<LvglBuiltInFont>(&font_awesome_30_4);

    // light theme
    auto light_theme = new LvglTheme("light");
    light_theme->set_background_color(lv_color_hex(0xFFFFFF));
    light_theme->set_text_color(lv_color_hex(0x000000));
    light_theme->set_chat_background_color(lv_color_hex(0xE0E0E0));
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
     * 不透明遮罩留在屏上（表现为"重启后屏幕全黑，只有状态栏")。 */
    if (reminder_overlay_ != nullptr) {
        lv_obj_del(reminder_overlay_);
        reminder_overlay_ = nullptr;
        reminder_label_ = nullptr;
    }
    /* <<< END elder-reminder-fullscreen <<< */
    if (chat_message_label_ != nullptr) {
        lv_obj_del(chat_message_label_);
    }
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
// >>> BEGIN elder-layout-70-30 (2026-10-19 布局改造，勿手改；被上游 sync 冲掉时
// merge_to_xiaozhi.py --check 会红，按 README「上游定制补丁」节重贴) >>>
// 简洁风格布局（本板启用，320x240 物理 / swap_xy 后 LVGL 逻辑 240x320 竖屏）：
//
//    ┌──────────────────────────┐
//    │ 状态栏 WiFi/静音/电量（不变）│  ← top_bar_ / status_bar_
//    ├──────────────────────────┤
//    │                          │
//    │      字幕区（约 70%）      │  ← bottom_bar_（全宽，紧贴状态栏下方）
//    │                          │
//    ├──────────────┬───────────┤
//    │  （空）        │ 图片区 30% │  ← emoji_box_ / preview_image_（右下角）
//    └──────────────┴───────────┘
//
// 2026-10-19 用户拍板：对话界面上下 70/30 分割，右下角 30% 显示图标/表情，
// 字幕占 70%，顶部状态栏保留。改动只涉及默认风格分支；WECHAT 气泡风格
// （CONFIG_USE_WECHAT_MESSAGE_STYLE，本板未启用）不动。
// <<< END elder-layout-70-30 >>>
void LcdDisplay::SetupUI() {
    DisplayLockGuard lock(this);
    LvglTheme* lvgl_theme = static_cast<LvglTheme*>(current_theme_);
    auto text_font = lvgl_theme->text_font()->font();
    auto icon_font = lvgl_theme->icon_font()->font();
    auto large_icon_font = lvgl_theme->large_icon_font()->font();

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

    /* Container - used as background */
    container_ = lv_obj_create(screen);
    lv_obj_set_size(container_, LV_HOR_RES, LV_VER_RES);
    lv_obj_set_style_radius(container_, 0, 0);
    lv_obj_set_style_pad_all(container_, 0, 0);
    lv_obj_set_style_border_width(container_, 0, 0);
    lv_obj_set_style_bg_color(container_, lvgl_theme->background_color(), 0);
    lv_obj_set_style_border_color(container_, lvgl_theme->border_color(), 0);

    /* Center layer: emoji_box_ - 屏幕中央大图区（42% x 46%，2026-10-20 横屏布局）
     *
     * 为什么从「右下角 30%x30%」改成「正中大图」：
     * 老人坐在 1-2 米外看这块屏，右下角 30% 的图形只有 ~72px，
     * 实际观感是「看不清、像是屏保」。放大到中央之后，
     * 「小安在听你说话 / 小安在回答」这件事才看得出来。
     *
     * 布局按**横屏 320x240** 算（config.h 已把 swap_xy/mirror 关掉）：
     *   top_bar_   顶部状态栏 ≈ 28
     *   emoji_box_ 320*42%=134 宽 × 240*46%=110 高，居中 y=-8 ⇒ 范围 59..169
     *   bottom_bar_ 贴底 240*28%=67 高 ⇒ 从 173 起
     * 两者留 3px 间隙，不重叠。
     */
    emoji_box_ = lv_obj_create(screen);
    lv_obj_set_size(emoji_box_, lv_pct(42), lv_pct(46));
    lv_obj_set_style_bg_opa(emoji_box_, LV_OPA_TRANSP, 0);
    lv_obj_set_style_pad_all(emoji_box_, 0, 0);
    lv_obj_set_style_border_width(emoji_box_, 0, 0);
    /* 待机跑马灯会把这个盒子的子对象挪到边界之外（滚出左边缘），盒子的
     * 可滚动标志必须关掉：否则 LVGL 会把越界部分算成"可滚动内容"，
     * 触摸拖动时整块图片区会跟着手指偏移，看起来像界面错位。 */
    lv_obj_clear_flag(emoji_box_, LV_OBJ_FLAG_SCROLLABLE);
    // 偏移量与 preview_image_ 必须逐字一致（两者是同一块区域，只是内容不同）
    lv_obj_align(emoji_box_, LV_ALIGN_CENTER, 0, -lvgl_theme->spacing(4));

    emoji_label_ = lv_label_create(emoji_box_);
    lv_obj_set_style_text_font(emoji_label_, large_icon_font, 0);
    lv_obj_set_style_text_color(emoji_label_, lvgl_theme->text_color(), 0);
    lv_label_set_text(emoji_label_, FONT_AWESOME_MICROCHIP_AI);
    // 盒子改为固定尺寸后必须显式居中（原来是 LV_SIZE_CONTENT 自适应）
    lv_obj_center(emoji_label_);

    emoji_image_ = lv_img_create(emoji_box_);
    // 表情图（twemoji_64 等）尺寸随主题而定：铺满图片区 + CONTAIN 等比缩放，
    // 再大的原图也只会缩小不会溢出（lv_image 的 SIZE_CHANGED 会自动重算）
    lv_obj_set_size(emoji_image_, lv_pct(100), lv_pct(100));
    lv_image_set_inner_align(emoji_image_, LV_IMAGE_ALIGN_CONTAIN);
    lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);

    /* Middle layer: preview_image_ - 与 emoji_box_ 同一块正中大图区 */
    preview_image_ = lv_image_create(screen);
    lv_obj_set_size(preview_image_, lv_pct(42), lv_pct(46));
    // CONTAIN：LVGL 自动按图片区等比缩放（替代原先手算 128*width_/w 的公式，
    // 那个公式按半屏宽算，在固定图片区下会把图放出界）
    lv_image_set_inner_align(preview_image_, LV_IMAGE_ALIGN_CONTAIN);
    lv_obj_align(preview_image_, LV_ALIGN_CENTER, 0, -lvgl_theme->spacing(4));
    lv_obj_add_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);

    /* Layer 1: Top bar - for status icons */
    top_bar_ = lv_obj_create(screen);
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
    lv_obj_align(top_bar_, LV_ALIGN_TOP_MID, 0, 0);

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
    lv_obj_set_width(notification_label_, LV_HOR_RES * 0.75);
    lv_obj_set_style_text_align(notification_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(notification_label_, lvgl_theme->text_color(), 0);
    lv_label_set_text(notification_label_, "");
    lv_obj_align(notification_label_, LV_ALIGN_CENTER, 0, 0);
    lv_obj_add_flag(notification_label_, LV_OBJ_FLAG_HIDDEN);

    status_label_ = lv_label_create(status_bar_);
    lv_obj_set_width(status_label_, LV_HOR_RES * 0.75);
    lv_label_set_long_mode(status_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_set_style_text_align(status_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(status_label_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_text_font(status_label_, text_font, 0);  /* 显式设，不靠继承 */
    lv_label_set_text(status_label_, Lang::Strings::INITIALIZING);
    lv_obj_align(status_label_, LV_ALIGN_CENTER, 0, 0);

    /* Top layer: Bottom bar - 字幕区（2026-10-20 横屏布局：贴屏幕底部，高度 28%）
     *
     * 与 2026-10-19 的「紧贴状态栏、占 70%」相比，这一版把字幕压到底部固定一条，
     * 把屏幕中央让给放大的表情区（见上面 emoji_box_ 的注释）。
     * 老人看屏幕的顺序因此变成：先看中间「小安的脸」，再低头看底部「她说了什么」。
     */
    bottom_bar_ = lv_obj_create(screen);
    lv_obj_set_width(bottom_bar_, LV_HOR_RES);
    // 高度 = 屏幕 28%；下限 60 保底（横屏 240 高时 28%≈67，约三行中文）。
    int subtitle_height = LV_VER_RES * 28 / 100;
    if (subtitle_height < 60) {
        subtitle_height = 60;
    }
    lv_obj_set_height(bottom_bar_, subtitle_height);
    lv_obj_set_style_min_height(bottom_bar_, 60, 0);
    lv_obj_set_style_radius(bottom_bar_, 0, 0);
    lv_obj_set_style_bg_color(bottom_bar_, lvgl_theme->background_color(), 0);
    lv_obj_set_style_text_color(bottom_bar_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_pad_top(bottom_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_bottom(bottom_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_left(bottom_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_pad_right(bottom_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_border_width(bottom_bar_, 0, 0);
    // 贴屏幕底部（中央让给放大的表情区）
    lv_obj_align(bottom_bar_, LV_ALIGN_BOTTOM_MID, 0, 0);

    /* chat_message_label_ placed in bottom_bar_ and vertically centered */
    chat_message_label_ = lv_label_create(bottom_bar_);
    lv_label_set_text(chat_message_label_, "");
    lv_obj_set_width(chat_message_label_, LV_HOR_RES - lvgl_theme->spacing(8)); // Subtract left and right padding
    lv_label_set_long_mode(chat_message_label_, LV_LABEL_LONG_WRAP); // Auto wrap mode
    lv_obj_set_style_text_align(chat_message_label_, LV_TEXT_ALIGN_CENTER, 0); // Center text alignment
    lv_obj_set_style_text_color(chat_message_label_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_text_font(chat_message_label_, text_font, 0);  /* 显式设，不靠继承 */
    lv_obj_align(chat_message_label_, LV_ALIGN_CENTER, 0, 0); // Vertically and horizontally centered in bottom_bar_

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

void LcdDisplay::SetChatMessage(const char* role, const char* content) {
    /* 有新字幕 = 界面正在变化 ⇒ 重置防烧屏空闲计时（见 StartAntiBurnInTimer） */
    last_ui_change_tick_ = lv_tick_get();
    DisplayLockGuard lock(this);
    if (chat_message_label_ == nullptr) {
        ESP_LOGW("FontDiag", "SetChatMessage: chat_message_label_ is NULL!");
        return;
    }
    ESP_LOGW("FontDiag", "SetChatMessage role=%s content_len=%d label=%p w=%d h=%d",
             role ? role : "(null)",
             content ? (int)strlen(content) : -1,
             (void*)chat_message_label_,
             (int)lv_obj_get_width(chat_message_label_),
             (int)lv_obj_get_height(chat_message_label_));
    /* 2026-10-23 诊断：h=36 之谜。文件里 line_height=25，而这里 h=36，
     * 数学上只可能是 label 实际字体的 line_height 运行时读出来不是 25
     * （或 ls≠0 / 有 padding）。把 label 的**解析后**字体与排版参数
     * 全部打出来 —— 注意打的是 label 自己，不是 theme 里的字体。 */
    {
        const lv_font_t* label_font = lv_obj_get_style_text_font(chat_message_label_, LV_PART_MAIN);
        ESP_LOGW("FontDiag", "chat label font=%p lh=%d bl=%d ls=%d les=%d long=%d pad=%d,%d style_h=%d",
                 (const void*)label_font,
                 label_font ? label_font->line_height : -1,
                 label_font ? label_font->base_line : -1,
                 (int)lv_obj_get_style_text_line_space(chat_message_label_, LV_PART_MAIN),
                 (int)lv_obj_get_style_text_letter_space(chat_message_label_, LV_PART_MAIN),
                 (int)lv_label_get_long_mode(chat_message_label_),
                 (int)lv_obj_get_style_pad_top(chat_message_label_, LV_PART_MAIN),
                 (int)lv_obj_get_style_pad_bottom(chat_message_label_, LV_PART_MAIN),
                 (int)lv_obj_get_style_height(chat_message_label_, LV_PART_MAIN));
    }
    lv_label_set_text(chat_message_label_, content);
    /* 同步跑一次布局，打「这段文字 + 当前字体」的真实高度：与函数开头
     * 打的上一次高度对照，才能把 25→36 的跳变钉死在哪一侧。 */
    lv_obj_update_layout(chat_message_label_);
    ESP_LOGW("FontDiag", "after set_text+layout: w=%d h=%d",
             (int)lv_obj_get_width(chat_message_label_),
             (int)lv_obj_get_height(chat_message_label_));
}
#endif

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

/* 滚动速度 35 px/s。
 *
 * 上限由「看得清」定：图片区宽 134px，图 110px，一张划过约 7 秒；再快
 * （>50px/s）老人只看到一团颜色在动，认不出是什么表情。
 * 下限由「别以为卡住」定：慢于 ~25px/s 时相邻两帧位移不到 1px，
 * 观感接近静止，起不到「设备活着」的信号作用。
 * 取中间值 35 ⇒ 单张过屏 (134+110)/35 ≈ 7.0 秒，7 张一轮约 49 秒，
 * 与防烧屏的 60 秒量级一致（一轮下来整块图片区的像素全换过）。 */
constexpr int32_t kIdleMarqueeSpeedPxPerSec = 35;

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
        self->AdvanceIdleMarqueeImage();
    }
    self->idle_marquee_last_x_ = x;
    lv_obj_set_x(self->idle_marquee_image_, x);
}

void LcdDisplay::AdvanceIdleMarqueeImage() {
    if (idle_marquee_ready_.empty() || idle_marquee_image_ == nullptr) {
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
        lv_image_set_src(idle_marquee_image_, image->image_dsc());
        return;
    }
}

bool LcdDisplay::StartIdleMarquee() {
    if (idle_marquee_running_) {
        return true;
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

    /* 尺寸：表情源是 128x128 方图，图片区 134x110 ⇒ 按**盒子高度**取正方形，
     * CONTAIN 等比缩到 110x110，正好铺满高度、宽度也留出左右各 12px 的
     * 进出空间（滚出/滚入时不会突然整块跳出来）。 */
    lv_obj_update_layout(emoji_box_);
    const int32_t box_w = lv_obj_get_width(emoji_box_);
    const int32_t box_h = lv_obj_get_height(emoji_box_);
    if (box_w <= 0 || box_h <= 0) {
        ESP_LOGW(TAG, "待机跑马灯：图片区尺寸异常（%dx%d），不启动",
                 (int)box_w, (int)box_h);
        return false;
    }

    if (idle_marquee_image_ == nullptr) {
        idle_marquee_image_ = lv_image_create(emoji_box_);
        lv_obj_set_style_pad_all(idle_marquee_image_, 0, 0);
        lv_obj_set_style_border_width(idle_marquee_image_, 0, 0);
        lv_image_set_inner_align(idle_marquee_image_, LV_IMAGE_ALIGN_CONTAIN);
        /* 只建一次，之后靠显隐复用：LVGL 对象创建在 PSRAM 碎片化时可能失败，
         * 而这块是待机态反复要用的东西。 */
        lv_obj_add_flag(idle_marquee_image_, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_set_size(idle_marquee_image_, box_h, box_h);

    idle_marquee_next_ = 0;
    const LvglImage* first = collection->GetEmojiImage(idle_marquee_ready_[0]);
    if (first == nullptr) {
        /* 上面刚校验过，走到这里说明这一瞬间主题换了 —— 交给下一轮兜底 */
        ESP_LOGW(TAG, "待机跑马灯：首帧取图失败，不启动");
        return false;
    }
    lv_image_set_src(idle_marquee_image_, first->image_dsc());
    idle_marquee_next_ = 1 % idle_marquee_ready_.size();

    if (emoji_image_ != nullptr) {
        lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
    }
    if (emoji_label_ != nullptr) {
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_remove_flag(idle_marquee_image_, LV_OBJ_FLAG_HIDDEN);

    /* x 从图片区右边缘外开始（对象左边缘 = box_w ⇒ 整张在图区之外），
     * 匀速左移到 -img_w（对象右边缘 = 0 ⇒ 整张移出左边缘）。一条 lv_anim
     * 用 repeat 无限循环，每轮回起点时 x 由动画自己复位。
     *
     * var 传 this 而不是 image：exec 回调里要拿 LcdDisplay 的成员（ready 列表、
     * 下一张下标），而 lv_anim 的 exec_cb 只给一个 void*。顺带获得一个好处：
     * lv_anim_start 会 remove_concurrent_anims（同 var + 同 exec_cb），
     * 所以本函数被重入也不会同时跑两条动画。 */
    const int32_t img_w = box_h;
    const uint32_t duration_ms =
        (uint32_t)((box_w + img_w) * 1000 / kIdleMarqueeSpeedPxPerSec);

    /* early_apply 默认开 ⇒ lv_anim_start 会立刻用 start_value 调一次回调。
     * 所以 last_x_ 必须在 start **之前**置成起点，否则首次回调会被当成
     * 「x 变大」而白白换掉一张图。 */
    idle_marquee_last_x_ = box_w;

    lv_anim_t anim;
    lv_anim_init(&anim);
    lv_anim_set_var(&anim, this);
    lv_anim_set_exec_cb(&anim, IdleMarqueeExecCb);
    lv_anim_set_values(&anim, box_w, -img_w);
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
    idle_marquee_last_x_ = 0;
    idle_marquee_next_ = 0;
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
        lv_obj_set_style_text_font(chat_message_label_, text_font, 0);
    }
    if (status_label_ != nullptr) {
        lv_obj_set_style_text_font(status_label_, text_font, 0);
    }
    if (notification_label_ != nullptr) {
        lv_obj_set_style_text_font(notification_label_, text_font, 0);
    }
    /* 全屏提醒的 label 是**运行时懒创建**的：换主题时它可能已经存在
     * （提醒挂着、用户又切了主题），也可能还没建（下次 ShowReminder 会用
     * 新主题的字体新建）。两条路都要覆盖，否则就是上一段注释里那种
     * 「改了字体但屏上不变」。 */
    if (reminder_label_ != nullptr) {
        lv_obj_set_style_text_font(reminder_label_, text_font, 0);
        lv_obj_set_style_text_color(reminder_label_, lvgl_theme->text_color(), 0);
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
    
    // Update top bar background color with 50% opacity
    if (top_bar_ != nullptr) {
        lv_obj_set_style_bg_opa(top_bar_, LV_OPA_50, 0);
        lv_obj_set_style_bg_color(top_bar_, lvgl_theme->background_color(), 0);
    }
    
    // Update status bar elements
    lv_obj_set_style_text_color(network_label_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_text_color(status_label_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_text_color(notification_label_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_text_color(mute_label_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_text_color(battery_label_, lvgl_theme->text_color(), 0);
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
    
    if (emoji_label_ != nullptr) {
        lv_obj_set_style_text_color(emoji_label_, lvgl_theme->text_color(), 0);
    }
    
    // Update bottom bar background color with 50% opacity
    if (bottom_bar_ != nullptr) {
        lv_obj_set_style_bg_opa(bottom_bar_, LV_OPA_50, 0);
        lv_obj_set_style_bg_color(bottom_bar_, lvgl_theme->background_color(), 0);
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
    if (bottom_bar_ != nullptr) {
        if (hide) {
            lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_remove_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
        }
    }
}

// >>> BEGIN elder-reminder-fullscreen (2026-10-22 需求 8) >>>
/* 全屏大字提醒。
 *
 * 为什么覆盖整屏、且**必须不透明**：用户原话是「屏幕的图片不再显示」——
 * 半透明会让中间那张表情图透出来，看起来像"提醒叠在屏保上"，
 * 老人第一反应是"设备坏了"，而不是"有事要做"。
 *
 * ⚠️ 字号：2026-10-22 第一版用 transform_scale 把 20px 放大 2 倍，真机
 * **整屏纯白、一个字都没有**（详见下面 label 创建处的注释）。现在退回
 * 原生 20px 全宽排版。要真正的大字，正确做法是往 assets 分区加一套
 * 30px 的 cbin 字库（分区 8MB、已用 1.59MB，余量充足），而不是缩放。
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

    if (reminder_overlay_ == nullptr) {
        reminder_overlay_ = lv_obj_create(screen);
        lv_obj_set_size(reminder_overlay_, LV_HOR_RES, LV_VER_RES);
        lv_obj_align(reminder_overlay_, LV_ALIGN_TOP_LEFT, 0, 0);
        lv_obj_set_style_radius(reminder_overlay_, 0, 0);
        lv_obj_set_style_border_width(reminder_overlay_, 0, 0);
        lv_obj_set_style_pad_all(reminder_overlay_, 0, 0);
        lv_obj_set_style_bg_color(reminder_overlay_, lvgl_theme->background_color(), 0);
        lv_obj_set_style_bg_opa(reminder_overlay_, LV_OPA_COVER, 0);
        /* 放大后的字要能画出 overlay 之外 → 不裁剪子对象 */
        lv_obj_add_flag(reminder_overlay_, LV_OBJ_FLAG_OVERFLOW_VISIBLE);
        lv_obj_clear_flag(reminder_overlay_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_style_layout(reminder_overlay_, LV_LAYOUT_NONE, 0);

        reminder_label_ = lv_label_create(reminder_overlay_);
        /* ⚠️ 2026-10-22 真机事故：这里原本用 transform_scale 放大 2 倍
         * （半屏宽排版 + 512 缩放）。真机表现是**整屏纯白、一个字都没有**
         * ——覆盖层画出来了，label 却没画出来，日志里连一条 LVGL 警告都没有。
         *
         * 缩放走的是 lv_refr.c 的 LV_LAYER_TYPE_TRANSFORM 分支：先
         * layer_get_area() 求变换后区域，求不出就 `return` **静默跳过整个对象**
         * （lv_refr.c:1218 那个 res != LV_RESULT_OK 的 return）。
         * 本工程 LV_USE_MATRIX / LV_DRAW_TRANSFORM_USE_MATRIX 都是 n，
         * 走的是老的逐像素变换路径 —— 这条路在本仓从未被验证过（全工程
         * 只有这一处用 transform_scale）。
         *
         * 结论：**先用确定能显示的原生字库把字画出来**，字小可以改，
         * 白屏等于老人什么都看不见。字号问题另案处理（assets 分区还有
         * 6.4MB 空余，可以放一套 30px 的 cbin 字库，不受 app 分区限制）。
         *
         * 布局取全宽而不是半宽：不缩放之后半宽排版会让每行只剩 8 个字，
         * 4 行字挤在中间，反而不如全宽两行好读。 */
        lv_obj_set_size(reminder_label_, LV_HOR_RES - 16, LV_VER_RES - 16);
        lv_label_set_long_mode(reminder_label_, LV_LABEL_LONG_WRAP);
        lv_obj_set_style_text_align(reminder_label_, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_color(reminder_label_, lvgl_theme->text_color(), 0);
        /* 行距放宽：20px 字在 240 高的屏上只占 4 行，加行距更好认 */
        lv_obj_set_style_text_line_space(reminder_label_, 8, 0);
        /* 字体**显式设**，不靠从 screen 继承 —— 与 status_label_/chat_message_label_
         * 同一条纪律（见 SetTheme 里 2026-10-22 字幕缺字的注释）。
         * 全屏提醒恰恰是最不能缺字的地方：老人只看到「该吃 了」这种残句，
         * 比什么都不显示更糟 —— 他可能按错事去办。 */
        if (lvgl_theme->text_font() != nullptr) {
            lv_obj_set_style_text_font(reminder_label_,
                                       lvgl_theme->text_font()->font(), 0);
        }
        lv_obj_center(reminder_label_);
    }

    lv_label_set_text(reminder_label_, (text != nullptr) ? text : "");
    lv_obj_remove_flag(reminder_overlay_, LV_OBJ_FLAG_HIDDEN);
    /* 每次都要提到最前：期间可能新建了别的对象（通知、预览图），
     * 后建的对象会盖在 overlay 上面，表现是"字被吃掉一半"。 */
    lv_obj_move_foreground(reminder_overlay_);
    ESP_LOGI(TAG, "全屏提醒：%s", (text != nullptr) ? text : "");
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
