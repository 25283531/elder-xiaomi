#ifndef _APPLICATION_H_
#define _APPLICATION_H_

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>
#include <esp_timer.h>

#include <string>
#include <mutex>
#include <deque>
#include <memory>

#include "protocol.h"
#include "ota.h"
#include "audio_service.h"
#include "device_state_event.h"


#define MAIN_EVENT_SCHEDULE (1 << 0)
#define MAIN_EVENT_SEND_AUDIO (1 << 1)
#define MAIN_EVENT_WAKE_WORD_DETECTED (1 << 2)
#define MAIN_EVENT_VAD_CHANGE (1 << 3)
#define MAIN_EVENT_ERROR (1 << 4)
#define MAIN_EVENT_CHECK_NEW_VERSION_DONE (1 << 5)
#define MAIN_EVENT_CLOCK_TICK (1 << 6)


enum AecMode {
    kAecOff,
    kAecOnDeviceSide,
    kAecOnServerSide,
};

class Application {
public:
    static Application& GetInstance() {
        static Application instance;
        return instance;
    }
    // 删除拷贝构造函数和赋值运算符
    Application(const Application&) = delete;
    Application& operator=(const Application&) = delete;

    void Start();
    void MainEventLoop();
    DeviceState GetDeviceState() const { return device_state_; }
    bool IsVoiceDetected() const { return audio_service_.IsVoiceDetected(); }
    void Schedule(std::function<void()> callback);
    void SetDeviceState(DeviceState state);
    void Alert(const char* status, const char* message, const char* emotion = "", const std::string_view& sound = "");
    void DismissAlert();
    void AbortSpeaking(AbortReason reason);
    void ToggleChatState();
    /* 一键唤醒 / 打断（2026-10-27 用户需求：板载用户按键要"按了就说话"）。
     *
     * 与 ToggleChatState 的**唯一**区别在「正在说话」这一支：
     *   ToggleChatState：说话中 → AbortSpeaking → 回待机
     *   WakeUp         ：说话中 → AbortSpeaking → **直接进收听**
     * 老人按键的意图是"我要说话"，不是"别说了"。其余状态（待机→收听、
     * 收听→停止）与 ToggleChatState 完全一致，保证同一个按键不出现
     * 两套语义 —— 老人记不住"什么时候按是停、什么时候按是说"。 */
    void WakeUp();
    void StartListening();
    void StopListening();
    void Reboot();
    void WakeWordInvoke(const std::string& wake_word);
    bool UpgradeFirmware(Ota& ota, const std::string& url = "");
    bool CanEnterSleepMode();
    void SendMcpMessage(const std::string& payload);
    void SetAecMode(AecMode mode);
    AecMode GetAecMode() const { return aec_mode_; }
    void PlaySound(const std::string_view& sound);
    AudioService& GetAudioService() { return audio_service_; }

private:
    Application();
    ~Application();

    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    std::unique_ptr<Protocol> protocol_;
    EventGroupHandle_t event_group_ = nullptr;
    esp_timer_handle_t clock_timer_handle_ = nullptr;
    volatile DeviceState device_state_ = kDeviceStateUnknown;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    AecMode aec_mode_ = kAecOff;
    std::string last_error_message_;
    AudioService audio_service_;

    /* ---- 字幕与语音对齐（2026-11-03，用户实报「字幕比声音快 2 秒以上」）----
     *
     * 现象：`sentence_start` 一到就把文字画上屏，而它的音频还要排在
     * 前面若干帧之后，并且设备播放链路里常驻 MAX_PLAYBACK_TASKS_IN_QUEUE
     * （16 帧 = 960ms）已解码音频 —— 再加上服务端 TTS 首字延迟，
     * 字幕必然跑在声音前面 1~2 秒。
     *
     * 做法：字幕**先入队**，等到「本句第一个字真的出声」再上屏。
     * 判据是音频帧计数：`sentence_start` 与它的音频帧走同一条 WS 连接、
     * 同一个任务、按序到达，所以收到字幕时已收 N 帧，就等于本句音频排在
     * 第 N 帧之后；PlayedFrameCount() 超过 N 的那一刻就是开口的时刻
     * （见 audio_service.h 的 ReceivedFrameCount 说明）。
     *
     * 为什么不在服务端把 `sentence_start` 往后压：那样只能省掉「TTS 首字
     * 延迟」这一段，设备侧那 960ms 缓冲仍然在；而设备侧这一条判据把两段
     * 一起吃掉，且**完全不碰音频通路**（用户的硬约束：声音已经正常，
     * 不许再修出破音/卡顿 —— 所以宁可不缩缓冲）。
     *
     * 线程：只在主任务里读写（QueueSubtitle / FlushSubtitles 都经由
     * Schedule 进来），定时器回调只负责 Schedule，不直接碰这个队列。 */
    struct PendingSubtitle {
        std::string text;
        int frame_mark = 0;      // 收到本句字幕时已收的音频帧数
        int64_t queued_us = 0;   // 入队时刻（兜底超时用）
    };
    std::deque<PendingSubtitle> pending_subtitles_;
    esp_timer_handle_t subtitle_timer_handle_ = nullptr;
    void QueueSubtitle(const std::string& text, int frame_mark);
    void FlushSubtitles(bool force);
    void ClearPendingSubtitles();

    bool has_server_time_ = false;
    bool aborted_ = false;
    int clock_ticks_ = 0;
    TaskHandle_t check_new_version_task_handle_ = nullptr;
    TaskHandle_t main_event_loop_task_handle_ = nullptr;

    void OnWakeWordDetected();
    void CheckNewVersion(Ota& ota);
    void CheckAssetsVersion();
    void ShowActivationCode(const std::string& code, const std::string& message);
    void SetListeningMode(ListeningMode mode);
};


class TaskPriorityReset {
public:
    TaskPriorityReset(BaseType_t priority) {
        original_priority_ = uxTaskPriorityGet(NULL);
        vTaskPrioritySet(NULL, priority);
    }
    ~TaskPriorityReset() {
        vTaskPrioritySet(NULL, original_priority_);
    }

private:
    BaseType_t original_priority_;
};

#endif // _APPLICATION_H_
