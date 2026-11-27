#include "audio_service.h"
#include <esp_log.h>
#include <algorithm>
#include <cstring>

#define RATE_CVT_CFG(_src_rate, _dest_rate, _channel)                                        \
    (esp_ae_rate_cvt_cfg_t) {                                                                \
        .src_rate = (uint32_t)(_src_rate), .dest_rate = (uint32_t)(_dest_rate),              \
        .channel = (uint8_t)(_channel), .bits_per_sample = ESP_AUDIO_BIT16, .complexity = 2, \
        .perf_type = ESP_AE_RATE_CVT_PERF_TYPE_SPEED,                                        \
    }

#define OPUS_DEC_CFG(_sample_rate, _frame_duration_ms)                                     \
    (esp_opus_dec_cfg_t) {                                                                 \
        .sample_rate = (uint32_t)(_sample_rate), .channel = ESP_AUDIO_MONO,                \
        .frame_duration =                                                                  \
            (esp_opus_dec_frame_duration_t)AS_OPUS_GET_FRAME_DRU_ENUM(_frame_duration_ms), \
        .self_delimited = false,                                                           \
    }

#if CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32P4 || CONFIG_IDF_TARGET_ESP32S31
#include "engines/afe_audio_engine.h"
#else
#include "engines/lite_audio_engine.h"
#endif

#define TAG "AudioService"

AudioService::AudioService() {
    event_group_ = xEventGroupCreate();
    // Bounded byte staging: never grows beyond one full PCM frame (§7.1).
    pcm_pending_bytes_.reserve(PCM_PLAYBACK_CHUNK_BYTES);
}

AudioService::~AudioService() {
    if (event_group_ != nullptr) {
        vEventGroupDelete(event_group_);
    }
    if (opus_encoder_ != nullptr) {
        esp_opus_enc_close(opus_encoder_);
    }
    if (opus_decoder_ != nullptr) {
        esp_opus_dec_close(opus_decoder_);
    }
    if (input_resampler_ != nullptr) {
        esp_ae_rate_cvt_close(input_resampler_);
    }
    if (output_resampler_ != nullptr) {
        esp_ae_rate_cvt_close(output_resampler_);
    }
}

void AudioService::Initialize(AudioCodec* codec) {
    codec_ = codec;
    codec_->Start();

    esp_opus_dec_cfg_t opus_dec_cfg =
        OPUS_DEC_CFG(codec->output_sample_rate(), OPUS_FRAME_DURATION_MS);
    auto ret = esp_opus_dec_open(&opus_dec_cfg, sizeof(esp_opus_dec_cfg_t), &opus_decoder_);
    if (opus_decoder_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create audio decoder, error code: %d", ret);
    } else {
        decoder_sample_rate_ = codec->output_sample_rate();
        decoder_duration_ms_ = OPUS_FRAME_DURATION_MS;
        decoder_frame_size_ = decoder_sample_rate_ / 1000 * OPUS_FRAME_DURATION_MS;
    }
    esp_opus_enc_config_t opus_enc_cfg = AS_OPUS_ENC_CONFIG();
    ret = esp_opus_enc_open(&opus_enc_cfg, sizeof(esp_opus_enc_config_t), &opus_encoder_);
    if (opus_encoder_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create audio encoder, error code: %d", ret);
    } else {
        encoder_sample_rate_ = 16000;
        encoder_duration_ms_ = OPUS_FRAME_DURATION_MS;
        esp_opus_enc_get_frame_size(opus_encoder_, &encoder_frame_size_, &encoder_outbuf_size_);
        encoder_frame_size_ = encoder_frame_size_ / sizeof(int16_t);
    }

    if (codec->input_sample_rate() != 16000) {
        esp_ae_rate_cvt_cfg_t input_resampler_cfg = RATE_CVT_CFG(
            codec->input_sample_rate(), ESP_AUDIO_SAMPLE_RATE_16K, codec->input_channels());
        auto resampler_ret = esp_ae_rate_cvt_open(&input_resampler_cfg, &input_resampler_);
        if (input_resampler_ == nullptr) {
            ESP_LOGE(TAG, "Failed to create input resampler, error code: %d", resampler_ret);
        }
    }

#if CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32P4 || CONFIG_IDF_TARGET_ESP32S31
    audio_engine_ = std::make_unique<AfeAudioEngine>();
#else
    audio_engine_ = std::make_unique<LiteAudioEngine>();
#endif
    audio_engine_->OnOutput([this](std::vector<int16_t>&& data) {
        // Drop any buffered AFE output after mute is engaged.
        if (microphone_muted_.load()) {
            return;
        }
        PushTaskToEncodeQueue(kAudioTaskTypeEncodeToSendQueue, std::move(data));
    });
    audio_engine_->OnVadStateChange([this](bool speaking) {
        voice_detected_ = speaking;
        if (callbacks_.on_vad_change) {
            callbacks_.on_vad_change(speaking);
        }
    });
    audio_engine_->OnWakeWordDetected([this](const std::string& wake_word) {
        xEventGroupClearBits(event_group_, AS_EVENT_WAKE_WORD_RUNNING);
        if (microphone_muted_.load()) {
            // AFE may finish processing a frame captured just before mute. Ignore it and
            // restore detection so unmuting does not leave wake-word detection disabled.
            EnableWakeWordDetection(true);
            return;
        }
        if (callbacks_.on_wake_word_detected) {
            callbacks_.on_wake_word_detected(wake_word);
        }
    });

    esp_timer_create_args_t audio_power_timer_args = {
        .callback =
            [](void* arg) {
                AudioService* audio_service = (AudioService*)arg;
                audio_service->CheckAndUpdateAudioPowerState();
            },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "audio_power_timer",
        .skip_unhandled_events = true,
    };
    esp_timer_create(&audio_power_timer_args, &audio_power_timer_);
}

