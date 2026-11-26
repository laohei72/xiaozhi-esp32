"""Host tests for AudioService::EndPcmPlayback() (docs/PCM_PLAYBACK_DESIGN.md §9.1).

The real main/audio/audio_service.cc is compiled unchanged. Only the platform boundary
(FreeRTOS tasks/event groups, esp_timer, Opus/rate-cvt calls, the audio codec and the audio
engine) is replaced by the stubs below. Audio is written by a fake codec that simulates the
duration of an I2S write, so these tests validate queue, drain and cancellation logic only;
they are NOT an I2S or speaker validation.
"""

import math
import os
import shlex
import shutil
import struct
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHUNK_SAMPLES = 1440
FRAME_BYTES = CHUNK_SAMPLES * 2
OUTPUT_SAMPLE_RATE = 24000


def _tone():
    """24 kHz mono s16le sine (local data source, no network/TTS dependency)."""
    samples = []
    for index in range(8000):
        samples.append(int(8000 * math.sin(2 * math.pi * 440 * index / OUTPUT_SAMPLE_RATE)))
    return struct.pack("<%dh" % len(samples), *samples)


STUBS = {
    "esp_log.h": r'''
        #pragma once
        #include <cstdio>
        #define ESP_LOGE(tag, fmt, ...) std::printf("E %s: " fmt "\n", tag, ##__VA_ARGS__)
        #define ESP_LOGW(tag, fmt, ...) std::printf("W %s: " fmt "\n", tag, ##__VA_ARGS__)
        #define ESP_LOGI(tag, fmt, ...) std::printf("I %s: " fmt "\n", tag, ##__VA_ARGS__)
        #define ESP_LOGD(tag, fmt, ...) std::printf("D %s: " fmt "\n", tag, ##__VA_ARGS__)
    ''',
    "esp_system.h": r'''
        #pragma once
        #include <cstdio>
        #include <cstdlib>
        [[noreturn]] inline void esp_system_abort(const char* message) {
            std::fprintf(stderr, "esp_system_abort: %s\n", message);
            std::abort();
        }
    ''',
    "model_path.h": r'''
        #pragma once
        typedef struct srmodel_list_s srmodel_list_t;
    ''',
    "freertos/FreeRTOS.h": r'''
        #pragma once
        #include <cstdint>
        typedef void* TaskHandle_t;
        typedef uint32_t TickType_t;
        typedef int BaseType_t;
        typedef unsigned int UBaseType_t;
        #define pdTRUE 1
        #define pdFALSE 0
        #define pdPASS 1
        #define pdFAIL 0
        #define portMAX_DELAY ((TickType_t)0xFFFFFFFFU)
        #define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
    ''',
    "freertos/event_groups.h": r'''
        #pragma once
        #include <chrono>
        #include <condition_variable>
        #include <cstdint>
        #include <mutex>
        #include <freertos/FreeRTOS.h>
        typedef uint32_t EventBits_t;
        typedef void* EventGroupHandle_t;
        namespace host_rtos {
        struct EventGroup {
            std::mutex mutex;
            std::condition_variable cv;
            EventBits_t bits = 0;
        };
        inline EventGroup* AsGroup(EventGroupHandle_t handle) {
            return static_cast<EventGroup*>(handle);
        }
        }  // namespace host_rtos
        inline EventGroupHandle_t xEventGroupCreate() { return new host_rtos::EventGroup(); }
        inline void vEventGroupDelete(EventGroupHandle_t handle) { delete host_rtos::AsGroup(handle); }
        inline EventBits_t xEventGroupSetBits(EventGroupHandle_t handle, EventBits_t bits) {
            auto* group = host_rtos::AsGroup(handle);
            std::lock_guard<std::mutex> lock(group->mutex);
            group->bits |= bits;
            group->cv.notify_all();
            return group->bits;
        }
        inline EventBits_t xEventGroupClearBits(EventGroupHandle_t handle, EventBits_t bits) {
            auto* group = host_rtos::AsGroup(handle);
            std::lock_guard<std::mutex> lock(group->mutex);
            const EventBits_t previous = group->bits;
            group->bits &= ~bits;
            return previous;
        }
        inline EventBits_t xEventGroupGetBits(EventGroupHandle_t handle) {
            auto* group = host_rtos::AsGroup(handle);
            std::lock_guard<std::mutex> lock(group->mutex);
            return group->bits;
        }
        inline EventBits_t xEventGroupWaitBits(EventGroupHandle_t handle, EventBits_t bits,
                                               BaseType_t clear_on_exit, BaseType_t wait_for_all,
                                               TickType_t ticks) {
            auto* group = host_rtos::AsGroup(handle);
            std::unique_lock<std::mutex> lock(group->mutex);
            auto ready = [&]() {
                const EventBits_t current = group->bits & bits;
                return wait_for_all ? current == bits : current != 0;
            };
            if (ticks == portMAX_DELAY) {
                group->cv.wait(lock, ready);
            } else {
                group->cv.wait_for(lock, std::chrono::milliseconds(ticks), ready);
            }
            EventBits_t result = group->bits & bits;
            if (clear_on_exit) {
                group->bits &= ~bits;
            }
            return result;
        }
    ''',
    "freertos/task.h": r'''
        #pragma once
        #include <chrono>
        #include <thread>
        #include <freertos/FreeRTOS.h>
        inline void vTaskDelay(TickType_t ticks) {
            std::this_thread::sleep_for(std::chrono::milliseconds(ticks));
        }
        inline void vTaskDelete(TaskHandle_t) {}
        inline BaseType_t xTaskCreate(void (*entry)(void*), const char*, uint32_t, void* arg,
                                      UBaseType_t, TaskHandle_t* handle) {
            if (handle != nullptr) {
                *handle = nullptr;
            }
            std::thread(entry, arg).detach();
            return pdTRUE;
        }
        inline BaseType_t xTaskCreatePinnedToCore(void (*entry)(void*), const char* name,
                                                  uint32_t stack, void* arg, UBaseType_t priority,
                                                  TaskHandle_t* handle, int) {
            return xTaskCreate(entry, name, stack, arg, priority, handle);
        }
    ''',
    "esp_timer.h": r'''
        #pragma once
        // Periodic callbacks are intentionally not fired: audio power management is out of
        // scope for the EndPcmPlayback() tests.
        #include <chrono>
        #include <cstdint>
        struct host_esp_timer_state {
            int unused = 0;
        };
        typedef struct host_esp_timer_state* esp_timer_handle_t;
        typedef enum { ESP_TIMER_TASK, ESP_TIMER_ISR } esp_timer_dispatch_t;
        typedef struct {
            void (*callback)(void* arg);
            void* arg;
            esp_timer_dispatch_t dispatch_method;
            const char* name;
            bool skip_unhandled_events;
        } esp_timer_create_args_t;
        inline int64_t esp_timer_get_time() {
            static const auto start = std::chrono::steady_clock::now();
            return std::chrono::duration_cast<std::chrono::microseconds>(
                       std::chrono::steady_clock::now() - start)
                .count();
        }
        inline int esp_timer_create(const esp_timer_create_args_t*, esp_timer_handle_t* handle) {
            static host_esp_timer_state token;
            *handle = &token;
            return 0;
        }
        inline int esp_timer_start_periodic(esp_timer_handle_t, uint64_t) { return 0; }
        inline int esp_timer_stop(esp_timer_handle_t) { return 0; }
    ''',
    "esp_audio_types.h": r'''
        #pragma once
        #include <cstdint>
        #define ESP_AUDIO_SAMPLE_RATE_16K 16000
        #define ESP_AUDIO_MONO 1
        #define ESP_AUDIO_BIT16 16
        #define ESP_AUDIO_ERR_OK 0
        #define ESP_AUDIO_DEC_RECOVERY_NONE 0
        typedef int esp_audio_err_t;
        typedef struct {
            uint8_t* buffer;
            uint32_t len;
            uint32_t consumed;
            int frame_recover;
        } esp_audio_dec_in_raw_t;
        typedef struct {
            uint8_t* buffer;
            uint32_t len;
            uint32_t decoded_size;
        } esp_audio_dec_out_frame_t;
        typedef struct {
            uint32_t sample_rate;
            uint8_t channel;
            uint8_t bits_per_sample;
        } esp_audio_dec_info_t;
        typedef struct {
            uint8_t* buffer;
            uint32_t len;
            int eos;
        } esp_audio_enc_in_frame_t;
        typedef struct {
            uint8_t* buffer;
            uint32_t len;
            uint32_t encoded_bytes;
        } esp_audio_enc_out_frame_t;
    ''',
    "esp_ae_rate_cvt.h": r'''
        #pragma once
        #include <cstdint>
        #include "esp_audio_types.h"
        typedef void* esp_ae_rate_cvt_handle_t;
        typedef void* esp_ae_sample_t;
        #define ESP_AE_RATE_CVT_PERF_TYPE_SPEED 0
        typedef struct {
            uint32_t src_rate;
            uint32_t dest_rate;
            uint8_t channel;
            uint8_t bits_per_sample;
            uint32_t complexity;
            int perf_type;
        } esp_ae_rate_cvt_cfg_t;
        inline esp_audio_err_t esp_ae_rate_cvt_open(esp_ae_rate_cvt_cfg_t*,
                                                    esp_ae_rate_cvt_handle_t* handle) {
            static int token = 0;
            *handle = &token;
            return ESP_AUDIO_ERR_OK;
        }
        inline esp_audio_err_t esp_ae_rate_cvt_process(esp_ae_rate_cvt_handle_t, esp_ae_sample_t,
                                                       uint32_t, esp_ae_sample_t,
                                                       uint32_t* out_sample_num) {
            if (out_sample_num != nullptr) {
                *out_sample_num = 0;
            }
            return ESP_AUDIO_ERR_OK;
        }
        inline esp_audio_err_t esp_ae_rate_cvt_reset(esp_ae_rate_cvt_handle_t) {
            return ESP_AUDIO_ERR_OK;
        }
        inline esp_audio_err_t esp_ae_rate_cvt_get_max_out_sample_num(esp_ae_rate_cvt_handle_t,
                                                                      uint32_t in_sample_num,
                                                                      uint32_t* out_sample_num) {
            if (out_sample_num != nullptr) {
                *out_sample_num = in_sample_num;
            }
            return ESP_AUDIO_ERR_OK;
        }
        inline void esp_ae_rate_cvt_close(esp_ae_rate_cvt_handle_t) {}
    ''',
    "esp_audio_enc.h": r'''
        #pragma once
        #include "esp_audio_types.h"
    ''',
    "esp_opus_dec.h": r'''
        #pragma once
        #include <cstdint>
        #include "esp_audio_types.h"
        typedef enum {
            ESP_OPUS_DEC_FRAME_DURATION_ARG = -1,
            ESP_OPUS_DEC_FRAME_DURATION_2_5_MS = 0,
            ESP_OPUS_DEC_FRAME_DURATION_5_MS = 1,
            ESP_OPUS_DEC_FRAME_DURATION_10_MS = 2,
            ESP_OPUS_DEC_FRAME_DURATION_20_MS = 3,
            ESP_OPUS_DEC_FRAME_DURATION_40_MS = 4,
            ESP_OPUS_DEC_FRAME_DURATION_60_MS = 5,
            ESP_OPUS_DEC_FRAME_DURATION_80_MS = 6,
            ESP_OPUS_DEC_FRAME_DURATION_100_MS = 7,
            ESP_OPUS_DEC_FRAME_DURATION_120_MS = 8,
        } esp_opus_dec_frame_duration_t;
        typedef struct {
            uint32_t sample_rate;
            uint8_t channel;
            esp_opus_dec_frame_duration_t frame_duration;
            bool self_delimited;
        } esp_opus_dec_cfg_t;
        inline esp_audio_err_t esp_opus_dec_open(esp_opus_dec_cfg_t*, uint32_t, void** handle) {
            static int token = 0;
            *handle = &token;
            return ESP_AUDIO_ERR_OK;
        }
        inline esp_audio_err_t esp_opus_dec_close(void*) { return ESP_AUDIO_ERR_OK; }
        inline esp_audio_err_t esp_opus_dec_reset(void*) { return ESP_AUDIO_ERR_OK; }
        // Opus decoding is not exercised by these tests: fail so callers take their error path.
        inline esp_audio_err_t esp_opus_dec_decode(void*, esp_audio_dec_in_raw_t*,
                                                   esp_audio_dec_out_frame_t*,
                                                   esp_audio_dec_info_t*) {
            return -1;
        }
    ''',
    "esp_opus_enc.h": r'''
        #pragma once
        #include <cstdint>
        #include "esp_audio_enc.h"
        #define ESP_OPUS_BITRATE_AUTO (-1000)
        #define ESP_OPUS_ENC_APPLICATION_AUDIO 1
        typedef enum {
            ESP_OPUS_ENC_FRAME_DURATION_2_5_MS = 0,
            ESP_OPUS_ENC_FRAME_DURATION_5_MS = 1,
            ESP_OPUS_ENC_FRAME_DURATION_10_MS = 2,
            ESP_OPUS_ENC_FRAME_DURATION_20_MS = 3,
            ESP_OPUS_ENC_FRAME_DURATION_40_MS = 4,
            ESP_OPUS_ENC_FRAME_DURATION_60_MS = 5,
            ESP_OPUS_ENC_FRAME_DURATION_80_MS = 6,
            ESP_OPUS_ENC_FRAME_DURATION_100_MS = 7,
            ESP_OPUS_ENC_FRAME_DURATION_120_MS = 8,
        } esp_opus_enc_frame_duration_t;
        typedef struct {
            int sample_rate;
            int channel;
            int bits_per_sample;
            int bitrate;
            esp_opus_enc_frame_duration_t frame_duration;
            int application_mode;
            int complexity;
            bool enable_fec;
            bool enable_dtx;
            bool enable_vbr;
        } esp_opus_enc_config_t;
        inline esp_audio_err_t esp_opus_enc_open(void*, uint32_t, void** enc_hd) {
            static int token = 0;
            *enc_hd = &token;
            return ESP_AUDIO_ERR_OK;
        }
        inline esp_audio_err_t esp_opus_enc_get_frame_size(void*, int* in_size, int* out_size) {
            if (in_size != nullptr) {
                *in_size = 1920;
            }
            if (out_size != nullptr) {
                *out_size = 512;
            }
            return ESP_AUDIO_ERR_OK;
        }
        inline esp_audio_err_t esp_opus_enc_process(void*, esp_audio_enc_in_frame_t*,
                                                    esp_audio_enc_out_frame_t*) {
            return -1;
        }
        inline void esp_opus_enc_close(void*) {}
    ''',
    "board.h": r'''
        #pragma once
        class AudioCodec;
        // Minimal Board stand-in. Only SetDecodeSampleRate() asks the board for the codec, and
        // the fake codec registered by the rig becomes the answer.
        class Board {
        public:
            static Board& GetInstance() {
                static Board instance;
                return instance;
            }
            AudioCodec* GetAudioCodec() {
                return codec_;
            }
            static void SetAudioCodec(AudioCodec* codec) {
                GetInstance().codec_ = codec;
            }

        private:
            AudioCodec* codec_ = nullptr;
        };
    ''',
    "audio_codec.h": r'''
        #pragma once
        #include <cstdint>
        #include <vector>
        #include "board.h"
        // The hardware boundary is replaced: tests use a fake codec that simulates I2S timing.
        class AudioCodec {
        public:
            virtual ~AudioCodec() = default;
            virtual void Start() {}
            virtual bool input_enabled() const { return input_enabled_; }
            virtual bool output_enabled() const { return output_enabled_; }
            virtual void EnableInput(bool enable) { input_enabled_ = enable; }
            virtual void EnableOutput(bool enable) { output_enabled_ = enable; }
            virtual int input_sample_rate() const { return 16000; }
            virtual int input_channels() const { return 1; }
            virtual int output_sample_rate() const { return 24000; }
            virtual bool duplex() const { return false; }
            virtual bool InputData(std::vector<int16_t>& data) {
                data.assign(data.size(), 0);
                return true;
            }
            virtual void OutputData(std::vector<int16_t>& data) { (void)data; }

        protected:
            bool input_enabled_ = true;
            bool output_enabled_ = true;
        };
    ''',
    "audio_debugger.h": r'''
        #pragma once
        #include <cstdint>
        #include <vector>
        class AudioDebugger {
        public:
            void Feed(std::vector<int16_t>& data) { (void)data; }
        };
    ''',
    "ogg_demuxer.h": r'''
        #pragma once
        #include <cstddef>
        #include <cstdint>
        #include <functional>
        // Ogg playback (PlaySound) is not exercised by these tests.
        class OggDemuxer {
        public:
            void Reset() {}
            size_t Process(const uint8_t*, size_t) { return 0; }
            bool Finish() const { return true; }
            bool HasError() const { return false; }
            void OnPacket(std::function<void(const uint8_t*, int, int, size_t)>) {}
        };
    ''',
    "protocol.h": r'''
        #pragma once
        #include <cstdint>
        #include <vector>
        struct AudioStreamPacket {
            int sample_rate = 0;
            int frame_duration = 0;
            uint32_t timestamp = 0;
            uint32_t playback_id = 0;
            uint32_t media_position_ms = 0;
            std::vector<uint8_t> payload;
        };
    ''',
    "engines/lite_audio_engine.h": r'''
        #pragma once
        #include <string>
        #include <vector>
        #include "audio_engine.h"
        // Wake word / VAD / AFE are out of scope: this engine never detects anything.
        class LiteAudioEngine : public AudioEngine {
        public:
            bool Initialize(AudioCodec* codec, int, srmodel_list_t*) override {
                codec_ = codec;
                return true;
            }
            void Feed(std::vector<int16_t>&& data) override { (void)data; }
            void EnableWakeWordDetection(bool enable) override { wake_word_enabled_ = enable; }
            void EnableVoiceProcessing(bool enable) override { voice_processing_enabled_ = enable; }
            void EnableDeviceAec(bool) override {}
            bool HasWakeWord() const override { return false; }
            bool IsWakeWordDetectionEnabled() const override { return wake_word_enabled_; }
            bool IsVoiceProcessingEnabled() const override { return voice_processing_enabled_; }
            bool IsAfeWakeWord() const override { return false; }
            std::vector<std::string> GetWakeWords() const override { return {}; }
            size_t GetFeedSize() const override { return 0; }
            void OnWakeWordDetected(std::function<void(const std::string&)> callback) override {
                on_wake_word_ = std::move(callback);
            }
            void OnOutput(std::function<void(std::vector<int16_t>&& data)> callback) override {
                on_output_ = std::move(callback);
            }
            void OnVadStateChange(std::function<void(bool speaking)> callback) override {
                on_vad_ = std::move(callback);
            }
            void EncodeWakeWordData() override {}
            bool GetWakeWordOpus(std::vector<uint8_t>&) override { return false; }
            const std::string& GetLastDetectedWakeWord() const override { return empty_; }
            bool RestoreWakeWordResources() { return false; }
            void ReleaseWakeWordResources() {}

        private:
            AudioCodec* codec_ = nullptr;
            bool wake_word_enabled_ = false;
            bool voice_processing_enabled_ = false;
            std::string empty_;
            std::function<void(const std::string&)> on_wake_word_;
            std::function<void(std::vector<int16_t>&&)> on_output_;
            std::function<void(bool)> on_vad_;
        };
    ''',
}

