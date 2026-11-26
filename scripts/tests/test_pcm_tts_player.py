"""Host tests for PcmTtsPlayer (docs/PCM_PLAYBACK_DESIGN.md §6.3, §9.1, §9.5).

Both main/reminder/pcm_tts_player.cc and main/audio/audio_service.cc are compiled unchanged
against the platform stubs of test_pcm_playback_end.py (FreeRTOS tasks/event groups, esp_timer,
the audio codec and the audio engine). The reader sources below stand in for the Baidu HTTP body
of the next stage, so these tests validate the player state machine, the deferred abort and the
finished-callback contract only -- they are NOT a network, I2S or speaker validation.
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

from test_pcm_playback_end import ROOT, STUBS

OUTPUT_SAMPLE_RATE = 24000
FRAME_SAMPLES = 1440
FRAME_BYTES = FRAME_SAMPLES * 2


def _tone(samples=8000, amplitude=8000):
    """24 kHz mono s16le sine: the local stand-in for a Baidu TTS audio body."""
    values = [
        int(amplitude * math.sin(2 * math.pi * 440 * index / OUTPUT_SAMPLE_RATE))
        for index in range(samples)
    ]
    return struct.pack("<%dh" % len(values), *values)


DRIVER_HEAD = r'''
    #include <algorithm>
    #include <atomic>
    #include <chrono>
    #include <condition_variable>
    #include <cstdint>
    #include <cstdio>
    #include <cstring>
    #include <fstream>
    #include <iterator>
    #include <memory>
    #include <mutex>
    #include <string>
    #include <thread>
    #include <vector>

    #include "audio_service.h"
    #include "pcm_tts_player.h"

    namespace {

    using Clock = std::chrono::steady_clock;
    using Ms = std::chrono::milliseconds;

    constexpr size_t kFrameSamples = 1440;  // 60 ms @ 24 kHz mono
    constexpr size_t kFrameBytes = kFrameSamples * sizeof(int16_t);
    constexpr size_t kChunkBytes = 2880;  // exactly one full frame per reader call
    constexpr int kSampleRate = 24000;

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

    std::vector<int16_t> AsSamples(const std::vector<uint8_t>& bytes) {
        std::vector<int16_t> samples(bytes.size() / sizeof(int16_t));
        if (!samples.empty()) {
            std::memcpy(samples.data(), bytes.data(), samples.size() * sizeof(int16_t));
        }
        return samples;
    }

    // Stands in for I2S: records what was "played" and how long each write took. A long write
    // time keeps the shared playback queue full, which is what the drain and cancel paths react
    // to.
    class FakeCodec : public AudioCodec {
    public:
        explicit FakeCodec(Ms write_time) : write_time_(write_time) {}

        // Models the amplifier/I2S power-on that BeginPcmPlayback() performs before it takes its
        // locks: the next EnableOutput(true) blocks, which keeps the player in kBeginning long
        // enough for a cancel to land inside BeginPcmPlayback().
        void DelayNextOutputEnable(Ms delay) {
            std::lock_guard<std::mutex> lock(mutex_);
            output_enabled_ = false;
            enable_delay_ = delay;
            enable_entered_ = false;
        }

        // Blocks until EnableOutput(true) started its delay, i.e. until the worker is demonstrably
        // inside BeginPcmPlayback() instead of merely being scheduled.
        bool WaitForEnableEntered(Ms timeout) {
            std::unique_lock<std::mutex> lock(mutex_);
            return enable_cv_.wait_for(lock, timeout, [this]() { return enable_entered_; });
        }

        bool output_enabled() const override {
            std::lock_guard<std::mutex> lock(mutex_);
            return output_enabled_;
        }

        void EnableOutput(bool enable) override {
            Ms delay(0);
            {
                std::lock_guard<std::mutex> lock(mutex_);
                if (enable && enable_delay_.count() > 0) {
                    delay = enable_delay_;
                    enable_delay_ = Ms(0);
                    enable_entered_ = true;
                    enable_cv_.notify_all();
                }
            }
            if (delay.count() > 0) {
                std::this_thread::sleep_for(delay);
            }
            std::lock_guard<std::mutex> lock(mutex_);
            output_enabled_ = enable;
        }

        void OutputData(std::vector<int16_t>& data) override {
            if (write_time_.count() > 0) {
                std::this_thread::sleep_for(write_time_);
            }
            std::lock_guard<std::mutex> lock(mutex_);
            frames_.push_back(data.size());
            samples_.insert(samples_.end(), data.begin(), data.end());
        }

        size_t frame_count() {
            std::lock_guard<std::mutex> lock(mutex_);
            return frames_.size();
        }
        std::vector<size_t> frame_sizes() {
            std::lock_guard<std::mutex> lock(mutex_);
            return frames_;
        }
        std::vector<int16_t> samples() {
            std::lock_guard<std::mutex> lock(mutex_);
            return samples_;
        }
        size_t sample_count() {
            std::lock_guard<std::mutex> lock(mutex_);
            return samples_.size();
        }

    private:
        Ms write_time_;
        Ms enable_delay_{0};
        bool enable_entered_ = false;
        bool output_enabled_ = false;
        mutable std::mutex mutex_;
        std::condition_variable enable_cv_;
        std::vector<size_t> frames_;
        std::vector<int16_t> samples_;
    };
'''

DRIVER_RIG = r'''
    // Captures the single finished callback per session. Heap-owned, so the worker's copy of the
    // std::function can outlive the scenario frame without dangling.
    class Completion {
    public:
        void OnFinished(uint32_t session_id, PcmTtsPlayer::Error error) {
            std::lock_guard<std::mutex> lock(mutex_);
            ++count_;
            session_id_ = session_id;
            error_ = error;
            cv_.notify_all();
        }

        bool Wait(Ms timeout) {
            std::unique_lock<std::mutex> lock(mutex_);
            return cv_.wait_for(lock, timeout, [this]() { return count_ > 0; });
        }

        int count() const {
            std::lock_guard<std::mutex> lock(mutex_);
            return count_;
        }
        uint32_t session_id() const {
            std::lock_guard<std::mutex> lock(mutex_);
            return session_id_;
        }
        PcmTtsPlayer::Error error() const {
            std::lock_guard<std::mutex> lock(mutex_);
            return error_;
        }

    private:
        mutable std::mutex mutex_;
        std::condition_variable cv_;
        int count_ = 0;
        uint32_t session_id_ = 0;
        PcmTtsPlayer::Error error_ = PcmTtsPlayer::Error::kSuccess;
    };

    // Records every state the player reports while a scenario waits for the callback.
    class StateTrace {
    public:
        void Record(PcmTtsPlayer::State state) {
            const int index = static_cast<int>(state);
            if (index >= 0 && index < 4) {
                seen_[index] = true;
            }
        }
        bool Seen(PcmTtsPlayer::State state) const {
            return seen_[static_cast<int>(state)];
        }
        std::string Names() const {
            static const char* kNames[4] = {"idle", "beginning", "active", "ending"};
            std::string text;
            for (int index = 0; index < 4; ++index) {
                if (!seen_[index]) {
                    continue;
                }
                if (!text.empty()) {
                    text += ">";
                }
                text += kNames[index];
            }
            return text;
        }

    private:
        bool seen_[4] = {false, false, false, false};
    };

    // Streams prebuilt PCM in fixed slices. pace_ms simulates a body that arrives slower than
    // real time, so a cancel lands while the worker is reading instead of writing.
    class ToneSource {
    public:
        ToneSource(const std::vector<uint8_t>& pcm, size_t chunk_bytes, int pace_ms)
            : pcm_(pcm), chunk_bytes_(chunk_bytes), pace_ms_(pace_ms) {}

        PcmTtsPlayer::ReadResult Read(std::vector<uint8_t>& chunk) {
            reads.fetch_add(1, std::memory_order_relaxed);
            if (pace_ms_ > 0) {
                std::this_thread::sleep_for(Ms(pace_ms_));
            }
            if (offset_ >= pcm_.size()) {
                return PcmTtsPlayer::ReadResult::kEndOfStream;
            }
            const size_t take = std::min(chunk_bytes_, pcm_.size() - offset_);
            chunk.assign(pcm_.begin() + offset_, pcm_.begin() + offset_ + take);
            offset_ += take;
            return PcmTtsPlayer::ReadResult::kOk;
        }

        std::atomic<size_t> reads{0};

    private:
        const std::vector<uint8_t>& pcm_;
        size_t chunk_bytes_;
        int pace_ms_;
        size_t offset_ = 0;
    };
'''


DRIVER_SOURCES = r'''

    // Fails the transport after a few good chunks: the TTS HTTP body aborting mid-stream.
    class ErrorSource {
    public:
        ErrorSource(const std::vector<uint8_t>& pcm, size_t chunk_bytes, size_t good_reads)
            : pcm_(pcm), chunk_bytes_(chunk_bytes), good_reads_(good_reads) {}

        PcmTtsPlayer::ReadResult Read(std::vector<uint8_t>& chunk) {
            reads.fetch_add(1, std::memory_order_relaxed);
            if (reads.load() > good_reads_) {
                return PcmTtsPlayer::ReadResult::kHttpFailed;
            }
            const size_t take = std::min(chunk_bytes_, pcm_.size() - offset_);
            chunk.assign(pcm_.begin() + offset_, pcm_.begin() + offset_ + take);
            offset_ += take;
            return PcmTtsPlayer::ReadResult::kOk;
        }

        std::atomic<size_t> reads{0};

    private:
        const std::vector<uint8_t>& pcm_;
        size_t chunk_bytes_;
        size_t good_reads_;
        size_t offset_ = 0;
    };

    // Reports "no data yet" a few times before delivering the body.
    class EmptyPrefixSource {
    public:
        EmptyPrefixSource(const std::vector<uint8_t>& pcm, size_t chunk_bytes, size_t empty_reads)
            : pcm_(pcm), chunk_bytes_(chunk_bytes), empty_reads_(empty_reads) {}

        PcmTtsPlayer::ReadResult Read(std::vector<uint8_t>& chunk) {
            if (empty_reads_ > 0) {
                --empty_reads_;
                chunk.clear();
                return PcmTtsPlayer::ReadResult::kOk;
            }
            if (offset_ >= pcm_.size()) {
                return PcmTtsPlayer::ReadResult::kEndOfStream;
            }
            const size_t take = std::min(chunk_bytes_, pcm_.size() - offset_);
            chunk.assign(pcm_.begin() + offset_, pcm_.begin() + offset_ + take);
            offset_ += take;
            return PcmTtsPlayer::ReadResult::kOk;
        }

    private:
        const std::vector<uint8_t>& pcm_;
        size_t chunk_bytes_;
        size_t empty_reads_;
        size_t offset_ = 0;
    };

    struct Rig {
        FakeCodec codec;
        AudioService service;
        PcmTtsPlayer player;

        explicit Rig(Ms write_time) : codec(write_time), player(service) {
            Board::SetAudioCodec(&codec);
            AudioServiceCallbacks callbacks;
            callbacks.on_playback_progress = [](uint32_t, uint32_t) {};
            service.SetCallbacks(callbacks);
            service.Initialize(&codec);
            service.Start();
        }

        ~Rig() {
            // Never leave a session behind: the detached worker must be idle before the service
            // (and its codec) is destroyed.
            player.Stop();
            const auto start = Clock::now();
            while (player.IsBusy() && ElapsedMs(start) < 3000) {
                std::this_thread::sleep_for(Ms(5));
            }
            service.Stop();
            std::this_thread::sleep_for(Ms(600));
        }
    };

    bool WaitForState(PcmTtsPlayer& player, PcmTtsPlayer::State state, Ms timeout) {
        const auto start = Clock::now();
        while (ElapsedMs(start) <= timeout.count()) {
            if (player.GetState() == state) {
                return true;
            }
            std::this_thread::sleep_for(Ms(1));
        }
        return false;
    }

    // Polls the player state until the finished callback fires, so the trace shows the whole
    // IDLE -> BEGINNING -> ACTIVE -> ENDING -> IDLE progression of the session.
    bool WaitForCompletion(PcmTtsPlayer& player, const std::shared_ptr<Completion>& done,
                           StateTrace& trace, Ms timeout) {
        const auto start = Clock::now();
        while (ElapsedMs(start) <= timeout.count()) {
            trace.Record(player.GetState());
            if (done->Wait(Ms(2))) {
                trace.Record(player.GetState());
                return true;
            }
        }
        return false;
    }
'''


DRIVER_SCENARIOS_A = r'''
    // Happy path: every chunk reaches the codec and EndPcmPlayback() flushes the tail frame.
    void RunSuccess(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO success\n");
        Rig rig(Ms(2));
        auto done = std::make_shared<Completion>();
        StateTrace trace;
        // Odd chunk size on purpose: an HTTP body arrives in whatever size the socket returns.
        auto source = std::make_shared<ToneSource>(pcm, 1001, 0);

        const uint32_t session_id = rig.player.Start(
            kSampleRate, 1,
            [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); });

        Check(session_id != 0, "success/start_returns_id", "");
        Check(rig.player.IsBusy(), "success/busy_after_start",
              PcmTtsPlayer::GetStateName(rig.player.GetState()));

        const bool completed = WaitForCompletion(rig.player, done, trace, Ms(8000));
        const std::vector<int16_t> played = rig.codec.samples();
        const std::vector<size_t> frames = rig.codec.frame_sizes();
        const std::string frame_text = Join(frames);

        std::printf("OBS success.completed=%d\n", completed ? 1 : 0);
        std::printf("OBS success.error=%s\n", PcmTtsPlayer::GetErrorName(done->error()));
        std::printf("OBS success.callbacks=%d\n", done->count());
        std::printf("OBS success.session_id_match=%d\n",
                    done->session_id() == session_id ? 1 : 0);
        std::printf("OBS success.frames=%s\n", frame_text.c_str());
        std::printf("OBS success.played_samples=%zu\n", played.size());
        std::printf("OBS success.states=%s\n", trace.Names().c_str());
        std::printf("OBS success.session_after=%u\n",
                    static_cast<unsigned>(rig.player.session_id()));

        Check(completed, "success/completed", "finished callback never fired");
        Check(done->error() == PcmTtsPlayer::Error::kSuccess, "success/error_success",
              PcmTtsPlayer::GetErrorName(done->error()));
        Check(done->count() == 1, "success/single_callback", std::to_string(done->count()));
        Check(done->session_id() == session_id, "success/callback_session_id", "");
        Check(played == AsSamples(pcm), "success/all_samples_played",
              "played=" + std::to_string(played.size()) +
                  " source=" + std::to_string(pcm.size() / sizeof(int16_t)));
        Check(frames == std::vector<size_t>{1440, 1440, 1440, 1440, 1440, 800},
              "success/frame_layout", "frames=" + frame_text);
        Check(trace.Seen(PcmTtsPlayer::State::kActive), "success/observed_active",
              trace.Names());
        Check(trace.Seen(PcmTtsPlayer::State::kEnding), "success/observed_ending",
              trace.Names());
        Check(!rig.player.IsBusy(), "success/idle_after_callback",
              PcmTtsPlayer::GetStateName(rig.player.GetState()));
        Check(rig.player.session_id() == 0, "success/session_cleared", "");
        Check(!rig.service.IsPcmPlaybackActive(), "success/audio_session_closed", "");
        Finish("success", before);
    }

    // BeginPcmPlayback() rejects every format but the codec's own 24 kHz mono output.
    void RunSampleRateMismatch(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO sample_rate_mismatch\n");
        Rig rig(Ms(2));
        auto done = std::make_shared<Completion>();
        StateTrace trace;
        auto source = std::make_shared<ToneSource>(pcm, kChunkBytes, 0);

        const uint32_t session_id = rig.player.Start(
            16000, 1, [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); });
        const bool completed = WaitForCompletion(rig.player, done, trace, Ms(4000));

        std::printf("OBS mismatch.error=%s\n", PcmTtsPlayer::GetErrorName(done->error()));
        std::printf("OBS mismatch.frames=%zu\n", rig.codec.frame_count());
        std::printf("OBS mismatch.reads=%zu\n", source->reads.load());

        Check(session_id != 0, "mismatch/start_returns_id", "");
        Check(completed, "mismatch/completed", "finished callback never fired");
        Check(done->error() == PcmTtsPlayer::Error::kSampleRateMismatch,
              "mismatch/error_sample_rate_mismatch", PcmTtsPlayer::GetErrorName(done->error()));
        Check(rig.codec.frame_count() == 0, "mismatch/nothing_played", "");
        Check(source->reads.load() == 0, "mismatch/reader_never_used", "");
        Check(!rig.service.IsPcmPlaybackActive(), "mismatch/no_audio_session", "");
        Check(!rig.player.IsBusy(), "mismatch/idle_after_callback", "");
        Finish("sample_rate_mismatch", before);
    }
'''


DRIVER_SCENARIOS_DEFER = r'''

    // §6.3: a Stop() arriving inside BeginPcmPlayback() only records pending_abort_; the worker
    // aborts right after Begin returns, so Begin and Abort never overlap.
    void RunCancelDuringBegin(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO cancel_during_begin\n");
        Rig rig(Ms(2));
        auto done = std::make_shared<Completion>();
        StateTrace trace;
        auto source = std::make_shared<ToneSource>(pcm, kChunkBytes, 0);

        rig.codec.DelayNextOutputEnable(Ms(400));

        const uint32_t session_id = rig.player.Start(
            kSampleRate, 1,
            [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); });

        const bool beginning = WaitForState(rig.player, PcmTtsPlayer::State::kBeginning, Ms(500));
        // Wait until the worker is really inside BeginPcmPlayback() (blocked in the PA power-on),
        // not merely past Start(): only then is a Stop() a cancel "during begin".
        const bool inside_begin = rig.codec.WaitForEnableEntered(Ms(2000));
        const auto stop_start = Clock::now();
        rig.player.Stop();
        const bool still_beginning = rig.player.GetState() == PcmTtsPlayer::State::kBeginning;
        const bool completed = WaitForCompletion(rig.player, done, trace, Ms(4000));
        const int64_t cancel_ms = ElapsedMs(stop_start);

        std::printf("OBS begin.error=%s\n", PcmTtsPlayer::GetErrorName(done->error()));
        std::printf("OBS begin.cancel_ms=%lld\n", static_cast<long long>(cancel_ms));
        std::printf("OBS begin.frames=%zu\n", rig.codec.frame_count());
        std::printf("OBS begin.reads=%zu\n", source->reads.load());
        std::printf("OBS begin.states=%s\n", trace.Names().c_str());

        Check(session_id != 0, "begin/start_returns_id", "");
        Check(beginning, "begin/state_beginning", trace.Names());
        Check(inside_begin, "begin/worker_inside_begin_pcm_playback", "");
        Check(still_beginning, "begin/stop_does_not_abort_inline", "");
        Check(completed, "begin/completed", "finished callback never fired");
        Check(done->error() == PcmTtsPlayer::Error::kCancelled, "begin/error_cancelled",
              PcmTtsPlayer::GetErrorName(done->error()));
        // The completion only arrives after the blocked BeginPcmPlayback() returned, which proves
        // Stop() never issued AbortPcmPlayback() against a half-open session.
        Check(cancel_ms >= 300, "begin/abort_deferred_until_after_begin",
              "cancel_ms=" + std::to_string(cancel_ms));
        Check(cancel_ms < 3000, "begin/abort_not_waiting_for_a_timeout",
              "cancel_ms=" + std::to_string(cancel_ms));
        Check(rig.codec.frame_count() == 0, "begin/nothing_played", "");
        Check(source->reads.load() == 0, "begin/reader_never_used", "");
        Check(!rig.service.IsPcmPlaybackActive(), "begin/audio_session_closed", "");
        Check(!rig.player.IsBusy(), "begin/idle_after_callback", "");
        Finish("cancel_during_begin", before);
    }
'''


DRIVER_SCENARIOS_B = r'''
    // §9.1: a cancel while streaming aborts at once and still reports exactly one completion.
    void RunCancelActive(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO cancel_active\n");
        Rig rig(Ms(2));
        auto done = std::make_shared<Completion>();
        StateTrace trace;
        // 50 ms per frame: a body that arrives slower than real time.
        auto source = std::make_shared<ToneSource>(pcm, kChunkBytes, 50);

        const uint32_t session_id = rig.player.Start(
            kSampleRate, 1,
            [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); });

        const bool active = WaitForState(rig.player, PcmTtsPlayer::State::kActive, Ms(3000));
        const auto stop_start = Clock::now();
        rig.player.Stop();
        rig.player.Stop();  // a repeated Stop() must not issue a second AbortPcmPlayback()
        const bool completed = WaitForCompletion(rig.player, done, trace, Ms(4000));
        const int64_t cancel_ms = ElapsedMs(stop_start);

        std::printf("OBS cancel.error=%s\n", PcmTtsPlayer::GetErrorName(done->error()));
        std::printf("OBS cancel.cancel_ms=%lld\n", static_cast<long long>(cancel_ms));
        std::printf("OBS cancel.callbacks=%d\n", done->count());
        std::printf("OBS cancel.played_samples=%zu\n", rig.codec.sample_count());

        Check(session_id != 0, "cancel/start_returns_id", "");
        Check(active, "cancel/became_active", "");
        Check(completed, "cancel/completed", "finished callback never fired");
        Check(done->error() == PcmTtsPlayer::Error::kCancelled, "cancel/error_cancelled",
              PcmTtsPlayer::GetErrorName(done->error()));
        Check(cancel_ms < 1500, "cancel/prompt", "cancel_ms=" + std::to_string(cancel_ms));
        Check(done->count() == 1, "cancel/single_callback", std::to_string(done->count()));
        Check(done->session_id() == session_id, "cancel/callback_session_id", "");
        Check(rig.codec.sample_count() < pcm.size() / sizeof(int16_t), "cancel/audio_truncated",
              "played=" + std::to_string(rig.codec.sample_count()));
        Check(!rig.service.IsPcmPlaybackActive(), "cancel/audio_session_closed", "");
        Check(!rig.player.IsBusy(), "cancel/idle_after_callback", "");
        Finish("cancel_active", before);
    }

    // §9.1 path (2): EndPcmPlayback() is already draining when Stop() aborts; AudioService wakes
    // the drain and returns false, which is reported as kCancelled instead of kDrainTimeout.
    void RunCancelDuringEnd(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO cancel_during_end\n");
        // 150 ms per 60 ms frame: the drain outlives the writer by a wide margin.
        Rig rig(Ms(150));
        auto done = std::make_shared<Completion>();
        StateTrace trace;
        auto source = std::make_shared<ToneSource>(pcm, kChunkBytes, 0);

        const uint32_t session_id = rig.player.Start(
            kSampleRate, 1,
            [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); });

        const bool ending = WaitForState(rig.player, PcmTtsPlayer::State::kEnding, Ms(6000));
        const auto stop_start = Clock::now();
        rig.player.Stop();
        const bool completed = WaitForCompletion(rig.player, done, trace, Ms(6000));
        const int64_t cancel_ms = ElapsedMs(stop_start);

        std::printf("OBS end_cancel.error=%s\n", PcmTtsPlayer::GetErrorName(done->error()));
        std::printf("OBS end_cancel.cancel_ms=%lld\n", static_cast<long long>(cancel_ms));
        std::printf("OBS end_cancel.states=%s\n", trace.Names().c_str());
        std::printf("OBS end_cancel.played_samples=%zu\n", rig.codec.sample_count());

        Check(session_id != 0, "end_cancel/start_returns_id", "");
        Check(ending, "end_cancel/reached_ending", trace.Names());
        Check(completed, "end_cancel/completed", "finished callback never fired");
        Check(done->error() == PcmTtsPlayer::Error::kCancelled, "end_cancel/error_cancelled",
              PcmTtsPlayer::GetErrorName(done->error()));
        Check(cancel_ms < 1500, "end_cancel/woken_early",
              "cancel_ms=" + std::to_string(cancel_ms));
        Check(!rig.service.IsPcmPlaybackActive(), "end_cancel/audio_session_closed", "");
        Check(!rig.player.IsBusy(), "end_cancel/idle_after_callback", "");
        Finish("cancel_during_end", before);
    }
'''


DRIVER_SCENARIOS_C = r'''

    // A rejected write without any cancel request (here: ResetDecoder() clearing the playback
    // queue) means the tail can never drain: kDrainTimeout, not kCancelled.
    void RunWriteRejected(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO write_rejected\n");
        Rig rig(Ms(2));
        auto done = std::make_shared<Completion>();
        StateTrace trace;
        auto source = std::make_shared<ToneSource>(pcm, kChunkBytes, 40);

        const uint32_t session_id = rig.player.Start(
            kSampleRate, 1,
            [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); });

        const bool active = WaitForState(rig.player, PcmTtsPlayer::State::kActive, Ms(3000));
        std::this_thread::sleep_for(Ms(120));  // let a couple of frames through
        rig.service.ResetDecoder();            // bumps playback_generation_ behind the player
        const bool completed = WaitForCompletion(rig.player, done, trace, Ms(4000));

        std::printf("OBS rejected.error=%s\n", PcmTtsPlayer::GetErrorName(done->error()));
        std::printf("OBS rejected.reads=%zu\n", source->reads.load());
        std::printf("OBS rejected.played_samples=%zu\n", rig.codec.sample_count());

        Check(session_id != 0, "rejected/start_returns_id", "");
        Check(active, "rejected/became_active", "");
        Check(completed, "rejected/completed", "finished callback never fired");
        Check(done->error() == PcmTtsPlayer::Error::kDrainTimeout, "rejected/error_drain_timeout",
              PcmTtsPlayer::GetErrorName(done->error()));
        Check(!rig.service.IsPcmPlaybackActive(), "rejected/audio_session_closed", "");
        Check(!rig.player.IsBusy(), "rejected/idle_after_callback", "");
        Finish("write_rejected", before);
    }

    // The TTS transport dies mid-body: the player reports kHttpFailed and still closes cleanly.
    void RunSourceError(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO source_error\n");
        Rig rig(Ms(2));
        auto done = std::make_shared<Completion>();
        StateTrace trace;
        auto source = std::make_shared<ErrorSource>(pcm, kChunkBytes, 2);

        const uint32_t session_id = rig.player.Start(
            kSampleRate, 1,
            [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); });
        const bool completed = WaitForCompletion(rig.player, done, trace, Ms(4000));

        std::printf("OBS source_error.error=%s\n", PcmTtsPlayer::GetErrorName(done->error()));
        std::printf("OBS source_error.reads=%zu\n", source->reads.load());
        std::printf("OBS source_error.played_samples=%zu\n", rig.codec.sample_count());

        Check(session_id != 0, "source_error/start_returns_id", "");
        Check(completed, "source_error/completed", "finished callback never fired");
        Check(done->error() == PcmTtsPlayer::Error::kHttpFailed, "source_error/error_http_failed",
              PcmTtsPlayer::GetErrorName(done->error()));
        Check(rig.codec.sample_count() == 2 * kFrameSamples, "source_error/played_before_failure",
              "played=" + std::to_string(rig.codec.sample_count()));
        Check(!rig.service.IsPcmPlaybackActive(), "source_error/audio_session_closed", "");
        Check(!rig.player.IsBusy(), "source_error/idle_after_callback", "");
        Finish("source_error", before);
    }

    // An empty chunk means "no data yet": the worker must back off, not spin or fail.
    void RunEmptyChunks(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO empty_chunks\n");
        Rig rig(Ms(2));
        auto done = std::make_shared<Completion>();
        StateTrace trace;
        auto source = std::make_shared<EmptyPrefixSource>(pcm, kChunkBytes, 3);

        const uint32_t session_id = rig.player.Start(
            kSampleRate, 1,
            [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); });
        const bool completed = WaitForCompletion(rig.player, done, trace, Ms(4000));

        std::printf("OBS empty.error=%s\n", PcmTtsPlayer::GetErrorName(done->error()));
        std::printf("OBS empty.played_samples=%zu\n", rig.codec.sample_count());

        Check(session_id != 0, "empty/start_returns_id", "");
        Check(completed, "empty/completed", "finished callback never fired");
        Check(done->error() == PcmTtsPlayer::Error::kSuccess, "empty/error_success",
              PcmTtsPlayer::GetErrorName(done->error()));
        Check(rig.codec.samples() == AsSamples(pcm), "empty/all_samples_played", "");
        Check(!rig.service.IsPcmPlaybackActive(), "empty/audio_session_closed", "");
        Finish("empty_chunks", before);
    }
'''


DRIVER_SCENARIOS_D = r'''

    // Start() is single-session: a second start while streaming is rejected and never runs.
    void RunBusyStart(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO busy_start\n");
        Rig rig(Ms(2));
        auto done = std::make_shared<Completion>();
        StateTrace trace;
        auto source = std::make_shared<ToneSource>(pcm, kChunkBytes, 20);

        const uint32_t first = rig.player.Start(
            kSampleRate, 1,
            [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); });
        const bool active = WaitForState(rig.player, PcmTtsPlayer::State::kActive, Ms(3000));

        auto rejected_source = std::make_shared<ToneSource>(pcm, kChunkBytes, 0);
        const uint32_t second = rig.player.Start(
            kSampleRate, 1,
            [rejected_source](std::vector<uint8_t>& chunk) {
                return rejected_source->Read(chunk);
            },
            [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); });

        std::printf("OBS busy.first=%u\n", static_cast<unsigned>(first));
        std::printf("OBS busy.second=%u\n", static_cast<unsigned>(second));
        std::printf("OBS busy.rejected_reads=%zu\n", rejected_source->reads.load());

        Check(first != 0, "busy/first_started", "");
        Check(active, "busy/first_active", "");
        Check(second == 0, "busy/second_rejected", std::to_string(second));
        Check(rig.player.session_id() == first, "busy/session_unchanged", "");
        Check(rejected_source->reads.load() == 0, "busy/rejected_reader_never_used", "");
        Check(done->count() == 0, "busy/no_callback_from_rejected_start",
              std::to_string(done->count()));

        rig.player.Stop();
        const bool completed = WaitForCompletion(rig.player, done, trace, Ms(4000));
        Check(completed, "busy/first_completed", "finished callback never fired");
        Check(done->count() == 1 && done->session_id() == first,
              "busy/only_first_session_finished",
              "callbacks=" + std::to_string(done->count()));
        Check(done->error() == PcmTtsPlayer::Error::kCancelled, "busy/first_cancelled",
              PcmTtsPlayer::GetErrorName(done->error()));
        Finish("busy_start", before);
    }
'''


DRIVER_SCENARIOS_E = r'''

    // Two sessions in a row: ids increase and no state or reader leaks into the next session.
    void RunSequentialSessions(const std::vector<uint8_t>& pcm) {
        const int before = g_failures;
        std::printf("SCENARIO sequential_sessions\n");
        Rig rig(Ms(2));
        auto first_done = std::make_shared<Completion>();
        auto second_done = std::make_shared<Completion>();
        StateTrace trace;
        auto source = std::make_shared<ToneSource>(pcm, kChunkBytes, 0);

        // Invalid starts are rejected without touching the state machine.
        const uint32_t no_reader = rig.player.Start(
            kSampleRate, 1, nullptr, [](uint32_t, PcmTtsPlayer::Error) {});
        const uint32_t no_callback = rig.player.Start(
            kSampleRate, 1, [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            nullptr);
        Check(no_reader == 0, "sequential/missing_reader_rejected", std::to_string(no_reader));
        Check(no_callback == 0, "sequential/missing_callback_rejected",
              std::to_string(no_callback));
        Check(!rig.player.IsBusy(), "sequential/invalid_start_keeps_idle", "");

        const uint32_t first = rig.player.Start(
            kSampleRate, 1,
            [source](std::vector<uint8_t>& chunk) { return source->Read(chunk); },
            [first_done](uint32_t id, PcmTtsPlayer::Error error) {
                first_done->OnFinished(id, error);
            });
        const bool first_completed = WaitForCompletion(rig.player, first_done, trace, Ms(4000));

        auto second_source = std::make_shared<ToneSource>(pcm, kChunkBytes, 0);
        const uint32_t second = rig.player.Start(
            kSampleRate, 1,
            [second_source](std::vector<uint8_t>& chunk) { return second_source->Read(chunk); },
            [second_done](uint32_t id, PcmTtsPlayer::Error error) {
                second_done->OnFinished(id, error);
            });
        const bool second_completed = WaitForCompletion(rig.player, second_done, trace, Ms(4000));

        std::printf("OBS sequential.first=%u\n", static_cast<unsigned>(first));
        std::printf("OBS sequential.second=%u\n", static_cast<unsigned>(second));
        std::printf("OBS sequential.first_error=%s\n",
                    PcmTtsPlayer::GetErrorName(first_done->error()));
        std::printf("OBS sequential.second_error=%s\n",
                    PcmTtsPlayer::GetErrorName(second_done->error()));
        std::printf("OBS sequential.played_samples=%zu\n", rig.codec.sample_count());

        Check(first_completed && second_completed, "sequential/both_completed", "");
        Check(first != 0 && second > first, "sequential/ids_increase", "");
        Check(first_done->session_id() == first && first_done->count() == 1,
              "sequential/first_callback", std::to_string(first_done->session_id()));
        Check(second_done->session_id() == second && second_done->count() == 1,
              "sequential/second_callback", std::to_string(second_done->session_id()));
        Check(first_done->error() == PcmTtsPlayer::Error::kSuccess &&
                  second_done->error() == PcmTtsPlayer::Error::kSuccess,
              "sequential/both_success", "");
        Check(rig.codec.sample_count() == 2 * (pcm.size() / sizeof(int16_t)),
              "sequential/audio_played_twice",
              "played=" + std::to_string(rig.codec.sample_count()));
        Check(!rig.player.IsBusy() && rig.player.session_id() == 0, "sequential/idle_at_end", "");
        Finish("sequential_sessions", before);
    }

    }  // namespace
'''


DRIVER_MAIN = r'''
    int main(int argc, char** argv) {
        if (argc < 3) {
            std::printf("usage: %s <tone.pcm> <scenario>\n", argv[0]);
            return 2;
        }
        std::ifstream stream(argv[1], std::ios::binary);
        const std::vector<uint8_t> pcm{std::istreambuf_iterator<char>(stream),
                                       std::istreambuf_iterator<char>()};
        if (pcm.size() < 3 * kFrameBytes) {
            std::printf("pcm file too small: %zu bytes\n", pcm.size());
            return 2;
        }

        const std::string scenario = argv[2];
        if (scenario == "success") {
            RunSuccess(pcm);
        } else if (scenario == "sample_rate_mismatch") {
            RunSampleRateMismatch(pcm);
        } else if (scenario == "cancel_during_begin") {
            RunCancelDuringBegin(pcm);
        } else if (scenario == "cancel_active") {
            RunCancelActive(pcm);
        } else if (scenario == "cancel_during_end") {
            RunCancelDuringEnd(pcm);
        } else if (scenario == "write_rejected") {
            RunWriteRejected(pcm);
        } else if (scenario == "source_error") {
            RunSourceError(pcm);
        } else if (scenario == "empty_chunks") {
            RunEmptyChunks(pcm);
        } else if (scenario == "busy_start") {
            RunBusyStart(pcm);
        } else if (scenario == "sequential_sessions") {
            RunSequentialSessions(pcm);
        } else {
            std::printf("unknown scenario: %s\n", scenario.c_str());
            return 2;
        }
        return g_failures == 0 ? 0 : 1;
    }
'''

DRIVER = (
    DRIVER_HEAD
    + DRIVER_RIG
    + DRIVER_SOURCES
    + DRIVER_SCENARIOS_A
    + DRIVER_SCENARIOS_DEFER
    + DRIVER_SCENARIOS_B
    + DRIVER_SCENARIOS_C
    + DRIVER_SCENARIOS_D
    + DRIVER_SCENARIOS_E
    + DRIVER_MAIN
)


class PcmTtsPlayerTest(unittest.TestCase):
    """Runs the player state machine against the real audio_service.cc with stubbed platform APIs."""

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

        driver = build / "pcm_tts_player_driver.cc"
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
        for name in ("pcm_tts_player.cc", "pcm_tts_player.h"):
            target = build / name
            shutil.copyfile(ROOT / "main" / "reminder" / name, target)
            if name.endswith(".cc"):
                sources.append(str(target))

        cls.exe_path = build / ("pcm_tts_player" + (".exe" if os.name == "nt" else ""))
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
            raise AssertionError("failed to build the PCM TTS player host test:\n" + compiled.stderr)

    def _run(self, scenario):
        completed = subprocess.run(
            [str(self.exe_path), str(self.pcm_path), scenario],
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = completed.stdout + completed.stderr
        if os.environ.get("PCM_TTS_PLAYER_VERBOSE"):
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


    def test_success_streams_every_sample_and_drains(self):
        output = self._run("success")
        self.assertEqual("1", self._obs(output, "success.completed"))
        self.assertEqual("success", self._obs(output, "success.error"))
        self.assertEqual("1", self._obs(output, "success.callbacks"))
        self.assertEqual("1", self._obs(output, "success.session_id_match"))
        self.assertEqual("1440,1440,1440,1440,1440,800", self._obs(output, "success.frames"))
        self.assertEqual("8000", self._obs(output, "success.played_samples"))
        self.assertEqual("0", self._obs(output, "success.session_after"))
        # Every state of the contract is observed while the session runs.
        states = self._obs(output, "success.states")
        for name in ("active", "ending"):
            self.assertIn(name, states)

    def test_unsupported_format_reports_sample_rate_mismatch(self):
        output = self._run("sample_rate_mismatch")
        self.assertEqual("sample-rate-mismatch", self._obs(output, "mismatch.error"))
        self.assertEqual("0", self._obs(output, "mismatch.frames"))
        self.assertEqual("0", self._obs(output, "mismatch.reads"))

    def test_stop_during_begin_defers_the_abort(self):
        output = self._run("cancel_during_begin")
        self.assertEqual("cancelled", self._obs(output, "begin.error"))
        self.assertEqual("0", self._obs(output, "begin.frames"))
        self.assertEqual("0", self._obs(output, "begin.reads"))
        # The completion only arrives after the blocked BeginPcmPlayback() returned (400 ms PA
        # power-on), which is what keeps Begin and Abort from overlapping.
        self.assertGreaterEqual(int(self._obs(output, "begin.cancel_ms")), 300)
        self.assertLess(int(self._obs(output, "begin.cancel_ms")), 3000)

    def test_stop_while_streaming_cancels_once(self):
        output = self._run("cancel_active")
        self.assertEqual("cancelled", self._obs(output, "cancel.error"))
        self.assertEqual("1", self._obs(output, "cancel.callbacks"))
        self.assertLess(int(self._obs(output, "cancel.cancel_ms")), 1500)
        self.assertLess(int(self._obs(output, "cancel.played_samples")), 8000)

    def test_stop_during_drain_reports_cancelled(self):
        output = self._run("cancel_during_end")
        self.assertEqual("cancelled", self._obs(output, "end_cancel.error"))
        self.assertLess(int(self._obs(output, "end_cancel.cancel_ms")), 1500)

    def test_write_rejected_without_cancel_reports_drain_timeout(self):
        output = self._run("write_rejected")
        self.assertEqual("drain-timeout", self._obs(output, "rejected.error"))

    def test_source_error_reports_http_failed(self):
        output = self._run("source_error")
        self.assertEqual("http-failed", self._obs(output, "source_error.error"))
        # The two good chunks still reached the codec before the transport failed.
        self.assertEqual("2880", self._obs(output, "source_error.played_samples"))

    def test_empty_chunks_are_not_failures(self):
        output = self._run("empty_chunks")
        self.assertEqual("success", self._obs(output, "empty.error"))
        self.assertEqual("8000", self._obs(output, "empty.played_samples"))

    def test_second_start_is_rejected_while_busy(self):
        output = self._run("busy_start")
        self.assertEqual("0", self._obs(output, "busy.second"))
        self.assertEqual("0", self._obs(output, "busy.rejected_reads"))
        self.assertIn("CHECK PASS busy/first_cancelled", output)

    def test_sessions_run_back_to_back_with_increasing_ids(self):
        output = self._run("sequential_sessions")
        first = int(self._obs(output, "sequential.first"))
        second = int(self._obs(output, "sequential.second"))
        self.assertNotEqual(0, first)
        self.assertGreater(second, first)
        self.assertEqual("success", self._obs(output, "sequential.first_error"))
        self.assertEqual("success", self._obs(output, "sequential.second_error"))
        self.assertEqual("16000", self._obs(output, "sequential.played_samples"))


if __name__ == "__main__":
    unittest.main()

