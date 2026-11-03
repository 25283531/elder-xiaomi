#include "afe_audio_processor.h"
#include <esp_log.h>

#define PROCESSOR_RUNNING 0x01

#define TAG "AfeAudioProcessor"

AfeAudioProcessor::AfeAudioProcessor()
    : afe_data_(nullptr) {
    event_group_ = xEventGroupCreate();
}

void AfeAudioProcessor::Initialize(AudioCodec* codec, int frame_duration_ms, srmodel_list_t* models_list) {
    codec_ = codec;
    frame_samples_ = frame_duration_ms * 16000 / 1000;

    // Pre-allocate output buffer capacity
    output_buffer_.reserve(frame_samples_);

    int ref_num = codec_->input_reference() ? 1 : 0;

    std::string input_format;
    for (int i = 0; i < codec_->input_channels() - ref_num; i++) {
        input_format.push_back('M');
    }
    for (int i = 0; i < ref_num; i++) {
        input_format.push_back('R');
    }

    srmodel_list_t *models;
    if (models_list == nullptr) {
        models = esp_srmodel_init("model");
    } else {
        models = models_list;
    }

    char* vad_model_name = esp_srmodel_filter(models, ESP_VADN_PREFIX, NULL);

    afe_config_t* afe_config = afe_config_init(input_format.c_str(), NULL, AFE_TYPE_VC, AFE_MODE_HIGH_PERF);
    afe_config->aec_mode = AEC_MODE_VOIP_HIGH_PERF;
    afe_config->vad_mode = VAD_MODE_0;
    afe_config->vad_min_noise_ms = 100;
    if (vad_model_name != nullptr) {
        afe_config->vad_model_name = vad_model_name;
    }

    /* ══════════════════════════════════════════════════════════════════════
     * 降噪：只用算法（WebRTC NS）。NSNet2 / 模型相关的代码已全部删除。
     * ══════════════════════════════════════════════════════════════════════
     * 2026-11-04 定调：把 NSNet2 相关代码删掉，只用算法降噪。
     *
     * 为什么是"删"而不是"留着当备选"：NSNet2 在本机**永远不可能生效**。
     * 分区表 partitions/v2/16m.csv 只有 nvs / otadata / phy_init / ota_0 /
     * ota_1 / assets，**没有 model 分区** ⇒ esp_srmodel_init("model") 拿到
     * 空列表 ⇒ esp_srmodel_filter(models, ESP_NSNET_PREFIX, NULL) 恒为
     * nullptr。10-31 到 11-04 这两个星期里"降噪"一帧都没做、而构建与 CI
     * 全绿，就是这条走不到的死分支造成的。留着它只会让人以为"支持 NSNet2"。
     *
     * WebRTC NS 是纯算法（webrtc_create / webrtc_process），不读任何模型
     * 数据，与 model 分区无关。三件事必须同时成立：
     *   1) ns_init       = true
     *   2) afe_ns_mode   = AFE_NS_MODE_WEBRTC
     *   3) ns_model_name = nullptr        ← **不是** "WEBRTC"
     *
     * ⚠️⚠️ 第 3 条是 2026-11-04 那次「唤醒后设备必重启」的根因（用户连试
     * 三次三次重启，Guru Meditation: LoadProhibited，EXCVADDR=0）。
     * esp_afe_config.h 里那句
     *     AFE_NS_MODE_WEBRTC = 0, // please use model name of NS, SSP: "WEBRTC"
     * 误导了我，我照着写 ns_model_name = "WEBRTC" —— 而 AFE 只要看到
     * ns_model_name 非空，就会走 libnsnet.a 的 esp_nsnet_handle_from_name()
     * 按"模型名"去加载，在空模型列表里解析 "WEBRTC" ⇒ 空指针解引用。
     *
     * 崩溃点是**用 map 解析 backtrace 得到的，不是猜的**（2026-11-04 补测）：
     *   0x4205d86a  esp_afe_handle_from_config +0x1f92
     *   0x4205dc65  esp_afe_handle_from_config +0x238d
     *   0x42025f46  AfeAudioProcessor::Initialize +0xe6
     *   0x4200a59e  AudioService::EnableVoiceProcessing +0x22
     *   0x420188ee  Application::SetDeviceState +0x192
     *   ...         Application::OnWakeWordDetected ← MainEventLoop
     * 也就是说：唤醒 → 建 AFE → 死在 esp_afe_handle_from_config 里面，
     * 所以开机日志里**第二条** AFE 管线从来没出现过。
     *
     * WebRTC 是算法、不挂模型名，所以这里必须是 nullptr。
     */
    afe_config->ns_init = true;
    afe_config->afe_ns_mode = AFE_NS_MODE_WEBRTC;
    afe_config->ns_model_name = nullptr;

    /* ══════════════════════════════════════════════════════════════════════
     * AGC：上行（ASR 听到的那一路）自动增益
     * ══════════════════════════════════════════════════════════════════════
     * 证据（生产留证 wav `asr_empty_20261104-004208_bb05dae5.wav`，6.6 秒）：
     *   峰值 0.036（-29 dBFS）、rms 0.0026；而正常说话峰值该在 -6~-12 dBFS。
     *   同一时刻底噪只有 0.0003 ⇒ **SNR ≈ 37 dB：噪声根本不是问题，电平才是**。
     * 表现：老人说「你刚才没有说内容吗？」，ASR 只返回「当当。你刚才没有说。」
     *   —— 开头「当当」是上一句 TTS 的 AEC 残留，尾巴「内容吗」整段丢失。
     *   轻声的句尾字在被压低 25 dB 的信号里，识别器直接当成了噪声。
     *
     * 为什么是 AGC 而不是乘一个固定增益：固定增益会把近处说话推到削波，
     * 而 AGC 对远场（轻声）多给、对近场（大声）少给 —— 正是"远距离也能
     * 识别"需要的特性。
     *
     * 四个参数是互相配合的，改一个要看另外三个：
     *   agc_mode = WEBRTC         本管线里没有 WakeNet，WAKENET 模式不适用
     *                             （esp-sr 会打印 "wakenet is activated,
     *                             disable WebRTC AGC." 并直接关掉 AGC）
     *   compression_gain_db = 15  最大可补 15 dB。默认 9 只够补一半：实测差距
     *                             约 20~25 dB，只补 9 dB 补完仍然偏轻
     *   target_level_dbfs = 6     目标 -6 dBFS。**不用** WebRTC 默认的 -3：
     *                             老人凑近说话时 -3 会削顶，而削顶对 ASR 的
     *                             伤害比小声更大
     *   afe_linear_gain = 1.0f    固定线性增益保持 1.0。AGC 已经负责动态范围，
     *                             再叠一个固定增益只会把近场推爆
     *
     * ⚠️ AGC 作用在 **AFE 输出（上行）**，与播放通路完全无关 —— 不违反
     * "声音已正常、不许再修出破音/卡顿"的硬约束。
     *
     * ⚠️ 验证方法：开机日志里**第二条** AFE 管线（本文件创建的那条，特征是
     * 有 `AEC(VOIP_HIGH_PERF)`）必须出现 `|NS(WebRTC)|` 和 `|AGC(WebRTC)|`。
     * 只 grep 第一条（WakeNet 那条）会得出"没生效"的错误结论 —— 踩过。
     */
    afe_config->agc_init = true;
    afe_config->agc_mode = AFE_AGC_MODE_WEBRTC;
    afe_config->agc_compression_gain_db = 15;
    afe_config->agc_target_level_dbfs = 6;
    afe_config->afe_linear_gain = 1.0f;
    afe_config->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;

#ifdef CONFIG_USE_DEVICE_AEC
    afe_config->aec_init = true;
    afe_config->vad_init = false;
#else
    afe_config->aec_init = false;
    afe_config->vad_init = true;
#endif

    afe_iface_ = esp_afe_handle_from_config(afe_config);
    afe_data_ = afe_iface_->create_from_config(afe_config);
    
    /* 钉 core 1：AFE（AEC + 重采样）是纯计算任务，和 audio_input 放同一个核
     * 可以避免两核之间来回同步 PCM 缓冲（audio_input 也在 core 1）。
     * 原先用 xTaskCreate（tskNO_AFFINITY），实测落在 core 0，与 WiFi 抢核。
     *
     * 栈 4096→8192：fetch_with_delay 里跑 AEC/NS 神经网络推理 + VAD + 回调，
     * 是重计算深调用链。实测 4KB 时 audio_communication 起不来/栈溢出，
     * 表现是「AFE Ringbuffer of AFE(FEED) is full」狂刷（fetch 没在消费）、
     * 服务端收到 0ms 语音、唤醒后永远没答复（2026-10-20 实测）。
     * 与 elder_rmd 栈溢出同病根：重计算任务配了过小栈。 */
    xTaskCreatePinnedToCore([](void* arg) {
        auto this_ = (AfeAudioProcessor*)arg;
        this_->AudioProcessorTask();
        vTaskDelete(NULL);
    }, "audio_communication", 8192, this, 3, NULL, 1);
}