void AudioService::Start() {
    service_stopped_.store(false);
    xEventGroupClearBits(event_group_, AS_EVENT_AUDIO_TESTING_RUNNING | AS_EVENT_WAKE_WORD_RUNNING |
                                           AS_EVENT_AUDIO_PROCESSOR_RUNNING |
                                           AS_EVENT_AUDIO_INPUT_STOP_REQUEST);

    esp_timer_start_periodic(audio_power_timer_, 1000000);

#if CONFIG_USE_AUDIO_PROCESSOR
    /* Start the audio input task */
    xTaskCreatePinnedToCore(
        [](void* arg) {
            AudioService* audio_service = (AudioService*)arg;
            audio_service->AudioInputTask();
            vTaskDelete(NULL);
        },
        "audio_input", 2048 * 3, this, 8, &audio_input_task_handle_, 0);

    /* Start the audio output task */
    xTaskCreate(
        [](void* arg) {
            AudioService* audio_service = (AudioService*)arg;
            audio_service->AudioOutputTask();
            vTaskDelete(NULL);
        },
        "audio_output", 2048 * 2, this, 4, &audio_output_task_handle_);
#else
    /* Start the audio input task */
    xTaskCreate(
        [](void* arg) {
            AudioService* audio_service = (AudioService*)arg;
            audio_service->AudioInputTask();
            vTaskDelete(NULL);
        },
        "audio_input", 2048 * 2, this, 8, &audio_input_task_handle_);

    /* Start the audio output task */
    xTaskCreate(
        [](void* arg) {
            AudioService* audio_service = (AudioService*)arg;
            audio_service->AudioOutputTask();
            vTaskDelete(NULL);
        },
        "audio_output", 2048, this, 4, &audio_output_task_handle_);
#endif

    /* Start the opus codec task */
    xTaskCreate(
        [](void* arg) {
            AudioService* audio_service = (AudioService*)arg;
            audio_service->OpusCodecTask();
            vTaskDelete(NULL);
        },
        "opus_codec", 2048 * 12, this, 2, &opus_codec_task_handle_);
}

void AudioService::Stop() {
    esp_timer_stop(audio_power_timer_);
    service_stopped_.store(true);
    xEventGroupSetBits(event_group_, AS_EVENT_AUDIO_TESTING_RUNNING | AS_EVENT_WAKE_WORD_RUNNING |
                                         AS_EVENT_AUDIO_PROCESSOR_RUNNING);

    bool notify_drained = false;
    {
        std::lock_guard<std::mutex> lock(audio_queue_mutex_);
        ++playback_generation_;
        audio_encode_queue_.clear();
        audio_decode_queue_.clear();
        audio_playback_queue_.clear();
        audio_testing_queue_.clear();
        notify_drained = MarkPlaybackDrainedLocked();
        audio_queue_cv_.notify_all();
    }
    if (notify_drained && callbacks_.on_playback_drained) {
        callbacks_.on_playback_drained();
    }
}

bool AudioService::ReadAudioData(std::vector<int16_t>& data, int sample_rate, int samples) {
    if (microphone_muted_.load()) {
        return false;
    }
    if (!codec_->input_enabled()) {
        esp_timer_stop(audio_power_timer_);
        esp_timer_start_periodic(audio_power_timer_, AUDIO_POWER_CHECK_INTERVAL_MS * 1000);
        codec_->EnableInput(true);
    }

    if (codec_->input_sample_rate() != sample_rate) {
        data.resize(samples * codec_->input_sample_rate() / sample_rate * codec_->input_channels());
        if (!codec_->InputData(data)) {
            return false;
        }
        if (input_resampler_ != nullptr) {
            std::lock_guard<std::mutex> lock(input_resampler_mutex_);
            uint32_t in_sample_num = data.size() / codec_->input_channels();
            uint32_t output_samples = 0;
            esp_ae_rate_cvt_get_max_out_sample_num(input_resampler_, in_sample_num,
                                                   &output_samples);
            auto resampled = std::vector<int16_t>(output_samples * codec_->input_channels());
            uint32_t actual_output = output_samples;
            esp_ae_rate_cvt_process(input_resampler_, (esp_ae_sample_t)data.data(), in_sample_num,
                                    (esp_ae_sample_t)resampled.data(), &actual_output);
            resampled.resize(actual_output * codec_->input_channels());
            data = std::move(resampled);
        }
    } else {
        data.resize(samples * codec_->input_channels());
        if (!codec_->InputData(data)) {
            return false;
        }
    }

    // Mute may have been pressed while the I2S read was in progress. Never pass that frame
    // to the debugger, AFE, or Opus encoder.
    if (microphone_muted_.load()) {
        data.clear();
        return false;
    }

    /* Update the last input time */
    last_input_time_ = std::chrono::steady_clock::now();
    debug_statistics_.input_count++;

#if CONFIG_USE_AUDIO_DEBUGGER
    // 音频调试：发送原始音频数据
    if (audio_debugger_ == nullptr) {
        audio_debugger_ = std::make_unique<AudioDebugger>();
    }
    audio_debugger_->Feed(data);
#endif

    return true;
}