DRIVER = r'''
    #include <algorithm>
    #include <atomic>
    #include <chrono>
    #include <cstdint>
    #include <cstdio>
    #include <cstring>
    #include <fstream>
    #include <iterator>
    #include <mutex>
    #include <string>
    #include <thread>
    #include <utility>
    #include <vector>

    #include "audio_service.h"

    namespace {

    using Clock = std::chrono::steady_clock;
    using Ms = std::chrono::milliseconds;

    constexpr size_t kFrameSamples = 1440;  // 60 ms @ 24 kHz mono
    constexpr size_t kFrameBytes = kFrameSamples * sizeof(int16_t);

    int g_failures = 0;

    int64_t ElapsedMs(Clock::time_point start) {
        return std::chrono::duration_cast<Ms>(Clock::now() - start).count();
    }

    void Check(bool condition, const char* name, const std::string& detail) {
        std::printf("CHECK %s %s %s\n", condition ? "PASS" : "FAIL", name, detail.c_str());
        if (!condition) {
            ++g_failures;
        }
    }

    void Finish(const char* scenario, int failures_before) {
        std::printf("RESULT %s %s\n", scenario, g_failures == failures_before ? "PASS" : "FAIL");
    }

    struct PlayedFrame {
        size_t samples = 0;
        Clock::time_point begin;
        Clock::time_point end;
    };

    // Stands in for I2S: records what was "played" and how long the write took.
    class FakeCodec : public AudioCodec {
    public:
        explicit FakeCodec(Ms write_time) : write_time_(write_time) {}

        void Start() override {}
        int input_sample_rate() const override { return 16000; }
        int input_channels() const override { return 1; }
        int output_sample_rate() const override { return 24000; }
        bool duplex() const override { return false; }

        void OutputData(std::vector<int16_t>& data) override {
            const auto begin = Clock::now();
            if (write_time_.count() > 0) {
                std::this_thread::sleep_for(write_time_);
            }
            const auto end = Clock::now();
            std::lock_guard<std::mutex> lock(mutex_);
            frames_.push_back({data.size(), begin, end});
            samples_.insert(samples_.end(), data.begin(), data.end());
        }

        size_t frame_count() {
            std::lock_guard<std::mutex> lock(mutex_);
            return frames_.size();
        }
        size_t sample_count() {
            std::lock_guard<std::mutex> lock(mutex_);
            return samples_.size();
        }
        std::vector<int16_t> samples() {
            std::lock_guard<std::mutex> lock(mutex_);
            return samples_;
        }
        std::vector<size_t> frame_sizes() {
            std::lock_guard<std::mutex> lock(mutex_);
            std::vector<size_t> sizes;
            sizes.reserve(frames_.size());
            for (const auto& frame : frames_) {
                sizes.push_back(frame.samples);
            }
            return sizes;
        }
        Clock::time_point last_frame_end() {
            std::lock_guard<std::mutex> lock(mutex_);
            return frames_.empty() ? Clock::time_point{} : frames_.back().end;
        }

    private:
        Ms write_time_;
        std::mutex mutex_;
        std::vector<PlayedFrame> frames_;
        std::vector<int16_t> samples_;
    };

    struct Rig {
        FakeCodec codec;
        AudioService service;
        std::vector<std::pair<uint32_t, uint32_t>> progress;

        explicit Rig(Ms write_time) : codec(write_time) {
            Board::SetAudioCodec(&codec);
            AudioServiceCallbacks callbacks;
            callbacks.on_playback_progress = [this](uint32_t playback_id,
                                                    uint32_t media_position_ms) {
                progress.emplace_back(playback_id, media_position_ms);
            };
            service.SetCallbacks(callbacks);
            service.Initialize(&codec);
            service.Start();
        }

        ~Rig() {
            service.Stop();
            // Tasks are detached host threads: give them time to observe service_stopped_
            // and return before AudioService is destroyed.
            std::this_thread::sleep_for(Ms(600));
        }
    };

    std::vector<uint8_t> ReadFile(const char* path) {
        std::ifstream stream(path, std::ios::binary);
        return std::vector<uint8_t>(std::istreambuf_iterator<char>(stream),
                                    std::istreambuf_iterator<char>());
    }

    std::vector<int16_t> AsSamples(const std::vector<uint8_t>& bytes) {
        std::vector<int16_t> samples(bytes.size() / sizeof(int16_t));
        if (!samples.empty()) {
            std::memcpy(samples.data(), bytes.data(), samples.size() * sizeof(int16_t));
        }
        return samples;
    }

    bool WriteChunks(AudioService& service, const std::vector<uint8_t>& bytes, size_t chunk_size,
                     bool last_on_final) {
        size_t offset = 0;
        while (offset < bytes.size()) {
            const size_t take = std::min(chunk_size, bytes.size() - offset);
            const bool last = last_on_final && (offset + take == bytes.size());
            if (!service.WritePcmData(bytes.data() + offset, take, last)) {
                return false;
            }
            offset += take;
        }
        return true;
    }

    std::vector<uint32_t> ExpectedPositions(const std::vector<size_t>& frame_sizes) {
        std::vector<uint32_t> positions;
        uint32_t position = 0;
        for (size_t samples : frame_sizes) {
            positions.push_back(position);
            position += static_cast<uint32_t>(samples) * 1000u / 24000u;
        }
        return positions;
    }

    std::string Join(const std::vector<size_t>& values) {
        std::string text;
        for (size_t value : values) {
            if (!text.empty()) {
                text += ",";
            }
            text += std::to_string(value);
        }
        return text;
    }

    // Happy path (§9.1): flush the tail, then wait for the last write to finish.
    void RunDrain(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO drain\n");
        Rig rig(Ms(10));

        Check(rig.service.BeginPcmPlayback(24000, 1, 42), "drain/began", "");
        // HTTP-like odd chunk sizes must not lose or shift a single sample.
        Check(WriteChunks(rig.service, pcm, 1001, true), "drain/wrote", "");

        const auto start = Clock::now();
        const bool drained = rig.service.EndPcmPlayback(3000);
        const auto end_time = Clock::now();
        const int64_t end_ms = ElapsedMs(start);

        const std::vector<size_t> frame_sizes = rig.codec.frame_sizes();
        const std::vector<int16_t> played = rig.codec.samples();
        const std::vector<int16_t> source = AsSamples(pcm);
        const size_t expected_frames =
            source.size() / kFrameSamples + (source.size() % kFrameSamples != 0 ? 1 : 0);

        std::printf("OBS drain.end_result=%d\n", drained ? 1 : 0);
        std::printf("OBS drain.end_ms=%lld\n", static_cast<long long>(end_ms));
        std::printf("OBS drain.frames=%s\n", Join(frame_sizes).c_str());
        std::printf("OBS drain.played_samples=%zu\n", played.size());

        Check(drained, "drain/returns_true", "end_ms=" + std::to_string(end_ms));
        Check(!rig.service.IsPcmPlaybackActive(), "drain/session_closed", "");
        Check(played.size() == source.size(), "drain/all_samples_played",
              "played=" + std::to_string(played.size()) +
                  " source=" + std::to_string(source.size()));
        Check(played == source, "drain/sample_data_intact", "");
        Check(frame_sizes.size() == expected_frames, "drain/frame_count",
              "frames=" + Join(frame_sizes) + " expected=" + std::to_string(expected_frames));
        Check(end_time >= rig.codec.last_frame_end(), "drain/end_after_last_write", "");

        std::vector<uint32_t> positions;
        bool ids_ok = true;
        for (const auto& entry : rig.progress) {
            positions.push_back(entry.second);
            ids_ok = ids_ok && entry.first == 42;
        }
        Check(positions == ExpectedPositions(frame_sizes), "drain/media_positions",
              "count=" + std::to_string(positions.size()));
        Check(ids_ok, "drain/progress_playback_id", "");
        Finish("drain", before);
    }

    // §9.1 cancellation: AbortPcmPlayback() wakes the drain wait immediately.
    void RunAbortDuringEnd(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO abort_during_end\n");
        Rig rig(Ms(300));

        Check(rig.service.BeginPcmPlayback(24000, 1, 0), "abort/began", "");
        const std::vector<uint8_t> two_frames(pcm.begin(), pcm.begin() + 2 * kFrameBytes);
        Check(WriteChunks(rig.service, two_frames, kFrameBytes, false), "abort/wrote", "");

        const auto begin = Clock::now();
        bool end_result = true;
        int64_t end_ms = 0;
        std::thread ender([&]() {
            const auto start = Clock::now();
            end_result = rig.service.EndPcmPlayback(5000);
            end_ms = ElapsedMs(start);
        });

        std::this_thread::sleep_for(Ms(80));
        const int64_t abort_at_ms = ElapsedMs(begin);
        rig.service.AbortPcmPlayback();
        const int64_t abort_done_ms = ElapsedMs(begin);
        ender.join();

        std::printf("OBS abort.end_result=%d\n", end_result ? 1 : 0);
        std::printf("OBS abort.end_ms=%lld\n", static_cast<long long>(end_ms));
        std::printf("OBS abort.abort_ms=%lld\n", static_cast<long long>(abort_done_ms));

        Check(!end_result, "abort/returns_false", "end_ms=" + std::to_string(end_ms));
        Check(end_ms < 5000, "abort/no_full_timeout", "end_ms=" + std::to_string(end_ms));
        Check(end_ms - abort_at_ms <= 200, "abort/immediate_return",
              "delay=" + std::to_string(end_ms - abort_at_ms));
        Check(abort_done_ms - abort_at_ms <= 100, "abort/abort_does_not_block", "");
        Check(!rig.service.IsPcmPlaybackActive(), "abort/session_closed", "");
        Check(!rig.service.WritePcmData(nullptr, 0, false), "abort/write_rejected", "");

        // Soft cancel: frames already handed to the queue still play.
        std::this_thread::sleep_for(Ms(900));
        Check(rig.codec.frame_count() == 2, "abort/queued_frames_play",
              "frames=" + std::to_string(rig.codec.frame_count()));
        Finish("abort_during_end", before);
    }

    // §9.1 timeout: return false after the requested timeout without clearing the queue.
    void RunTimeout(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO timeout\n");
        Rig rig(Ms(300));

        Check(rig.service.BeginPcmPlayback(24000, 1, 0), "timeout/began", "");
        const std::vector<uint8_t> two_frames(pcm.begin(), pcm.begin() + 2 * kFrameBytes);
        Check(WriteChunks(rig.service, two_frames, kFrameBytes, false), "timeout/wrote", "");

        const auto start = Clock::now();
        const bool drained = rig.service.EndPcmPlayback(100);
        const int64_t end_ms = ElapsedMs(start);
        const size_t frames_at_end = rig.codec.frame_count();

        std::printf("OBS timeout.end_result=%d\n", drained ? 1 : 0);
        std::printf("OBS timeout.end_ms=%lld\n", static_cast<long long>(end_ms));
        std::printf("OBS timeout.frames_at_end=%zu\n", frames_at_end);

        Check(!drained, "timeout/returns_false", "end_ms=" + std::to_string(end_ms));
        Check(end_ms >= 100, "timeout/waited_for_timeout", "end_ms=" + std::to_string(end_ms));
        Check(end_ms < 1500, "timeout/not_stuck", "end_ms=" + std::to_string(end_ms));
        Check(frames_at_end < 2, "timeout/undrained_at_return",
              "frames=" + std::to_string(frames_at_end));
        Check(!rig.service.IsPcmPlaybackActive(), "timeout/session_closed", "");
        Check(!rig.service.WritePcmData(nullptr, 0, false), "timeout/write_rejected", "");

        // The queue must survive the timeout: the pending frame still reaches the speaker.
        std::this_thread::sleep_for(Ms(900));
        Check(rig.codec.frame_count() == 2, "timeout/queue_preserved",
              "frames=" + std::to_string(rig.codec.frame_count()));
        Finish("timeout", before);
    }

    // output_in_flight_ alone must keep IsPlaybackDrainedLocked() false (empty queue).
    void RunInFlightGate(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO in_flight_gate\n");
        Rig rig(Ms(300));

        Check(rig.service.BeginPcmPlayback(24000, 1, 0), "inflight/began", "");
        const std::vector<uint8_t> one_frame(pcm.begin(), pcm.begin() + kFrameBytes);
        Check(WriteChunks(rig.service, one_frame, kFrameBytes, false), "inflight/wrote", "");

        const auto start = Clock::now();
        const bool drained = rig.service.EndPcmPlayback(80);
        const int64_t end_ms = ElapsedMs(start);
        const size_t frames_at_end = rig.codec.frame_count();

        std::printf("OBS inflight.end_result=%d\n", drained ? 1 : 0);
        std::printf("OBS inflight.end_ms=%lld\n", static_cast<long long>(end_ms));
        std::printf("OBS inflight.frames_at_end=%zu\n", frames_at_end);

        Check(!drained, "inflight/not_drained", "end_ms=" + std::to_string(end_ms));
        Check(frames_at_end == 0, "inflight/output_in_flight_blocks_drain",
              "frames=" + std::to_string(frames_at_end));

        std::this_thread::sleep_for(Ms(600));
        Check(rig.codec.frame_count() == 1, "inflight/frame_completes",
              "frames=" + std::to_string(rig.codec.frame_count()));
        Finish("in_flight_gate", before);
    }

    // Tail shorter than one chunk frame must still be queued and drained (§7).
    void RunShortTail(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO short_tail\n");
        Rig rig(Ms(5));

        Check(rig.service.BeginPcmPlayback(24000, 1, 0), "tail/began", "");
        const std::vector<uint8_t> tail(pcm.begin(), pcm.begin() + 1000 * sizeof(int16_t));
        Check(WriteChunks(rig.service, tail, tail.size(), false), "tail/wrote", "");

        const bool drained = rig.service.EndPcmPlayback(1000);
        const std::vector<size_t> frame_sizes = rig.codec.frame_sizes();

        std::printf("OBS short_tail.end_result=%d\n", drained ? 1 : 0);
        std::printf("OBS short_tail.frames=%s\n", Join(frame_sizes).c_str());

        Check(drained, "tail/returns_true", "");
        Check(frame_sizes == std::vector<size_t>{1000}, "tail/single_partial_frame",
              "frames=" + Join(frame_sizes));
        Check(rig.codec.samples() == AsSamples(tail), "tail/sample_data_intact", "");
        Finish("short_tail", before);
    }

    // A single leftover byte cannot form a sample: drop it with a warning.
    void RunTrailingByte(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO trailing_byte\n");
        Rig rig(Ms(5));

        Check(rig.service.BeginPcmPlayback(24000, 1, 0), "tailbyte/began", "");
        std::vector<uint8_t> bytes(pcm.begin(), pcm.begin() + 1000 * sizeof(int16_t));
        bytes.push_back(0x7f);
        Check(rig.service.WritePcmData(bytes.data(), bytes.size(), true), "tailbyte/wrote", "");

        const bool drained = rig.service.EndPcmPlayback(1000);
        const std::vector<size_t> frame_sizes = rig.codec.frame_sizes();

        std::printf("OBS trailing_byte.end_result=%d\n", drained ? 1 : 0);
        std::printf("OBS trailing_byte.frames=%s\n", Join(frame_sizes).c_str());
        std::printf("OBS trailing_byte.samples=%zu\n", rig.codec.sample_count());

        Check(drained, "tailbyte/returns_true", "");
        Check(frame_sizes == std::vector<size_t>{1000}, "tailbyte/whole_samples_only",
              "frames=" + Join(frame_sizes));
        Check(rig.codec.sample_count() == 1000, "tailbyte/lone_byte_dropped",
              "samples=" + std::to_string(rig.codec.sample_count()));
        Finish("trailing_byte", before);
    }

    // No session: nothing to flush or wait for; the queue is idle.
    void RunNoSession() {
        const int before = g_failures;
        std::printf("SCENARIO no_session\n");
        Rig rig(Ms(0));

        Check(!rig.service.IsPcmPlaybackActive(), "nosession/inactive", "");
        const bool first = rig.service.EndPcmPlayback(100);
        const bool second = rig.service.EndPcmPlayback(100);

        std::printf("OBS no_session.first=%d\n", first ? 1 : 0);
        std::printf("OBS no_session.second=%d\n", second ? 1 : 0);

        Check(first, "nosession/returns_idle_true", "");
        Check(second, "nosession/idempotent", "");
        Check(!rig.service.IsPcmPlaybackActive(), "nosession/still_inactive", "");
        Finish("no_session", before);
    }

    // Service stop during the drain wait must wake EndPcmPlayback() too.
    void RunStopDuringEnd(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO stop_during_end\n");
        Rig rig(Ms(300));

        Check(rig.service.BeginPcmPlayback(24000, 1, 0), "stop/began", "");
        const std::vector<uint8_t> two_frames(pcm.begin(), pcm.begin() + 2 * kFrameBytes);
        Check(WriteChunks(rig.service, two_frames, kFrameBytes, false), "stop/wrote", "");

        const auto begin = Clock::now();
        bool end_result = true;
        int64_t end_ms = 0;
        std::thread ender([&]() {
            const auto start = Clock::now();
            end_result = rig.service.EndPcmPlayback(5000);
            end_ms = ElapsedMs(start);
        });

        std::this_thread::sleep_for(Ms(80));
        rig.service.Stop();
        const int64_t stop_ms = ElapsedMs(begin);
        ender.join();

        std::printf("OBS stop.end_result=%d\n", end_result ? 1 : 0);
        std::printf("OBS stop.end_ms=%lld\n", static_cast<long long>(end_ms));

        Check(!end_result, "stop/returns_false", "end_ms=" + std::to_string(end_ms));
        Check(end_ms < 5000, "stop/no_full_timeout", "end_ms=" + std::to_string(end_ms));
        Check(end_ms - stop_ms <= 300, "stop/woken_by_stop",
              "delay=" + std::to_string(end_ms - stop_ms));
        Finish("stop_during_end", before);
    }

    }  // namespace

    int main(int argc, char** argv) {
        if (argc < 3) {
            std::printf("usage: %s <tone.pcm> <scenario>\n", argv[0]);
            return 2;
        }
        const std::vector<uint8_t> pcm = ReadFile(argv[1]);
        if (pcm.size() < 3 * kFrameBytes) {
            std::printf("pcm file too small: %zu bytes\n", pcm.size());
            return 2;
        }

        const std::string scenario = argv[2];
        if (scenario == "drain") {
            RunDrain(pcm);
        } else if (scenario == "abort_during_end") {
            RunAbortDuringEnd(pcm);
        } else if (scenario == "timeout") {
            RunTimeout(pcm);
        } else if (scenario == "in_flight_gate") {
            RunInFlightGate(pcm);
        } else if (scenario == "short_tail") {
            RunShortTail(pcm);
        } else if (scenario == "trailing_byte") {
            RunTrailingByte(pcm);
        } else if (scenario == "no_session") {
            RunNoSession();
        } else if (scenario == "stop_during_end") {
            RunStopDuringEnd(pcm);
        } else {
            std::printf("unknown scenario: %s\n", scenario.c_str());
            return 2;
        }
        return g_failures == 0 ? 0 : 1;
    }
'''