AfeAudioProcessor::~AfeAudioProcessor() {
    if (afe_data_ != nullptr) {
        afe_iface_->destroy(afe_data_);
    }
    vEventGroupDelete(event_group_);
}

size_t AfeAudioProcessor::GetFeedSize() {
    if (afe_data_ == nullptr) {
        return 0;
    }
    return afe_iface_->get_feed_chunksize(afe_data_);
}

void AfeAudioProcessor::Feed(std::vector<int16_t>&& data) {
    if (afe_data_ == nullptr) {
        return;
    }
    /* Stop() 后 fetch 循环停了，若继续 feed 会让 AFE Ringbuffer 灌满
     * （实测「Ringbuffer of AFE(FEED) is full」狂刷）。feed 与 fetch 必须
     * 同启同停：未运行时不灌，保持两侧对称。 */
    if (!IsRunning()) {
        return;
    }
    afe_iface_->feed(afe_data_, data.data());
}

void AfeAudioProcessor::Start() {
    xEventGroupSetBits(event_group_, PROCESSOR_RUNNING);
}

void AfeAudioProcessor::Stop() {
    xEventGroupClearBits(event_group_, PROCESSOR_RUNNING);
    if (afe_data_ != nullptr) {
        afe_iface_->reset_buffer(afe_data_);
    }
}

