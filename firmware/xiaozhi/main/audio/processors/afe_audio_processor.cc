#include "afe_audio_processor.h"
#include <esp_log.h>

#define PROCESSOR_RUNNING 0x01

#define TAG "AfeAudioProcessor"

/* 是否启用 WebRTC 降噪（算法，不需要 model 分区）。
 *
 * 开关直接用 esp-sr 自己的 choice（Kconfig.projbuild 的 SR_NSN_MODEL_LOAD），
 * 这样"关降噪"只是模板里一行，不必改代码。两个符号都处理，代码在**两种
 * 配置下都必须能编译** —— 否则 CI 只编到其中一支，另一支永远是没编过的
 * 死代码（本仓踩过：写了不跑比没写更坏）。 */
#ifdef CONFIG_SR_NSN_WEBRTC
constexpr bool kWebrtcNs = true;
#else
constexpr bool kWebrtcNs = false;
#endif

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

    char* ns_model_name = esp_srmodel_filter(models, ESP_NSNET_PREFIX, NULL);
    char* vad_model_name = esp_srmodel_filter(models, ESP_VADN_PREFIX, NULL);
    
    afe_config_t* afe_config = afe_config_init(input_format.c_str(), NULL, AFE_TYPE_VC, AFE_MODE_HIGH_PERF);
    afe_config->aec_mode = AEC_MODE_VOIP_HIGH_PERF;
    afe_config->vad_mode = VAD_MODE_0;
    afe_config->vad_min_noise_ms = 100;
    if (vad_model_name != nullptr) {
        afe_config->vad_model_name = vad_model_name;
    }

    if (ns_model_name != nullptr) {
        afe_config->ns_init = true;
        afe_config->ns_model_name = ns_model_name;
        afe_config->afe_ns_mode = AFE_NS_MODE_NET;
    } else if (kWebrtcNs) {
        /* 2026-11-04：**本机永远走这一支**，因为分区表里没有 `model` 分区
         * （partitions/v2/16m.csv 只有 nvs/otadata/phy_init/ota_0/ota_1/assets）。
         * 没有模型列表 ⇒ `esp_srmodel_filter(models, ESP_NSNET_PREFIX, NULL)`
         * 恒为 nullptr ⇒ 原来那句 `ns_init = false` 就是**唯一结局**。
         *
         * 后果：10-31 那次提交「降噪改用 NSNet2」**至今一帧降噪都没做**，
         * 开机日志的管线串是铁证（NS 槽是空的）：
         *   `[input] -> |AEC(SR_HIGH_PERF)| ->  -> |VAD(WebRTC)| -> |WakeNet(...)|`
         * 而且它当时把模板从 SR_NSN_WEBRTC 改成了 SR_NSN_NSNET2，等于
         * 把一个"不需要模型、本来能生效"的降噪换成了一个"永远加载不到"的。
         *
         * 现在改回 WebRTC NS：它是**算法**不是模型，不需要 model 分区。
         * 开关仍是 esp-sr 自己的 choice（CONFIG_SR_NSN_WEBRTC / NSNET2），
         * 想关掉降噪只改模板一行，不用碰代码。 */
        static char kWebrtcNsName[] = "WEBRTC";   // esp-sr 约定的伪模型名
        afe_config->ns_init = true;
        afe_config->ns_model_name = kWebrtcNsName;
        afe_config->afe_ns_mode = AFE_NS_MODE_WEBRTC;
    } else {
        afe_config->ns_init = false;
    }

    /* 2026-11-04：AGC 从「关」改成「开」。这是本轮唯一有实测证据支撑的改动。
     *
     * 证据（生产留证 wav `asr_empty_20261104-004208_bb05dae5.wav`，6.6 秒）：
     *   峰值 0.036（-29 dBFS）、rms 0.0026；而正常说话峰值该在 -6~-12 dBFS。
     *   同一时刻底噪只有 0.0003 ⇒ **SNR ≈ 37 dB，噪声根本不是问题，电平才是**。
     * 表现：老人说「你刚才没有说内容吗？」，ASR 只返回「当当。你刚才没有说。」
     *   —— 开头「当当」是上一句 TTS 的 AEC 残留，尾巴「内容吗」整段丢失。
     *   轻声的句尾字在被压低 25 dB 的信号里，识别器直接当成了噪声。
     *
     * 为什么是 AGC 而不是单纯乘一个固定增益：固定增益会把近处说话推到削波，
     * 而 AGC 对远场（轻声）多给、对近场（大声）少给 —— 正是"远距离也能识别"
     * 需要的特性。目标电平取 -6 dBFS（不是 WebRTC 默认的 -3）留 6 dB 余量，
     * 避免老人凑近说话时削顶（削顶对 ASR 的伤害比小声更大）。
     *
     * ⚠️ AGC 作用在 **AFE 输出（上行，ASR 听到的那一路）**，与播放通路
     * 完全无关 —— 不违反"声音已正常、不许再修出破音/卡顿"的硬约束。 */
    afe_config->agc_init = true;
    afe_config->agc_mode = AFE_AGC_MODE_WEBRTC;
    afe_config->agc_compression_gain_db = 9;
    afe_config->agc_target_level_dbfs = 6;
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