void AudioService::AudioInputTask() {
    constexpr EventBits_t kAudioInputActiveBits = AS_EVENT_AUDIO_TESTING_RUNNING |
                                                  AS_EVENT_WAKE_WORD_RUNNING |
                                                  AS_EVENT_AUDIO_PROCESSOR_RUNNING;

    while (true) {
        EventBits_t bits = xEventGroupWaitBits(
            event_group_, kAudioInputActiveBits | AS_EVENT_AUDIO_INPUT_STOP_REQUEST, pdFALSE,
            pdFALSE, portMAX_DELAY);

        if (service_stopped_.load()) {
            // ADC continuous mode keeps its hardware mutex from start until stop,
            // so the input task that started it must also stop it before exiting.
            if (codec_->input_enabled()) {
                codec_->EnableInput(false);
            }
            break;
        }

        if (bits & AS_EVENT_AUDIO_INPUT_STOP_REQUEST) {
            xEventGroupClearBits(event_group_, AS_EVENT_AUDIO_INPUT_STOP_REQUEST);

            // Recheck the active state in this task. Audio capture may have been
            // enabled after the timer posted the stop request.
            bits = xEventGroupGetBits(event_group_);
            if ((bits & kAudioInputActiveBits) == 0) {
                if (codec_->input_enabled()) {
                    codec_->EnableInput(false);
                }
                // Do not process the stale active bits returned by waitBits().
                continue;
            }
        }

        if (microphone_muted_.load()) {
            // Only this task controls the codec RX channel. Stop sampling instead of merely
            // feeding zero-valued audio into the recognition pipeline.
            if (codec_->input_enabled()) {
                codec_->EnableInput(false);
            }
            // Feed silence to drain any audio already buffered inside AFE; output callbacks
            // are independently gated while muted.
            if (bits & (AS_EVENT_WAKE_WORD_RUNNING | AS_EVENT_AUDIO_PROCESSOR_RUNNING)) {
                std::vector<int16_t> silence(160 * codec_->input_channels(), 0);
                audio_engine_->Feed(std::move(silence));
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        if (audio_input_need_warmup_.exchange(false)) {
            vTaskDelay(pdMS_TO_TICKS(120));
            continue;
        }

        /* Used for audio testing in NetworkConfiguring mode by clicking the BOOT button */
        if (bits & AS_EVENT_AUDIO_TESTING_RUNNING) {
            bool testing_queue_full = false;
            {
                std::lock_guard<std::mutex> lock(audio_queue_mutex_);
                testing_queue_full = audio_testing_queue_.full();
            }
            if (testing_queue_full) {
                ESP_LOGW(TAG, "Audio testing queue is full, stopping audio testing");
                EnableAudioTesting(false);
                continue;
            }
            std::vector<int16_t> data;
            int samples = OPUS_FRAME_DURATION_MS * 16000 / 1000;
            if (ReadAudioData(data, 16000, samples)) {
                // If input channels is 2, we need to fetch the left channel data
                if (codec_->input_channels() == 2) {
                    auto mono_data = std::vector<int16_t>(data.size() / 2);
                    for (size_t i = 0, j = 0; i < mono_data.size(); ++i, j += 2) {
                        mono_data[i] = data[j];
                    }
                    data = std::move(mono_data);
                }
                PushTaskToEncodeQueue(kAudioTaskTypeEncodeToTestingQueue, std::move(data));
                continue;
            }
        }

        /* Feed the selected audio engine */
        if (bits & (AS_EVENT_WAKE_WORD_RUNNING | AS_EVENT_AUDIO_PROCESSOR_RUNNING)) {
            int samples = 160;  // 10ms
            std::vector<int16_t> data;
            if (ReadAudioData(data, 16000, samples)) {
                audio_engine_->Feed(std::move(data));
                continue;
            }
        }

        // Read timeout/error should not terminate the input task.
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    ESP_LOGW(TAG, "Audio input task stopped");
}

void AudioService::AudioOutputTask() {
    while (true) {
        std::unique_lock<std::mutex> lock(audio_queue_mutex_);
        audio_queue_cv_.wait(
            lock, [this]() { return !audio_playback_queue_.empty() || service_stopped_.load(); });
        if (service_stopped_.load()) {
            break;
        }

        auto task = std::move(audio_playback_queue_.front());
        audio_playback_queue_.pop_front();
        output_in_flight_ = true;
        audio_queue_cv_.notify_all();
        lock.unlock();

        if (!codec_->output_enabled()) {
            esp_timer_stop(audio_power_timer_);
            esp_timer_start_periodic(audio_power_timer_, AUDIO_POWER_CHECK_INTERVAL_MS * 1000);
            codec_->EnableOutput(true);
        }

        if (task.playback_id != 0 && callbacks_.on_playback_progress) {
            callbacks_.on_playback_progress(task.playback_id, task.media_position_ms);
        }

        codec_->OutputData(task.pcm);

        /* Update the last output time */
        last_output_time_ = std::chrono::steady_clock::now();
        debug_statistics_.playback_count++;

        bool notify_drained = false;
        lock.lock();
#if CONFIG_USE_SERVER_AEC
        /* Record the timestamp for server AEC */
        if (task.timestamp > 0) {
            if (timestamp_queue_.full()) {
                timestamp_queue_.pop_front();
            }
            timestamp_queue_.push_back(task.timestamp);
        }
#endif
        output_in_flight_ = false;
        notify_drained = MarkPlaybackDrainedLocked();
        audio_queue_cv_.notify_all();
        lock.unlock();

        if (notify_drained && callbacks_.on_playback_drained) {
            callbacks_.on_playback_drained();
        }
    }

    ESP_LOGW(TAG, "Audio output task stopped");
}

void AudioService::OpusCodecTask() {
    while (true) {
        std::unique_lock<std::mutex> lock(audio_queue_mutex_);
        audio_queue_cv_.wait(lock, [this]() {
            return service_stopped_.load() || !audio_encode_queue_.empty() ||
                   (!audio_decode_queue_.empty() &&
                    audio_playback_queue_.size() < MAX_PLAYBACK_TASKS_IN_QUEUE);
        });
        if (service_stopped_.load()) {
            break;
        }

        /* Decode the audio from decode queue */
        if (!audio_decode_queue_.empty() &&
            audio_playback_queue_.size() < MAX_PLAYBACK_TASKS_IN_QUEUE) {
            auto packet = std::move(audio_decode_queue_.front());
            audio_decode_queue_.pop_front();
            decode_in_flight_ = true;
            const uint32_t generation = playback_generation_;
            audio_queue_cv_.notify_all();
            lock.unlock();

            AudioTask task;
            task.type = kAudioTaskTypeDecodeToPlaybackQueue;
            task.timestamp = packet->timestamp;
            task.playback_id = packet->playback_id;
            task.media_position_ms = packet->media_position_ms;

            SetDecodeSampleRate(packet->sample_rate, packet->frame_duration);
            bool decoded = false;
            if (opus_decoder_ != nullptr) {
                task.pcm.resize(decoder_frame_size_);
                esp_audio_dec_in_raw_t raw = {
                    .buffer = (uint8_t*)(packet->payload.data()),
                    .len = (uint32_t)(packet->payload.size()),
                    .consumed = 0,
                    .frame_recover = ESP_AUDIO_DEC_RECOVERY_NONE,
                };
                esp_audio_dec_out_frame_t out_frame = {
                    .buffer = (uint8_t*)(task.pcm.data()),
                    .len = (uint32_t)(task.pcm.size() * sizeof(int16_t)),
                    .decoded_size = 0,
                };
                esp_audio_dec_info_t dec_info = {};
                std::unique_lock<std::mutex> decoder_lock(decoder_mutex_);
                auto ret = esp_opus_dec_decode(opus_decoder_, &raw, &out_frame, &dec_info);
                decoder_lock.unlock();
                if (ret == ESP_AUDIO_ERR_OK) {
                    task.pcm.resize(out_frame.decoded_size / sizeof(int16_t));
                    if (decoder_sample_rate_ != codec_->output_sample_rate() &&
                        output_resampler_ != nullptr) {
                        uint32_t target_size = 0;
                        esp_ae_rate_cvt_get_max_out_sample_num(output_resampler_, task.pcm.size(),
                                                               &target_size);
                        std::vector<int16_t> resampled(target_size);
                        uint32_t actual_output = target_size;
                        esp_ae_rate_cvt_process(output_resampler_, (esp_ae_sample_t)task.pcm.data(),
                                                task.pcm.size(), (esp_ae_sample_t)resampled.data(),
                                                &actual_output);
                        resampled.resize(actual_output);
                        task.pcm = std::move(resampled);
                    }
                    decoded = true;
                } else {
                    ESP_LOGE(TAG, "Failed to decode audio after resize, error code: %d", ret);
                }
            } else {
                ESP_LOGE(TAG, "Audio decoder is not configured");
            }

            lock.lock();
            if (decoded && generation == playback_generation_ && !service_stopped_.load()) {
                audio_playback_queue_.push_back(std::move(task));
            }
            decode_in_flight_ = false;
            debug_statistics_.decode_count++;
            const bool notify_drained = MarkPlaybackDrainedLocked();
            audio_queue_cv_.notify_all();
            lock.unlock();
            if (notify_drained && callbacks_.on_playback_drained) {
                callbacks_.on_playback_drained();
            }
            lock.lock();
        }
        /* Encode the audio to send queue */
        if (!audio_encode_queue_.empty()) {
            auto task = std::move(audio_encode_queue_.front());
            audio_encode_queue_.pop_front();
            const uint32_t mute_generation = microphone_mute_generation_.load();
            audio_queue_cv_.notify_all();
            lock.unlock();
            if (microphone_muted_.load()) {
                lock.lock();
                continue;
            }

            auto packet = std::make_unique<AudioStreamPacket>();
            packet->frame_duration = OPUS_FRAME_DURATION_MS;
            packet->sample_rate = 16000;
            packet->timestamp = task.timestamp;

            if (opus_encoder_ != nullptr && task.pcm.size() == encoder_frame_size_) {
                packet->payload.resize(encoder_outbuf_size_);
                esp_audio_enc_in_frame_t in = {
                    .buffer = (uint8_t*)(task.pcm.data()),
                    .len = (uint32_t)(encoder_frame_size_ * sizeof(int16_t)),
                };
                esp_audio_enc_out_frame_t out = {
                    .buffer = packet->payload.data(),
                    .len = (uint32_t)encoder_outbuf_size_,
                    .encoded_bytes = 0,
                };
                auto ret = esp_opus_enc_process(opus_encoder_, &in, &out);
                if (ret == ESP_AUDIO_ERR_OK) {
                    bool queued = false;
                    if (!microphone_muted_.load() &&
                        mute_generation == microphone_mute_generation_.load()) {
                        packet->payload.resize(out.encoded_bytes);
                        if (task.type == kAudioTaskTypeEncodeToSendQueue) {
                            std::lock_guard<std::mutex> lock2(audio_queue_mutex_);
                            // Recheck under the queue lock: mute may have started after encoding.
                            if (!microphone_muted_.load() &&
                                mute_generation == microphone_mute_generation_.load()) {
                                /* Never let a full send queue stall encoding: stale realtime
                                 * audio is useless to the server, so drop the oldest packet. */
                                if (audio_send_queue_.size() >= MAX_SEND_PACKETS_IN_QUEUE) {
                                    audio_send_queue_.pop_front();
                                }
                                audio_send_queue_.push_back(std::move(packet));
                                queued = true;
                            }
                        } else if (task.type == kAudioTaskTypeEncodeToTestingQueue) {
                            bool testing_queue_full = false;
                            {
                                std::lock_guard<std::mutex> lock2(audio_queue_mutex_);
                                if (!microphone_muted_.load() &&
                                    mute_generation == microphone_mute_generation_.load()) {
                                    testing_queue_full =
                                        !audio_testing_queue_.push_back(std::move(packet));
                                    queued = !testing_queue_full;
                                }
                            }
                            if (testing_queue_full && callbacks_.on_audio_testing_queue_full) {
                                callbacks_.on_audio_testing_queue_full();
                            }
                        }
                    }
                    if (queued) {
                        if (task.type == kAudioTaskTypeEncodeToSendQueue &&
                            callbacks_.on_send_queue_available) {
                            callbacks_.on_send_queue_available();
                        }
                        debug_statistics_.encode_count++;
                    }
                } else {
                    ESP_LOGE(TAG, "Failed to encode audio, error code: %d", ret);
                }
            } else {
                ESP_LOGE(TAG,
                         "Failed to encode audio: encoder not configured or invalid frame size "
                         "(got %u, expected %u)",
                         task.pcm.size(), encoder_frame_size_);
            }
            lock.lock();
        }
    }

    ESP_LOGW(TAG, "Opus codec task stopped");
}

void AudioService::SetDecodeSampleRate(int sample_rate, int frame_duration) {
    if (decoder_sample_rate_ == sample_rate && decoder_duration_ms_ == frame_duration) {
        return;
    }
    std::unique_lock<std::mutex> decoder_lock(decoder_mutex_);
    if (opus_decoder_ != nullptr) {
        esp_opus_dec_close(opus_decoder_);
        opus_decoder_ = nullptr;
    }
    decoder_lock.unlock();
    esp_opus_dec_cfg_t opus_dec_cfg = OPUS_DEC_CFG(sample_rate, frame_duration);
    auto ret = esp_opus_dec_open(&opus_dec_cfg, sizeof(esp_opus_dec_cfg_t), &opus_decoder_);
    if (opus_decoder_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create audio decoder, error code: %d", ret);
        return;
    }
    decoder_sample_rate_ = sample_rate;
    decoder_duration_ms_ = frame_duration;
    decoder_frame_size_ = decoder_sample_rate_ / 1000 * frame_duration;

    auto codec = Board::GetInstance().GetAudioCodec();
    if (decoder_sample_rate_ != codec->output_sample_rate()) {
        ESP_LOGI(TAG, "Resampling audio from %d to %d", decoder_sample_rate_,
                 codec->output_sample_rate());
        if (output_resampler_ != nullptr) {
            esp_ae_rate_cvt_close(output_resampler_);
            output_resampler_ = nullptr;
        }
        esp_ae_rate_cvt_cfg_t output_resampler_cfg =
            RATE_CVT_CFG(decoder_sample_rate_, codec->output_sample_rate(), ESP_AUDIO_MONO);
        auto resampler_ret = esp_ae_rate_cvt_open(&output_resampler_cfg, &output_resampler_);
        if (output_resampler_ == nullptr) {
            ESP_LOGE(TAG, "Failed to create output resampler, error code: %d", resampler_ret);
        }
    }
}

void AudioService::PushTaskToEncodeQueue(AudioTaskType type, std::vector<int16_t>&& pcm) {
    if (microphone_muted_.load()) {
        return;
    }
    AudioTask task;
    task.type = type;
    task.pcm = std::move(pcm);

    uint32_t dropped_total = 0;
    {
        /* Push the task to the encode queue */
        std::unique_lock<std::mutex> lock(audio_queue_mutex_);
        if (microphone_muted_.load()) {
            return;
        }

        /* If the task is to send queue, we need to set the timestamp */
        if (type == kAudioTaskTypeEncodeToSendQueue && !timestamp_queue_.empty()) {
            task.timestamp = timestamp_queue_.front();
            timestamp_queue_.pop_front();
        }

        /* Microphone audio is realtime, so drop the oldest frame instead of blocking.
         * Blocking here would stall the audio engine task (AFE fetch) and deadlock the
         * whole input pipeline when the send queue stops being drained (e.g. network
         * congestion or a failed UDP send). */
        if (audio_encode_queue_.size() >= MAX_ENCODE_TASKS_IN_QUEUE) {
            audio_encode_queue_.pop_front();
            dropped_total = ++debug_statistics_.encode_drop_count;
        }
        audio_encode_queue_.push_back(std::move(task));
        audio_queue_cv_.notify_all();
    }

    /* Log outside the lock (UART writes are slow and would starve the codec task),
     * at most once per second. */
    if (dropped_total > 0) {
        int64_t now = esp_timer_get_time();
        if (now - last_encode_drop_log_time_ >= 1000000) {
            last_encode_drop_log_time_ = now;
            ESP_LOGW(TAG, "Encode queue is full, dropping oldest frame (dropped %lu so far)",
                     (unsigned long)dropped_total);
        }
    }
}

bool AudioService::PushPacketToDecodeQueue(std::unique_ptr<AudioStreamPacket> packet, bool wait) {
    std::unique_lock<std::mutex> lock(audio_queue_mutex_);
    const uint32_t generation = playback_generation_;
    if (audio_decode_queue_.size() >= MAX_DECODE_PACKETS_IN_QUEUE) {
        if (wait) {
            audio_queue_cv_.wait(lock, [this, generation]() {
                return service_stopped_.load() || generation != playback_generation_ ||
                       audio_decode_queue_.size() < MAX_DECODE_PACKETS_IN_QUEUE;
            });
        } else {
            return false;
        }
    }
    if (service_stopped_.load() || generation != playback_generation_) {
        return false;
    }
    playback_drained_notified_ = false;
    audio_decode_queue_.push_back(std::move(packet));
    audio_queue_cv_.notify_all();
    return true;
}

std::unique_ptr<AudioStreamPacket> AudioService::PopPacketFromSendQueue() {
    std::lock_guard<std::mutex> lock(audio_queue_mutex_);
    if (microphone_muted_.load()) {
        audio_send_queue_.clear();
        return nullptr;
    }
    if (audio_send_queue_.empty()) {
        return nullptr;
    }
    auto packet = std::move(audio_send_queue_.front());
    audio_send_queue_.pop_front();
    audio_queue_cv_.notify_all();
    return packet;
}

void AudioService::EncodeWakeWord() {
    if (audio_engine_) {
        audio_engine_->EncodeWakeWordData();
    }
}

const std::string& AudioService::GetLastWakeWord() const {
    static const std::string empty;
    return audio_engine_ ? audio_engine_->GetLastDetectedWakeWord() : empty;
}

std::vector<std::string> AudioService::GetWakeWords() const {
    return audio_engine_initialized_ ? audio_engine_->GetWakeWords() : std::vector<std::string>{};
}

std::unique_ptr<AudioStreamPacket> AudioService::PopWakeWordPacket() {
    auto packet = std::make_unique<AudioStreamPacket>();
    if (audio_engine_ && audio_engine_->GetWakeWordOpus(packet->payload)) {
        return packet;
    }
    return nullptr;
}

void AudioService::EnableWakeWordDetection(bool enable) {
    ESP_LOGD(TAG, "%s wake word detection", enable ? "Enabling" : "Disabling");
    if (enable) {
        if (!InitializeAudioEngine()) {
            xEventGroupClearBits(event_group_, AS_EVENT_WAKE_WORD_RUNNING);
            return;
        }
#if !(CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32P4 || CONFIG_IDF_TARGET_ESP32S31)
        auto* lite_engine = static_cast<LiteAudioEngine*>(audio_engine_.get());
        if (!lite_engine->RestoreWakeWordResources()) {
            xEventGroupClearBits(event_group_, AS_EVENT_WAKE_WORD_RUNNING);
            return;
        }
#endif
        if (!audio_engine_->HasWakeWord()) {
            xEventGroupClearBits(event_group_, AS_EVENT_WAKE_WORD_RUNNING);
            return;
        }
        {
            std::lock_guard<std::mutex> lock(input_resampler_mutex_);
            if (input_resampler_ != nullptr) {
                esp_ae_rate_cvt_reset(input_resampler_);
            }
        }
        audio_engine_->EnableWakeWordDetection(true);
        xEventGroupSetBits(event_group_, AS_EVENT_WAKE_WORD_RUNNING);
    } else {
        if (audio_engine_initialized_) {
            audio_engine_->EnableWakeWordDetection(false);
        }
        xEventGroupClearBits(event_group_, AS_EVENT_WAKE_WORD_RUNNING);
    }
}

void AudioService::ReleaseWakeWordResources() {
#if !(CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32P4 || CONFIG_IDF_TARGET_ESP32S31)
    if (!audio_engine_initialized_) {
        return;
    }
    if (xEventGroupGetBits(event_group_) &
        (AS_EVENT_WAKE_WORD_RUNNING | AS_EVENT_AUDIO_PROCESSOR_RUNNING)) {
        ESP_LOGW(TAG, "Cannot release WakeNet while the audio engine is active");
        return;
    }
    static_cast<LiteAudioEngine*>(audio_engine_.get())->ReleaseWakeWordResources();
#endif
}

void AudioService::EnableVoiceProcessing(bool enable) {
    ESP_LOGD(TAG, "%s voice processing", enable ? "Enabling" : "Disabling");

    if (enable) {
        if (!InitializeAudioEngine()) {
            return;
        }
        ResetDecoder();
        audio_input_need_warmup_ = true;
        {
            std::lock_guard<std::mutex> lock(input_resampler_mutex_);
            if (input_resampler_ != nullptr) {
                esp_ae_rate_cvt_reset(input_resampler_);
            }
        }
        audio_engine_->EnableVoiceProcessing(true);
        xEventGroupSetBits(event_group_, AS_EVENT_AUDIO_PROCESSOR_RUNNING);
    } else {
        if (audio_engine_initialized_) {
            audio_engine_->EnableVoiceProcessing(false);
        }
        xEventGroupClearBits(event_group_, AS_EVENT_AUDIO_PROCESSOR_RUNNING);
    }
}

void AudioService::EnableAudioTesting(bool enable) {
    ESP_LOGI(TAG, "%s audio testing", enable ? "Enabling" : "Disabling");
    if (enable) {
        xEventGroupSetBits(event_group_, AS_EVENT_AUDIO_TESTING_RUNNING);
    } else {
        xEventGroupClearBits(event_group_, AS_EVENT_AUDIO_TESTING_RUNNING);
        /* Copy audio_testing_queue_ to audio_decode_queue_ */
        std::lock_guard<std::mutex> lock(audio_queue_mutex_);
        audio_decode_queue_.clear();
        audio_decode_queue_.swap(audio_testing_queue_);
        if (!audio_decode_queue_.empty()) {
            playback_drained_notified_ = false;
        }
        audio_queue_cv_.notify_all();
    }
}

void AudioService::EnableDeviceAec(bool enable) {
    ESP_LOGI(TAG, "%s device AEC", enable ? "Enabling" : "Disabling");
    device_aec_enabled_ = enable;

    if (audio_engine_initialized_) {
        audio_engine_->EnableDeviceAec(enable);
    } else {
        ESP_LOGI(TAG, "Deferring AEC change until the audio engine is initialized");
    }
}

void AudioService::SetMicrophoneMuted(bool muted) {
    const bool previous = microphone_muted_.exchange(muted);
    if (previous == muted) {
        return;
    }
    microphone_mute_generation_.fetch_add(1);
    ESP_LOGI(TAG, "Microphone %s", muted ? "muted" : "unmuted");

    // Invalidate audio captured before this transition, including a frame currently being
    // encoded by OpusCodecTask. Do not let queued speech leak out after mute is pressed.
    {
        std::lock_guard<std::mutex> lock(audio_queue_mutex_);
        audio_encode_queue_.clear();
        audio_send_queue_.clear();
        audio_testing_queue_.clear();
        timestamp_queue_.clear();
        audio_queue_cv_.notify_all();
    }
    if (audio_engine_initialized_ && audio_engine_ != nullptr) {
        audio_engine_->ClearWakeWordData();
    }
    if (!muted) {
        // Discard resampler history and the first input interval after I2S RX is restarted.
        {
            std::lock_guard<std::mutex> lock(input_resampler_mutex_);
            if (input_resampler_ != nullptr) {
                esp_ae_rate_cvt_reset(input_resampler_);
            }
        }
        audio_input_need_warmup_.store(true);
    }
}

void AudioService::SetCallbacks(AudioServiceCallbacks& callbacks) { callbacks_ = callbacks; }

void AudioService::PlaySound(const std::string_view& ogg) {
    if (!codec_->output_enabled()) {
        esp_timer_stop(audio_power_timer_);
        esp_timer_start_periodic(audio_power_timer_, AUDIO_POWER_CHECK_INTERVAL_MS * 1000);
        codec_->EnableOutput(true);
    }

    const auto* buf = reinterpret_cast<const uint8_t*>(ogg.data());
    size_t size = ogg.size();

    auto demuxer = std::make_unique<OggDemuxer>();
    demuxer->OnPacket(
        [this](const uint8_t* data, int sample_rate, int frame_duration_ms, size_t size) {
            auto packet = std::make_unique<AudioStreamPacket>();
            packet->sample_rate = sample_rate;
            packet->frame_duration = frame_duration_ms;
            packet->payload.resize(size);
            std::memcpy(packet->payload.data(), data, size);
            PushPacketToDecodeQueue(std::move(packet), true);
        });
    demuxer->Reset();
    demuxer->Process(buf, size);
}

bool AudioService::IsIdle() {
    std::lock_guard<std::mutex> lock(audio_queue_mutex_);
    return audio_encode_queue_.empty() && IsPlaybackDrainedLocked() && audio_testing_queue_.empty();
}

bool AudioService::IsPlaybackIdle() {
    std::lock_guard<std::mutex> lock(audio_queue_mutex_);
    return IsPlaybackDrainedLocked();
}

void AudioService::ResetDecoder() {
    bool notify_drained = false;
    {
        std::lock_guard<std::mutex> lock(audio_queue_mutex_);
        ++playback_generation_;
        std::unique_lock<std::mutex> decoder_lock(decoder_mutex_);
        if (opus_decoder_ != nullptr) {
            esp_opus_dec_reset(opus_decoder_);
        }
        decoder_lock.unlock();
        timestamp_queue_.clear();
        audio_decode_queue_.clear();
        audio_playback_queue_.clear();
        audio_testing_queue_.clear();
        notify_drained = MarkPlaybackDrainedLocked();
        audio_queue_cv_.notify_all();
    }
    if (notify_drained && callbacks_.on_playback_drained) {
        callbacks_.on_playback_drained();
    }
}

bool AudioService::BeginPcmPlayback(int input_sample_rate, int input_channels,
                                    uint32_t playback_id) {
    // 24 kHz pass-through only: no implicit resampling (§3.1).
    const int output_sample_rate = (codec_ != nullptr) ? codec_->output_sample_rate() : 0;
    if (codec_ == nullptr || input_sample_rate != output_sample_rate || input_channels != 1) {
        ESP_LOGE(TAG, "Unsupported PCM format: %d Hz / %d channels (need %d Hz / mono)",
                 input_sample_rate, input_channels, output_sample_rate);
        return false;
    }

    // Repeated BeginPcmPlayback() does not nest sessions: cancel the previous one first
    // (§3.3). AbortPcmPlayback() takes pcm_mutex_ itself, so it must run before we lock it.
    AbortPcmPlayback();

    // Power on the output ahead of the first frame (§6.5), mirroring PlaySound(): the amplifier
    // is already live when AudioOutputTask() writes frame 0, so the start of the utterance is
    // not swallowed. The periodic power check is re-armed first because
    // CheckAndUpdateAudioPowerState() stops the timer once both input and output are off; it
    // turns the output off again after AUDIO_POWER_TIMEOUT_MS without output, exactly like the
    // Opus path. Both AudioCodec::EnableOutput() (audio_codec.cc) and every board override
    // return early on a repeated same-value call, and AudioOutputTask() keeps its own fallback,
    // so this never double-triggers. Done outside the locks: opening the PA/I2S TX is slow and
    // must not extend a critical section shared with the audio output task.
    if (!codec_->output_enabled()) {
        esp_timer_stop(audio_power_timer_);
        esp_timer_start_periodic(audio_power_timer_, AUDIO_POWER_CHECK_INTERVAL_MS * 1000);
        codec_->EnableOutput(true);
    }

    std::lock_guard<std::mutex> pcm_lock(pcm_mutex_);
    std::lock_guard<std::mutex> queue_lock(audio_queue_mutex_);

    // Fixed cancellation tokens for this session (§6.2.1). playback_generation_ is a plain
    // uint32_t protected by audio_queue_mutex_, hence read under that lock;
    // pcm_stream_generation_ is atomic and only AbortPcmPlayback() increments it.
    pcm_session_pcm_gen_ = pcm_stream_generation_.load();
    pcm_session_playback_gen_ = playback_generation_;

    pcm_stream_active_ = true;
    pcm_playback_id_ = playback_id;
    pcm_media_position_ms_ = 0;
    pcm_pending_bytes_.clear();

    ESP_LOGI(TAG, "PCM playback started: %d Hz / %d channel, playback id %u", input_sample_rate,
             input_channels, (unsigned)playback_id);
    return true;
}

bool AudioService::WritePcmData(const uint8_t* data, size_t size, bool last) {
    if (service_stopped_.load()) {
        return false;
    }
    if (data == nullptr && size != 0) {
        return false;
    }

    std::lock_guard<std::mutex> lock(pcm_mutex_);
    // Order matters: check active before handling the empty block, otherwise an empty
    // non-final write would return true without a session (§3.3).
    if (!pcm_stream_active_) {
        ESP_LOGW(TAG, "PCM playback is not active, dropping %u bytes", (unsigned)size);
        return false;
    }
    // Fixed session tokens: never re-loaded or replaced here, never compared with each other.
    // Snapshotted before the empty-chunk paths so those paths can run the same recheck: an
    // empty chunk never reaches the authoritative check inside the playback queue.
    const uint32_t pcm_gen = pcm_session_pcm_gen_;
    const uint32_t playback_gen = pcm_session_playback_gen_;

    bool framed = false;  // did this call reach the authoritative cancel check via a frame?
    size_t offset = 0;
    while (offset < size) {
        const size_t room = PCM_PLAYBACK_CHUNK_BYTES - pcm_pending_bytes_.size();
        const size_t take = std::min(room, size - offset);
        pcm_pending_bytes_.insert(pcm_pending_bytes_.end(), data + offset, data + offset + take);
        offset += take;

        if (pcm_pending_bytes_.size() == PCM_PLAYBACK_CHUNK_BYTES) {
            if (!FlushPcmPendingBytes(false, pcm_gen, playback_gen)) {
                return false;
            }
            framed = true;
        }
    }

    // A final flush publishes the tail frame only when at least one complete sample is staged;
    // otherwise FlushPcmPendingBytes() enqueues nothing and returns true without any generation
    // check, so such a call has to pass the recheck below first (§7.2).
    if (last && pcm_pending_bytes_.size() >= sizeof(int16_t)) {
        // Tail frame: push every complete sample (may be < 1440 samples); a lone trailing
        // byte is discarded with a warning by FlushPcmPendingBytes(). The flush runs the
        // authoritative check inside PushPcmTaskToPlaybackQueue()'s critical section.
        return FlushPcmPendingBytes(true, pcm_gen, playback_gen);
    }

    // Short-block / empty-chunk recheck: nothing went through the authoritative cancellation
    // check inside PushPcmTaskToPlaybackQueue(). Compare the fixed session tokens once under a
    // brief, non-waiting audio_queue_mutex_ acquire (lock order stays pcm_mutex_ ->
    // audio_queue_mutex_), so a cancelled session stops the HTTP reader immediately.
    if (!framed) {
        std::lock_guard<std::mutex> queue_lock(audio_queue_mutex_);
        if (service_stopped_.load()) {
            return false;
        }
        if (pcm_gen != pcm_stream_generation_.load()) {
            return false;
        }
        if (playback_gen != playback_generation_) {
            return false;
        }
    }

    if (last) {
        // Reached only when no tail frame will be published (less than one sample staged, or
        // the frames of this call were already pushed): the flush just drops a lone trailing
        // byte with a warning and cannot enqueue anything.
        return FlushPcmPendingBytes(true, pcm_gen, playback_gen);
    }
    return true;
}

bool AudioService::EndPcmPlayback(uint32_t timeout_ms) {
    uint32_t pcm_gen = 0;
    uint32_t playback_gen = 0;
    bool flush_ok = true;
    {
        std::lock_guard<std::mutex> lock(pcm_mutex_);

        // No session: report whether the playback path is idle and change nothing (§9.1).
        if (!pcm_stream_active_) {
            return IsPlaybackIdle();
        }

        // Fixed tokens of this session: the drain wait identifies cancellation with them and
        // never replaces them with a freshly loaded generation (§6.2.1).
        pcm_gen = pcm_session_pcm_gen_;
        playback_gen = pcm_session_playback_gen_;

        // Tail frame (possibly < 1440 samples) goes through the same tokens as WritePcmData().
        flush_ok = FlushPcmPendingBytes(true, pcm_gen, playback_gen);

        // Close the writable flag on every path, before releasing pcm_mutex_, so a cancelled
        // session can never stay "active" while its tokens are stale (§9.1, §9.4).
        pcm_stream_active_ = false;
    }
    if (!flush_ok) {
        // The tail push already saw a stale token (Abort/Stop/ResetDecoder): do not wait.
        return false;
    }

    // Drain wait: holds audio_queue_mutex_ only. pcm_stream_generation_ is read atomically, so
    // the lock order pcm_mutex_ -> audio_queue_mutex_ is never reversed and AbortPcmPlayback()
    // (bump, notify, then take pcm_mutex_) cannot deadlock against this wait (§6.3, §9.1).
    std::unique_lock<std::mutex> lock(audio_queue_mutex_);
    audio_queue_cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms), [&]() {
        return service_stopped_.load() ||                   // service stopped
               pcm_gen != pcm_stream_generation_.load() ||  // AbortPcmPlayback()
               playback_gen != playback_generation_ ||      // Stop()/ResetDecoder()
               IsPlaybackDrainedLocked();                   // normal drain
    });

    // Cancellation wins over drain, drain wins over timeout; all three are read inside this one
    // critical section, consistent with the predicate (§9.1).
    if (service_stopped_.load()) {
        return false;
    }
    if (pcm_gen != pcm_stream_generation_.load()) {
        return false;
    }
    if (playback_gen != playback_generation_) {
        return false;
    }

    const bool drained = IsPlaybackDrainedLocked();
    if (!drained) {
        // Timeout path: the queue is left untouched so already queued PCM keeps playing.
        ESP_LOGW(TAG, "PCM playback drain timed out after %u ms", (unsigned)timeout_ms);
    }
    return drained;
}

void AudioService::AbortPcmPlayback() {
    // ① Bump the active PCM generation without taking any lock, so a writer blocked on
    //    audio_queue_cv_ (while holding pcm_mutex_) observes the mismatch once woken (§6.3).
    pcm_stream_generation_.fetch_add(1);
    // ② Wake blocked writers (shared cv; other waiters simply re-evaluate their predicate).
    audio_queue_cv_.notify_all();

    // ③ Only now take pcm_mutex_; never touches playback_generation_ nor the queues (§6.3).
    std::lock_guard<std::mutex> pcm_lock(pcm_mutex_);
    pcm_pending_bytes_.clear();
    pcm_stream_active_ = false;
}

bool AudioService::IsPcmPlaybackActive() const {
    // Only reflects the writable flag, not the validity of the two session tokens (§3.1).
    return pcm_stream_active_;
}

bool AudioService::PushPcmTaskToPlaybackQueue(std::vector<int16_t>&& pcm, uint32_t pcm_generation,
                                              uint32_t playback_generation) {
    // Caller (FlushPcmPendingBytes) must already hold pcm_mutex_.
    AudioTask task;
    task.type = kAudioTaskTypeDecodeToPlaybackQueue;
    task.pcm = std::move(pcm);
    task.playback_id = pcm_playback_id_;
    // Media position is the value before this frame; FlushPcmPendingBytes() advances it only
    // after a successful push (§6.2.3).
    task.media_position_ms = pcm_media_position_ms_;

    // Parameters differ from their members only by the trailing underscore; alias them so the
    // two independent comparisons below cannot be misread (§6.2.2 note 3).
    const uint32_t pcm_gen = pcm_generation;
    const uint32_t playback_gen = playback_generation;

    std::unique_lock<std::mutex> lock(audio_queue_mutex_);

    // The predicate only wakes us up: room in the queue, or either token invalid, or stopped.
    audio_queue_cv_.wait(lock, [&]() {
        return service_stopped_.load() || pcm_gen != pcm_stream_generation_.load() ||
               playback_gen != playback_generation_ ||
               audio_playback_queue_.size() < MAX_PLAYBACK_TASKS_IN_QUEUE;
    });

    // Final check in the same critical section as push_back. playback_generation_ is protected
    // by this same mutex (no window); pcm_stream_generation_ can still be bumped by
    // AbortPcmPlayback() without it (accepted soft-cancel window, §6.4.1/§6.4.2).
    if (service_stopped_.load()) {
        return false;
    }
    if (pcm_gen != pcm_stream_generation_.load()) {
        return false;
    }
    if (playback_gen != playback_generation_) {
        return false;
    }

    playback_drained_notified_ = false;  // so on_playback_drained() can fire again
    if (!audio_playback_queue_.push_back(std::move(task))) {
        ESP_LOGE(TAG, "Failed to push PCM task: playback queue is full");
        return false;
    }
    audio_queue_cv_.notify_all();
    return true;
}

bool AudioService::FlushPcmPendingBytes(bool is_final, uint32_t pcm_generation,
                                        uint32_t playback_generation) {
    // Caller must hold pcm_mutex_. Both tokens are passed through untouched: this function
    // never reads pcm_stream_generation_ or playback_generation_ (§7.3).
    for (;;) {
        if (pcm_pending_bytes_.size() < sizeof(int16_t)) {
            break;
        }
        size_t byte_count =
            pcm_pending_bytes_.size() - (pcm_pending_bytes_.size() % sizeof(int16_t));
        if (byte_count > PCM_PLAYBACK_CHUNK_BYTES) {
            byte_count = PCM_PLAYBACK_CHUNK_BYTES;
        }
        // Non-final flush pushes full frames only; the final flush also pushes the remainder.
        if (!is_final && byte_count < PCM_PLAYBACK_CHUNK_BYTES) {
            break;
        }

        const uint32_t frame_samples = (uint32_t)(byte_count / sizeof(int16_t));
        std::vector<int16_t> frame(frame_samples);
        std::memcpy(frame.data(), pcm_pending_bytes_.data(), byte_count);
        pcm_pending_bytes_.erase(pcm_pending_bytes_.begin(),
                                 pcm_pending_bytes_.begin() + byte_count);

        if (!PushPcmTaskToPlaybackQueue(std::move(frame), pcm_generation, playback_generation)) {
            return false;  // cancelled/stopped: do not advance the media position
        }
        // Advance only after a successful enqueue; integer milliseconds, rounded down.
        // 1440 samples -> 60 ms; the tail frame -> its real duration. 24000 is the output
        // sample rate validated by BeginPcmPlayback() (§5.1).
        pcm_media_position_ms_ += frame_samples * 1000u / 24000u;
    }
    if (is_final && !pcm_pending_bytes_.empty()) {
        // A single leftover byte can never form a sample: drop it (no sample is shifted).
        ESP_LOGW(TAG, "Discarding 1 trailing byte at end of PCM stream");
        pcm_pending_bytes_.clear();
    }
    return true;
}

bool AudioService::IsPlaybackDrainedLocked() const {
    return audio_decode_queue_.empty() && audio_playback_queue_.empty() && !decode_in_flight_ &&
           !output_in_flight_;
}

bool AudioService::MarkPlaybackDrainedLocked() {
    if (!IsPlaybackDrainedLocked() || playback_drained_notified_) {
        return false;
    }
    playback_drained_notified_ = true;
    return true;
}

void AudioService::CheckAndUpdateAudioPowerState() {
    auto now = std::chrono::steady_clock::now();
    auto input_elapsed =
        std::chrono::duration_cast<std::chrono::milliseconds>(now - last_input_time_).count();
    auto output_elapsed =
        std::chrono::duration_cast<std::chrono::milliseconds>(now - last_output_time_).count();
    if (input_elapsed > AUDIO_POWER_TIMEOUT_MS && codec_->input_enabled()) {
        // ADC continuous start/stop must run in the same task. Wake the audio
        // input task instead of closing the codec from the esp_timer task.
        xEventGroupSetBits(event_group_, AS_EVENT_AUDIO_INPUT_STOP_REQUEST);
    }
    if (output_elapsed > AUDIO_POWER_TIMEOUT_MS && codec_->output_enabled()) {
        // Keep TX clock when duplex RX is active; otherwise RX may stall on some boards.
        if (!(codec_->duplex() && codec_->input_enabled())) {
            codec_->EnableOutput(false);
        }
    }
    if (!codec_->input_enabled() && !codec_->output_enabled()) {
        esp_timer_stop(audio_power_timer_);
    }
}

void AudioService::SetModelsList(srmodel_list_t* models_list) {
    if (audio_engine_initialized_ && models_list_ != models_list) {
        ESP_LOGW(TAG, "Ignoring speech model replacement after audio engine initialization");
        return;
    }
    models_list_ = models_list;
}

bool AudioService::IsAfeWakeWord() {
    return audio_engine_initialized_ && audio_engine_->IsAfeWakeWord();
}

bool AudioService::InitializeAudioEngine() {
    if (!audio_engine_) {
        return false;
    }
    if (audio_engine_initialized_) {
        return true;
    }
    if (!audio_engine_->Initialize(codec_, OPUS_FRAME_DURATION_MS, models_list_)) {
        ESP_LOGE(TAG, "Failed to initialize audio engine");
        return false;
    }
    audio_engine_initialized_ = true;
    audio_engine_->EnableDeviceAec(device_aec_enabled_);
    return true;
}