bool AfeAudioProcessor::IsRunning() {
    return xEventGroupGetBits(event_group_) & PROCESSOR_RUNNING;
}

void AfeAudioProcessor::OnOutput(std::function<void(std::vector<int16_t>&& data)> callback) {
    output_callback_ = callback;
}

void AfeAudioProcessor::OnVadStateChange(std::function<void(bool speaking)> callback) {
    vad_state_change_callback_ = callback;
}

void AfeAudioProcessor::AudioProcessorTask() {
    auto fetch_size = afe_iface_->get_fetch_chunksize(afe_data_);
    auto feed_size = afe_iface_->get_feed_chunksize(afe_data_);
    ESP_LOGI(TAG, "Audio communication task started, feed size: %d fetch size: %d",
        feed_size, fetch_size);

    while (true) {
        xEventGroupWaitBits(event_group_, PROCESSOR_RUNNING, pdFALSE, pdTRUE, portMAX_DELAY);

        auto res = afe_iface_->fetch_with_delay(afe_data_, portMAX_DELAY);
        if ((xEventGroupGetBits(event_group_) & PROCESSOR_RUNNING) == 0) {
            continue;
        }
        if (res == nullptr || res->ret_value == ESP_FAIL) {
            if (res != nullptr) {
                ESP_LOGI(TAG, "Error code: %d", res->ret_value);
            }
            continue;
        }

        // VAD state change
        if (vad_state_change_callback_) {
            if (res->vad_state == VAD_SPEECH && !is_speaking_) {
                is_speaking_ = true;
                vad_state_change_callback_(true);
            } else if (res->vad_state == VAD_SILENCE && is_speaking_) {
                is_speaking_ = false;
                vad_state_change_callback_(false);
            }
        }

        if (output_callback_) {
            size_t samples = res->data_size / sizeof(int16_t);
            
            // Add data to buffer
            output_buffer_.insert(output_buffer_.end(), res->data, res->data + samples);
            
            // Output complete frames when buffer has enough data
            while (output_buffer_.size() >= frame_samples_) {
                if (output_buffer_.size() == frame_samples_) {
                    // If buffer size equals frame size, move the entire buffer
                    output_callback_(std::move(output_buffer_));
                    output_buffer_.clear();
                    output_buffer_.reserve(frame_samples_);
                } else {
                    // If buffer size exceeds frame size, copy one frame and remove it
                    output_callback_(std::vector<int16_t>(output_buffer_.begin(), output_buffer_.begin() + frame_samples_));
                    output_buffer_.erase(output_buffer_.begin(), output_buffer_.begin() + frame_samples_);
                }
            }
        }
    }
}

void AfeAudioProcessor::EnableDeviceAec(bool enable) {
    if (enable) {
#if CONFIG_USE_DEVICE_AEC
        afe_iface_->disable_vad(afe_data_);
        afe_iface_->enable_aec(afe_data_);
#else
        ESP_LOGE(TAG, "Device AEC is not supported");
#endif
    } else {
        afe_iface_->disable_aec(afe_data_);
        afe_iface_->enable_vad(afe_data_);
    }
}