class PcmPlaybackEndTest(unittest.TestCase):
    """Runs scenarios against the real audio_service.cc with stubbed platform APIs."""

    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(os.environ.get("CXX", "c++"))
        if not compiler or shutil.which(compiler[0]) is None:
            raise unittest.SkipTest(
                f"host C++ compiler not found: {(compiler or ['c++'])[0]} "
                "(set CXX=/path/to/g++ to run these tests)"
            )
        cls._tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tempdir.cleanup)
        build = Path(cls._tempdir.name)

        for name, content in STUBS.items():
            path = build / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")

        driver = build / "pcm_playback_end_driver.cc"
        driver.write_text(textwrap.dedent(DRIVER).lstrip("\n"), encoding="utf-8")
        cls.pcm_path = build / "tone_24k_mono_s16le.pcm"
        cls.pcm_path.write_bytes(_tone())

        # Verbatim copies of the files under test plus the AudioEngine interface: quote-includes
        # resolve the sibling stub audio_codec.h / engines/ headers from the build directory.
        sources = []
        for name in ("audio_service.cc", "audio_service.h", "audio_engine.h"):
            target = build / name
            shutil.copyfile(ROOT / "main" / "audio" / name, target)
            sources.append(str(target))

        cls.exe_path = build / ("pcm_playback_end" + (".exe" if os.name == "nt" else ""))
        command = compiler + [
            "-std=gnu++23",
            "-O1",
            "-g",
            "-pthread",
            f"-I{build}",
            f"-I{ROOT / 'main' / 'audio'}",
            f"-I{ROOT / 'main'}",
            str(driver),
        ] + sources + [
            "-o",
            str(cls.exe_path),
        ]
        compiled = subprocess.run(command, capture_output=True, text=True)
        if compiled.returncode != 0:
            raise AssertionError("failed to build the PCM playback host test:\n" + compiled.stderr)

    def _run(self, scenario):
        completed = subprocess.run(
            [str(self.exe_path), str(self.pcm_path), scenario],
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = completed.stdout + completed.stderr
        if os.environ.get("PCM_PLAYBACK_END_VERBOSE"):
            print(output)
        self.assertEqual(0, completed.returncode, msg=output)
        self.assertIn(f"RESULT {scenario} PASS", output, msg=output)
        self.assertNotIn("CHECK FAIL", output, msg=output)
        return output

    @staticmethod
    def _obs(output, key):
        prefix = f"OBS {key}="
        for line in output.splitlines():
            if line.startswith(prefix):
                return line[len(prefix) :].strip()
        raise AssertionError(f"missing {prefix} in output:\n{output}")

    def test_normal_drain_plays_every_sample(self):
        output = self._run("drain")
        self.assertEqual("1", self._obs(output, "drain.end_result"))
        # 8000 source samples -> 5 full frames + one 800-sample tail frame.
        self.assertEqual(str(8000), self._obs(output, "drain.played_samples"))
        self.assertEqual("1440,1440,1440,1440,1440,800", self._obs(output, "drain.frames"))

    def test_abort_wakes_the_drain_wait(self):
        output = self._run("abort_during_end")
        self.assertEqual("0", self._obs(output, "abort.end_result"))
        self.assertLess(int(self._obs(output, "abort.end_ms")), 5000)

    def test_timeout_returns_false_and_keeps_the_queue(self):
        output = self._run("timeout")
        self.assertEqual("0", self._obs(output, "timeout.end_result"))
        self.assertGreaterEqual(int(self._obs(output, "timeout.end_ms")), 100)
        self.assertLess(int(self._obs(output, "timeout.frames_at_end")), 2)
        self.assertIn("PCM playback drain timed out", output)

    def test_output_in_flight_keeps_drain_false(self):
        output = self._run("in_flight_gate")
        self.assertEqual("0", self._obs(output, "inflight.end_result"))
        # Queue was already empty, yet nothing had reached the codec: output_in_flight_.
        self.assertEqual("0", self._obs(output, "inflight.frames_at_end"))

    def test_short_tail_frame_is_flushed_and_drained(self):
        output = self._run("short_tail")
        self.assertEqual("1", self._obs(output, "short_tail.end_result"))
        self.assertEqual("1000", self._obs(output, "short_tail.frames"))

    def test_lone_trailing_byte_is_dropped(self):
        output = self._run("trailing_byte")
        self.assertEqual("1", self._obs(output, "trailing_byte.end_result"))
        self.assertEqual("1000", self._obs(output, "trailing_byte.samples"))
        self.assertIn("Discarding 1 trailing byte", output)

    def test_no_session_returns_idle(self):
        output = self._run("no_session")
        self.assertEqual("1", self._obs(output, "no_session.first"))
        self.assertEqual("1", self._obs(output, "no_session.second"))

    def test_stop_wakes_the_drain_wait(self):
        output = self._run("stop_during_end")
        self.assertEqual("0", self._obs(output, "stop.end_result"))
        self.assertLess(int(self._obs(output, "stop.end_ms")), 5000)


if __name__ == "__main__":
    unittest.main()

