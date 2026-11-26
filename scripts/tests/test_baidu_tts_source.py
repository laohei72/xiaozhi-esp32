"""Host-side tests for main/reminder/baidu_tts_source.* (stage 4b self test).

The source is compiled against the AudioService / PcmTtsPlayer stubs shared with
``test_pcm_playback_end.py``, so every assertion exercises the real streaming path: a scripted
Baidu reply travels through a fake ``Http`` transport, then
``BaiduTtsSource -> PcmTtsPlayer -> AudioService -> fake codec``.

Verified interface (do not change it back):
    POST https://aip.baidubce.com/rest/2.0/speech/publiccloudspeech/v1/tts?access_token=<token>
    Content-Type: application/json
    {"text": ..., "voice": "5003", "lang": "zh", "format": "pcm", "sample_rate": 24000,
     "speed": 1, "pitch": 0, "volume": 1}
    -> audio/basic;codec=pcm;rate=24000;channel=1 (s16le, mono)

The token comes from POST https://aip.baidubce.com/oauth/2.0/token with the AK/SK in an
``application/x-www-form-urlencoded`` body (never in the URL) and is cached until it expires.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_pcm_playback_end import ROOT, STUBS  # noqa: E402  (shared host stubs)

CJSON_DIR = ROOT / "managed_components" / "espressif__cjson" / "cJSON"

DRIVER_HEAD = r'''
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "audio_service.h"
#include "baidu_tts_source.h"
#include "http.h"
#include "pcm_tts_player.h"

namespace {

using Clock = std::chrono::steady_clock;
using Ms = std::chrono::milliseconds;

constexpr int kSampleRate = 24000;
constexpr int kChannels = 1;
constexpr size_t kFrameSamples = 1440;  // AudioService PCM frame: 60 ms @ 24 kHz
constexpr size_t kChunkBytes = 1001;    // odd on purpose, like a socket read
constexpr char kText[] = "测试一下，这是提醒时间的语音播报。";
constexpr char kApiKey[] = "AK-test";
constexpr char kSecretKey[] = "SK-test";
constexpr char kSpeechUrl[] =
    "https://aip.baidubce.com/rest/2.0/speech/publiccloudspeech/v1/tts";
constexpr char kTokenUrl[] = "https://aip.baidubce.com/oauth/2.0/token";

int g_failures = 0;

int64_t ElapsedMs(Clock::time_point start) {
    return std::chrono::duration_cast<Ms>(Clock::now() - start).count();
}

void Check(bool condition, const std::string& name, const std::string& detail) {
    if (condition) {
        std::printf("PASS %s\n", name.c_str());
        return;
    }
    ++g_failures;
    std::printf("FAIL %s (%s)\n", name.c_str(), detail.c_str());
}

void Finish(const char* scenario, int failures_before) {
    std::printf("SCENARIO_DONE %s %s\n", scenario,
                g_failures == failures_before ? "ok" : "failed");
    std::fflush(stdout);
}

// 8000 samples = 5 full AudioService frames plus a 800-sample tail the drain has to flush.
constexpr size_t kTotalSamples = 8000;

std::vector<uint8_t> MakePcm() {
    std::vector<uint8_t> pcm(kTotalSamples * sizeof(int16_t), 0);
    for (size_t i = 0; i < kTotalSamples; ++i) {
        const int16_t sample = static_cast<int16_t>((i * 37) % 20001 - 10000);
        std::memcpy(pcm.data() + i * sizeof(int16_t), &sample, sizeof(sample));
    }
    return pcm;
}

/** One scripted HTTP response: what the fake transport answers for a single request. */
struct ScriptedResponse {
    int status = 200;
    std::string content_type;
    std::string body;
    size_t chunk_bytes = kChunkBytes;
    int pace_ms = 0;         // delay before every body read (slow network)
    int fail_read_at = -1;   // body read index that fails the transport (-1: never)
    bool fail_open = false;  // Open() fails (no network / DNS)
    bool fail_status = false;  // GetStatusCode() fails
};

/** Shared between the rig and every fake transport it creates. */
struct TransportScript {
    std::mutex mutex;
    std::deque<ScriptedResponse> pending;
    std::vector<std::string> urls;
    std::vector<std::string> contents;
    std::vector<std::string> request_content_types;
    std::vector<int> active_at_open;
    std::function<bool()> probe_audio_active;
    std::string voice;  // what the credential loader reports; empty means "source default"
    std::atomic<int> opens{0};
    std::atomic<int> closes{0};
    std::atomic<int> body_reads{0};

    void Push(ScriptedResponse response) {
        std::lock_guard<std::mutex> lock(mutex);
        pending.push_back(std::move(response));
    }

    ScriptedResponse Next() {
        std::lock_guard<std::mutex> lock(mutex);
        if (pending.empty()) {
            return ScriptedResponse{};
        }
        ScriptedResponse response = std::move(pending.front());
        pending.pop_front();
        return response;
    }
};
'''

DRIVER_HTTP = r'''
/** Plays back a scripted response: no socket, DNS or TLS is involved. */
class FakeHttp : public Http {
public:
    explicit FakeHttp(std::shared_ptr<TransportScript> script) : script_(std::move(script)) {}

    void SetTimeout(int timeout_ms) override { timeout_ms_ = timeout_ms; }
    void SetHeader(const std::string& key, const std::string& value) override {
        if (key == "Content-Type") {
            header_content_type_ = value;
        }
    }
    void SetContent(std::string&& content) override { content_ = std::move(content); }
    void SetKeepAlive(bool enable) override { keep_alive_ = enable; }

    NetworkResult<> Open(const std::string& method, const std::string& url) override {
        method_ = method;
        url_ = url;
        response_ = script_->Next();
        {
            std::lock_guard<std::mutex> lock(script_->mutex);
            script_->urls.push_back(url);
            script_->contents.push_back(content_);
            script_->request_content_types.push_back(header_content_type_);
            const bool active =
                script_->probe_audio_active ? script_->probe_audio_active() : false;
            script_->active_at_open.push_back(active ? 1 : 0);
        }
        script_->opens.fetch_add(1);
        if (response_.fail_open) {
            return Fail(NetworkError::ConnectFailed(-1));
        }
        opened_ = true;
        return {};
    }

    void Close() override {
        if (opened_) {
            opened_ = false;
            script_->closes.fetch_add(1);
        }
    }

    NetworkResult<int> Read(char* buffer, size_t buffer_size) override {
        const int index = read_index_++;
        if (response_.pace_ms > 0) {
            std::this_thread::sleep_for(Ms(response_.pace_ms));
        }
        script_->body_reads.fetch_add(1);
        if (response_.fail_read_at >= 0 && index >= response_.fail_read_at) {
            return FailValue<int>(NetworkError::ServerDisconnected(-1));
        }
        if (offset_ >= response_.body.size()) {
            return 0;
        }
        const size_t take = std::min({buffer_size, response_.chunk_bytes,
                                      response_.body.size() - offset_});
        std::memcpy(buffer, response_.body.data() + offset_, take);
        offset_ += take;
        return static_cast<int>(take);
    }

    NetworkResult<int> Write(const char* buffer, size_t buffer_size) override {
        (void)buffer;
        (void)buffer_size;
        return 0;
    }

    NetworkResult<int> GetStatusCode() override {
        if (response_.fail_status) {
            return FailValue<int>(NetworkError::Timeout(-1));
        }
        return response_.status;
    }

    std::string GetResponseHeader(const std::string& key) const override {
        if (key == "Content-Type") {
            return response_.content_type;
        }
        return std::string();
    }

    size_t GetBodyLength() override { return response_.body.size(); }

    std::string ReadAll() override {
        std::string payload = response_.body.substr(offset_);
        offset_ = response_.body.size();
        return payload;
    }

    const std::string& method() const { return method_; }
    const std::string& url() const { return url_; }
    const std::string& content() const { return content_; }

private:
    std::shared_ptr<TransportScript> script_;
    ScriptedResponse response_;
    size_t offset_ = 0;
    int read_index_ = 0;
    int timeout_ms_ = 0;
    bool keep_alive_ = false;
    bool opened_ = false;
    std::string method_;
    std::string url_;
    std::string content_;
    std::string header_content_type_;
};
'''


DRIVER_ASSERTS = r'''
bool Contains(const std::string& haystack, const std::string& needle) {
    return haystack.find(needle) != std::string::npos;
}

// The verified JSON request body: PCM, 24 kHz, speed 1, pitch 0, volume 1, and no legacy
// tok/per/aue form parameters.
bool BodyLooksVerified(const std::string& body, const std::string& voice) {
    return Contains(body, "\"text\":\"测试一下") &&
           Contains(body, "\"voice\":\"" + voice + "\"") && Contains(body, "\"lang\":\"zh\"") &&
           Contains(body, "\"format\":\"pcm\"") && Contains(body, "\"sample_rate\":24000") &&
           Contains(body, "\"speed\":1") && Contains(body, "\"pitch\":0") &&
           Contains(body, "\"volume\":1") && !Contains(body, "aue") &&
           !Contains(body, "\"tok\"") && !Contains(body, "\"per\"");
}

struct RequestSummary {
    int opens = 0;
    int closes = 0;
    int token_requests = 0;
    int tts_requests = 0;
    int secrets_in_url = 0;
    int active_at_open = 0;
    int token_url_with_query = 0;
    bool tts_url_ok = true;
    bool tts_body_ok = true;
    bool token_body_ok = true;
    bool headers_ok = true;
};

// Classifies every request the source issued and checks the shape of the verified interface.
RequestSummary Summarize(TransportScript& script) {
    RequestSummary summary;
    const std::string expected_voice = script.voice.empty() ? "5003" : script.voice;
    std::lock_guard<std::mutex> lock(script.mutex);
    summary.opens = script.opens.load();
    summary.closes = script.closes.load();
    for (size_t i = 0; i < script.urls.size(); ++i) {
        const std::string& url = script.urls[i];
        const std::string& content = script.contents[i];
        const std::string& content_type = script.request_content_types[i];
        if (Contains(url, "secret")) {
            ++summary.secrets_in_url;
        }
        if (script.active_at_open[i] != 0) {
            ++summary.active_at_open;
        }
        if (url.find(kTokenUrl) == 0) {
            ++summary.token_requests;
            if (Contains(url, "?")) {
                ++summary.token_url_with_query;
            }
            if (content != "grant_type=client_credentials&client_id=AK-test"
                           "&client_secret=SK-test") {
                summary.token_body_ok = false;
            }
            if (content_type != "application/x-www-form-urlencoded") {
                summary.headers_ok = false;
            }
        } else if (url.find(kSpeechUrl) == 0) {
            ++summary.tts_requests;
            if (!Contains(url, "?access_token=")) {
                summary.tts_url_ok = false;
            }
            if (!BodyLooksVerified(content, expected_voice)) {
                summary.tts_body_ok = false;
            }
            if (content_type != "application/json") {
                summary.headers_ok = false;
            }
        } else {
            summary.tts_url_ok = false;
        }
    }
    return summary;
}

ScriptedResponse MakeToken(const std::string& token, long expires_in) {
    ScriptedResponse response;
    response.status = 200;
    response.content_type = "application/json";
    response.body = "{\"access_token\":\"" + token + "\",\"expires_in\":" +
                    std::to_string(expires_in) +
                    ",\"refresh_token\":\"refresh\",\"scope\":\"audio_tts_post\"}";
    return response;
}

// Case and spacing differ from the documented spelling on purpose: the parser must not care.
ScriptedResponse MakeAudio(const std::vector<uint8_t>& pcm) {
    ScriptedResponse response;
    response.status = 200;
    response.content_type = "Audio/Basic; codec=PCM; rate=24000; channel=1";
    response.body.assign(pcm.begin(), pcm.end());
    return response;
}

ScriptedResponse MakeJsonStatus(int status, const std::string& body) {
    ScriptedResponse response;
    response.status = status;
    response.content_type = "application/json";
    response.body = body;
    return response;
}
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

// Simulates the duration of an I2S write so the playback thread drains in real time.
class FakeCodec : public AudioCodec {
public:
    explicit FakeCodec(Ms write_time) : write_time_(write_time) {}

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
    size_t sample_count() {
        std::lock_guard<std::mutex> lock(mutex_);
        return samples_.size();
    }
    std::string FrameSizes() {
        std::lock_guard<std::mutex> lock(mutex_);
        std::string text;
        for (size_t size : frames_) {
            if (!text.empty()) {
                text += ",";
            }
            text += std::to_string(size);
        }
        return text;
    }

private:
    Ms write_time_;
    mutable std::mutex mutex_;
    std::vector<size_t> frames_;
    std::vector<int16_t> samples_;
};

enum class Credentials { kPresent, kMissing };

BaiduTtsSource::ConfigLoader MakeLoader(Credentials credentials,
                                        std::shared_ptr<TransportScript> script) {
    return [credentials, script](BaiduTtsConfig& config) {
        if (credentials == Credentials::kMissing) {
            config.api_key.clear();
            config.secret_key.clear();
            return false;
        }
        config.api_key = kApiKey;
        config.secret_key = kSecretKey;
        // Empty inside the source means "use the documented default speaker".
        config.voice = script->voice;
        return true;
    };
}

BaiduTtsSource::HttpFactory MakeFactory(std::shared_ptr<TransportScript> script) {
    return [script]() -> std::unique_ptr<Http> { return std::make_unique<FakeHttp>(script); };
}

struct Rig {
    FakeCodec codec;
    AudioService service;
    std::shared_ptr<TransportScript> script;
    BaiduTtsSource source;
    PcmTtsPlayer player;

    Rig(Ms write_time, Credentials credentials)
        : codec(write_time),
          script(std::make_shared<TransportScript>()),
          source(MakeLoader(credentials, script), MakeFactory(script)),
          player(service) {
        Board::SetAudioCodec(&codec);
        AudioServiceCallbacks callbacks;
        callbacks.on_playback_progress = [](uint32_t, uint32_t) {};
        service.SetCallbacks(callbacks);
        service.Initialize(&codec);
        service.Start();
        // Every request must be issued before the shared playback path is opened.
        script->probe_audio_active = [this]() { return service.IsPcmPlaybackActive(); };
    }

    ~Rig() {
        // Never leave a session behind: the detached worker must be idle before the service
        // (and its codec) is destroyed.
        source.Cancel();
        player.Stop();
        const auto start = Clock::now();
        while (player.IsBusy() && ElapsedMs(start) < 3000) {
            std::this_thread::sleep_for(Ms(5));
        }
        service.Stop();
        std::this_thread::sleep_for(Ms(600));
    }
};

struct SessionResult {
    uint32_t session_id = 0;
    bool completed = false;
    int callbacks = 0;
    PcmTtsPlayer::Error error = PcmTtsPlayer::Error::kSuccess;
    size_t frames = 0;
    size_t played_samples = 0;
    int64_t elapsed_ms = 0;
};

// Starts the source behind the player exactly like Application::StartPcmTtsSession() does.
SessionResult RunSession(Rig& rig, Ms timeout) {
    auto done = std::make_shared<Completion>();
    SessionResult result;
    const size_t frames_before = rig.codec.frame_count();
    const size_t samples_before = rig.codec.sample_count();
    const auto start = Clock::now();
    result.session_id = rig.player.Start(
        kSampleRate, kChannels,
        [&rig](std::vector<uint8_t>& chunk) { return rig.source.Read(chunk); },
        [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); },
        [&rig]() { return rig.source.Open(kText); },
        [&rig]() { rig.source.Close(); });
    if (result.session_id == 0) {
        return result;
    }
    result.completed = done->Wait(timeout);
    if (result.completed) {
        result.elapsed_ms = ElapsedMs(start);
        result.callbacks = done->count();
        result.error = done->error();
    } else {
        rig.source.Cancel();
        rig.player.Stop();
    }
    result.frames = rig.codec.frame_count() - frames_before;
    result.played_samples = rig.codec.sample_count() - samples_before;
    return result;
}

void Report(const char* scenario, const SessionResult& result, const RequestSummary& requests,
            Rig& rig) {
    std::printf("OBS %s.completed=%d\n", scenario, result.completed ? 1 : 0);
    std::printf("OBS %s.error=%s\n", scenario, PcmTtsPlayer::GetErrorName(result.error));
    std::printf("OBS %s.callbacks=%d\n", scenario, result.callbacks);
    std::printf("OBS %s.frames=%zu\n", scenario, result.frames);
    std::printf("OBS %s.frame_sizes=%s\n", scenario, rig.codec.FrameSizes().c_str());
    std::printf("OBS %s.played_samples=%zu\n", scenario, result.played_samples);
    std::printf("OBS %s.elapsed_ms=%lld\n", scenario, static_cast<long long>(result.elapsed_ms));
    std::printf("OBS %s.opens=%d\n", scenario, requests.opens);
    std::printf("OBS %s.closes=%d\n", scenario, requests.closes);
    std::printf("OBS %s.token_requests=%d\n", scenario, requests.token_requests);
    std::printf("OBS %s.tts_requests=%d\n", scenario, requests.tts_requests);
    std::printf("OBS %s.secrets_in_url=%d\n", scenario, requests.secrets_in_url);
    std::printf("OBS %s.active_at_open=%d\n", scenario, requests.active_at_open);
    std::printf("OBS %s.token_url_with_query=%d\n", scenario, requests.token_url_with_query);
    std::printf("OBS %s.tts_url_ok=%d\n", scenario, requests.tts_url_ok ? 1 : 0);
    std::printf("OBS %s.tts_body_ok=%d\n", scenario, requests.tts_body_ok ? 1 : 0);
    std::printf("OBS %s.token_body_ok=%d\n", scenario, requests.token_body_ok ? 1 : 0);
    std::printf("OBS %s.headers_ok=%d\n", scenario, requests.headers_ok ? 1 : 0);
    std::printf("OBS %s.player_idle=%d\n", scenario, rig.player.IsBusy() ? 0 : 1);
    std::printf("OBS %s.pcm_active=%d\n", scenario, rig.service.IsPcmPlaybackActive() ? 1 : 0);
    std::fflush(stdout);
}

// --- scenarios (stage 4b) ---------------------------------------------------

// tts_api_key / tts_secret_key missing: no request leaves the device.
void RunMissingCredentials() {
    const int before = g_failures;
    std::printf("SCENARIO missing_credentials\n");
    Rig rig(Ms(2), Credentials::kMissing);
    const SessionResult result = RunSession(rig, Ms(4000));
    const RequestSummary requests = Summarize(*rig.script);
    Report("missing_credentials", result, requests, rig);
    Check(result.completed, "missing_credentials/completed", "");
    Check(result.error == PcmTtsPlayer::Error::kApiKeyMissing, "missing_credentials/error",
          PcmTtsPlayer::GetErrorName(result.error));
    Check(requests.opens == 0, "missing_credentials/no_request",
          std::to_string(requests.opens));
    Check(result.frames == 0, "missing_credentials/nothing_played",
          std::to_string(result.frames));
    Check(!rig.service.IsPcmPlaybackActive(), "missing_credentials/no_audio_session", "");
    Finish("missing_credentials", before);
}

// The verified path end to end: token, verified JSON request, PCM body, drained playback.
void RunSuccess() {
    const int before = g_failures;
    const std::vector<uint8_t> pcm = MakePcm();
    std::printf("SCENARIO success\n");
    Rig rig(Ms(2), Credentials::kPresent);
    rig.script->Push(MakeToken("token-abc", 2592000));
    rig.script->Push(MakeAudio(pcm));
    const SessionResult result = RunSession(rig, Ms(15000));
    const RequestSummary requests = Summarize(*rig.script);
    const std::string frame_sizes = rig.codec.FrameSizes();
    Report("success", result, requests, rig);
    Check(result.session_id != 0, "success/start_returns_id",
          std::to_string(result.session_id));
    Check(result.completed, "success/completed", "");
    Check(result.error == PcmTtsPlayer::Error::kSuccess, "success/error",
          PcmTtsPlayer::GetErrorName(result.error));
    Check(result.callbacks == 1, "success/callbacks", std::to_string(result.callbacks));
    Check(result.played_samples == kTotalSamples, "success/played_all_samples",
          std::to_string(result.played_samples));
    Check(frame_sizes == "1440,1440,1440,1440,1440,800", "success/tail_frame_flushed",
          frame_sizes);
    Check(requests.token_requests == 1, "success/one_token_request",
          std::to_string(requests.token_requests));
    Check(requests.tts_requests == 1, "success/one_tts_request",
          std::to_string(requests.tts_requests));
    Check(requests.tts_url_ok, "success/tts_url_uses_access_token_query", "");
    Check(requests.tts_body_ok, "success/tts_body_is_verified_json", "");
    Check(requests.token_body_ok, "success/token_body_is_form", "");
    Check(requests.token_url_with_query == 0, "success/token_url_has_no_query",
          std::to_string(requests.token_url_with_query));
    Check(requests.secrets_in_url == 0, "success/no_secret_in_url",
          std::to_string(requests.secrets_in_url));
    Check(requests.headers_ok, "success/content_types", "");
    Check(requests.active_at_open == 0, "success/request_before_playback",
          std::to_string(requests.active_at_open));
    Check(requests.closes >= 1, "success/transport_closed",
          std::to_string(requests.closes));
    Check(!rig.player.IsBusy(), "success/idle_after", "");
    Check(!rig.service.IsPcmPlaybackActive(), "success/audio_session_closed", "");
    Finish("success", before);
}

// tts_voice overrides the default speaker id in the request body.
void RunVoiceOverride() {
    const int before = g_failures;
    const std::vector<uint8_t> pcm = MakePcm();
    std::printf("SCENARIO voice_override\n");
    Rig rig(Ms(2), Credentials::kPresent);
    rig.script->voice = "1001";
    rig.script->Push(MakeToken("token-abc", 2592000));
    rig.script->Push(MakeAudio(pcm));
    const SessionResult result = RunSession(rig, Ms(15000));
    const RequestSummary requests = Summarize(*rig.script);
    Report("voice_override", result, requests, rig);
    Check(result.error == PcmTtsPlayer::Error::kSuccess, "voice/error",
          PcmTtsPlayer::GetErrorName(result.error));
    Check(requests.tts_body_ok, "voice/body_carries_configured_voice", "");
    Check(result.played_samples == kTotalSamples, "voice/played_all_samples",
          std::to_string(result.played_samples));
    Finish("voice_override", before);
}

// The access token is cached until expires_in: the second utterance reuses it.
void RunTokenCached() {
    const int before = g_failures;
    const std::vector<uint8_t> pcm = MakePcm();
    std::printf("SCENARIO token_cached\n");
    Rig rig(Ms(2), Credentials::kPresent);
    rig.script->Push(MakeToken("token-1", 2592000));
    rig.script->Push(MakeAudio(pcm));
    const SessionResult first = RunSession(rig, Ms(15000));
    rig.script->Push(MakeAudio(pcm));
    const SessionResult second = RunSession(rig, Ms(15000));
    const RequestSummary requests = Summarize(*rig.script);
    Report("token_cached", second, requests, rig);
    std::printf("OBS token_cached.first_error=%s\n",
                PcmTtsPlayer::GetErrorName(first.error));
    Check(first.error == PcmTtsPlayer::Error::kSuccess, "token_cached/first_error",
          PcmTtsPlayer::GetErrorName(first.error));
    Check(second.error == PcmTtsPlayer::Error::kSuccess, "token_cached/second_error",
          PcmTtsPlayer::GetErrorName(second.error));
    Check(requests.token_requests == 1, "token_cached/token_reused",
          std::to_string(requests.token_requests));
    Check(requests.tts_requests == 2, "token_cached/two_tts_requests",
          std::to_string(requests.tts_requests));
    Check(requests.closes >= 2, "token_cached/each_transport_closed",
          std::to_string(requests.closes));
    Check(second.played_samples == kTotalSamples, "token_cached/second_played_all",
          std::to_string(second.played_samples));
    Finish("token_cached", before);
}

// expires_in without a usable margin is not cached: the next utterance asks for a new token.
void RunTokenExpired() {
    const int before = g_failures;
    const std::vector<uint8_t> pcm = MakePcm();
    std::printf("SCENARIO token_expired\n");
    Rig rig(Ms(2), Credentials::kPresent);
    rig.script->Push(MakeToken("token-1", 0));
    rig.script->Push(MakeAudio(pcm));
    const SessionResult first = RunSession(rig, Ms(15000));
    rig.script->Push(MakeToken("token-2", 0));
    rig.script->Push(MakeAudio(pcm));
    const SessionResult second = RunSession(rig, Ms(15000));
    const RequestSummary requests = Summarize(*rig.script);
    Report("token_expired", second, requests, rig);
    std::printf("OBS token_expired.first_error=%s\n",
                PcmTtsPlayer::GetErrorName(first.error));
    Check(first.error == PcmTtsPlayer::Error::kSuccess, "token_expired/first_error",
          PcmTtsPlayer::GetErrorName(first.error));
    Check(second.error == PcmTtsPlayer::Error::kSuccess, "token_expired/second_error",
          PcmTtsPlayer::GetErrorName(second.error));
    Check(requests.token_requests == 2, "token_expired/token_requested_again",
          std::to_string(requests.token_requests));
    Check(requests.tts_requests == 2, "token_expired/two_tts_requests",
          std::to_string(requests.tts_requests));
    Finish("token_expired", before);
}

/**
 * Runs one session that is expected to fail before any audio is played and asserts the shared
 * pass/fail conditions of that class of failure. |script| queues the scripted replies and
 * |extra| adds scenario-specific assertions.
 */
void RunFailureScenario(const char* name, const char* expected_error,
                        const std::function<void(Rig&)>& script,
                        const std::function<void(const RequestSummary&)>& extra) {
    const int before = g_failures;
    std::printf("SCENARIO %s\n", name);
    Rig rig(Ms(2), Credentials::kPresent);
    script(rig);
    const SessionResult result = RunSession(rig, Ms(15000));
    const RequestSummary requests = Summarize(*rig.script);
    Report(name, result, requests, rig);
    const std::string prefix(name);
    Check(result.completed, prefix + "/completed", "");
    Check(std::string(PcmTtsPlayer::GetErrorName(result.error)) == expected_error,
          prefix + "/error", PcmTtsPlayer::GetErrorName(result.error));
    Check(result.frames == 0, prefix + "/nothing_played", std::to_string(result.frames));
    Check(!rig.service.IsPcmPlaybackActive(), prefix + "/no_audio_session", "");
    Check(!rig.player.IsBusy(), prefix + "/idle_after", "");
    Check(requests.active_at_open == 0, prefix + "/request_before_playback",
          std::to_string(requests.active_at_open));
    if (extra) {
        extra(requests);
    }
    Finish(name, before);
}

// A rejected token request (HTTP level) never reaches the speech endpoint.
void RunTokenHttpError() {
    RunFailureScenario(
        "token_http_error", "http-failed",
        [](Rig& rig) {
            rig.script->Push(MakeJsonStatus(400, "{\"error\":\"invalid_client\"}"));
        },
        [](const RequestSummary& requests) {
            Check(requests.token_requests == 1, "token_http_error/one_token_request",
                  std::to_string(requests.token_requests));
            Check(requests.tts_requests == 0, "token_http_error/no_tts_request",
                  std::to_string(requests.tts_requests));
            Check(requests.token_body_ok, "token_http_error/token_body_is_form", "");
        });
}

// A 200 that carries an OAuth error object instead of a token.
void RunTokenJsonError() {
    RunFailureScenario(
        "token_json_error", "baidu-json-error",
        [](Rig& rig) {
            rig.script->Push(MakeJsonStatus(
                200, "{\"error\":\"invalid_client\",\"error_description\":\"unknown client id\"}"));
        },
        [](const RequestSummary& requests) {
            Check(requests.token_requests == 1, "token_json_error/one_token_request",
                  std::to_string(requests.token_requests));
            Check(requests.tts_requests == 0, "token_json_error/no_tts_request",
                  std::to_string(requests.tts_requests));
        });
}

// A gateway failure without a JSON body is a transport failure, not an API rejection.
void RunTtsHttpError() {
    RunFailureScenario(
        "tts_http_error", "http-failed",
        [](Rig& rig) {
            rig.script->Push(MakeToken("token-abc", 2592000));
            ScriptedResponse response;
            response.status = 503;
            response.content_type = "text/html";
            response.body = "<html><body>503 Service Unavailable</body></html>";
            rig.script->Push(response);
        },
        [](const RequestSummary& requests) {
            Check(requests.token_requests == 1, "tts_http_error/one_token_request",
                  std::to_string(requests.token_requests));
            Check(requests.tts_requests == 1, "tts_http_error/one_tts_request",
                  std::to_string(requests.tts_requests));
        });
}

// A non-auth API error is reported without a second attempt: only auth errors are retried.
void RunTtsJsonError() {
    RunFailureScenario(
        "tts_json_error", "baidu-json-error",
        [](Rig& rig) {
            rig.script->Push(MakeToken("token-abc", 2592000));
            rig.script->Push(MakeJsonStatus(
                200, "{\"err_no\":5000,\"err_msg\":\"unknown error\",\"error_code\":5000}"));
        },
        [](const RequestSummary& requests) {
            std::printf("OBS tts_json_error.no_retry=%d\n",
                        requests.tts_requests == 1 ? 1 : 0);
            Check(requests.tts_requests == 1, "tts_json_error/no_retry",
                  std::to_string(requests.tts_requests));
            Check(requests.token_requests == 1, "tts_json_error/no_token_refresh",
                  std::to_string(requests.token_requests));
        });
}

// The response announces no audio format, so it cannot be fed to the codec unverified.
void RunMissingContentType() {
    RunFailureScenario(
        "missing_content_type", "audio-format-error",
        [](Rig& rig) {
            const std::vector<uint8_t> pcm = MakePcm();
            rig.script->Push(MakeToken("token-abc", 2592000));
            ScriptedResponse response;
            response.status = 200;
            response.body.assign(pcm.begin(), pcm.end());
            rig.script->Push(response);
        },
        [](const RequestSummary& requests) {
            Check(requests.tts_requests == 1, "missing_content_type/one_tts_request",
                  std::to_string(requests.tts_requests));
        });
}

// 16 kHz audio would play at the wrong pitch: the format check rejects it before playback.
void RunWrongSampleRate() {
    RunFailureScenario(
        "wrong_rate", "audio-format-error",
        [](Rig& rig) {
            const std::vector<uint8_t> pcm = MakePcm();
            rig.script->Push(MakeToken("token-abc", 2592000));
            ScriptedResponse response = MakeAudio(pcm);
            response.content_type = "audio/basic;codec=pcm;rate=16000;channel=1";
            rig.script->Push(response);
        },
        [](const RequestSummary& requests) {
            Check(requests.tts_requests == 1, "wrong_rate/one_tts_request",
                  std::to_string(requests.tts_requests));
        });
}

// Stereo would be interleaved into a mono codec: rejected as well.
void RunWrongChannels() {
    RunFailureScenario(
        "wrong_channel", "audio-format-error",
        [](Rig& rig) {
            const std::vector<uint8_t> pcm = MakePcm();
            rig.script->Push(MakeToken("token-abc", 2592000));
            ScriptedResponse response = MakeAudio(pcm);
            response.content_type = "audio/basic;codec=pcm;rate=24000;channel=2";
            rig.script->Push(response);
        },
        [](const RequestSummary& requests) {
            Check(requests.tts_requests == 1, "wrong_channel/one_tts_request",
                  std::to_string(requests.tts_requests));
        });
}

// No network: the transport cannot even be opened.
void RunOpenFailure() {
    RunFailureScenario(
        "open_failure", "http-failed",
        [](Rig& rig) {
            ScriptedResponse response;
            response.fail_open = true;
            rig.script->Push(response);
        },
        [](const RequestSummary& requests) {
            Check(requests.opens == 1, "open_failure/one_attempt",
                  std::to_string(requests.opens));
            Check(requests.tts_requests == 0, "open_failure/no_tts_request",
                  std::to_string(requests.tts_requests));
        });
}

// A revoked cached token (100020) is refreshed once and the utterance then plays.
void RunAuthRetryOnce() {
    const int before = g_failures;
    const std::vector<uint8_t> pcm = MakePcm();
    std::printf("SCENARIO auth_retry\n");
    Rig rig(Ms(2), Credentials::kPresent);
    rig.script->Push(MakeToken("stale-token", 2592000));
    rig.script->Push(MakeJsonStatus(
        200, "{\"error_code\":100020,\"err_msg\":\"invalid access token\"}"));
    rig.script->Push(MakeToken("fresh-token", 2592000));
    rig.script->Push(MakeAudio(pcm));
    const SessionResult result = RunSession(rig, Ms(15000));
    const RequestSummary requests = Summarize(*rig.script);
    Report("auth_retry", result, requests, rig);
    Check(result.completed, "auth_retry/completed", "");
    Check(result.error == PcmTtsPlayer::Error::kSuccess, "auth_retry/error",
          PcmTtsPlayer::GetErrorName(result.error));
    Check(requests.token_requests == 2, "auth_retry/token_refreshed_once",
          std::to_string(requests.token_requests));
    Check(requests.tts_requests == 2, "auth_retry/one_retry",
          std::to_string(requests.tts_requests));
    Check(result.played_samples == kTotalSamples, "auth_retry/played_all_samples",
          std::to_string(result.played_samples));
    Finish("auth_retry", before);
}

// A rejected refreshed token is reported instead of retried forever.
void RunAuthRetryExhausted() {
    RunFailureScenario(
        "auth_retry_exhausted", "baidu-json-error",
        [](Rig& rig) {
            rig.script->Push(MakeToken("stale-token", 2592000));
            rig.script->Push(MakeJsonStatus(
                200, "{\"error_code\":100020,\"err_msg\":\"invalid access token\"}"));
            rig.script->Push(MakeToken("fresh-token", 2592000));
            rig.script->Push(MakeJsonStatus(
                200, "{\"error_code\":100020,\"err_msg\":\"invalid access token\"}"));
        },
        [](const RequestSummary& requests) {
            Check(requests.tts_requests == 2, "auth_retry_exhausted/exactly_one_retry",
                  std::to_string(requests.tts_requests));
            Check(requests.token_requests == 2, "auth_retry_exhausted/two_token_requests",
                  std::to_string(requests.token_requests));
        });
}

// The socket dies mid-body: what already streamed plays, then kHttpFailed, then a clean close.
void RunBodyReadError() {
    const int before = g_failures;
    const std::vector<uint8_t> pcm = MakePcm();
    std::printf("SCENARIO body_error\n");
    Rig rig(Ms(2), Credentials::kPresent);
    rig.script->Push(MakeToken("token-abc", 2592000));
    ScriptedResponse audio = MakeAudio(pcm);
    audio.fail_read_at = 3;  // three good chunks (~4.5 KB) then a broken connection
    rig.script->Push(audio);
    const SessionResult result = RunSession(rig, Ms(15000));
    const RequestSummary requests = Summarize(*rig.script);
    Report("body_error", result, requests, rig);
    Check(result.completed, "body_error/completed", "");
    Check(result.error == PcmTtsPlayer::Error::kHttpFailed, "body_error/error",
          PcmTtsPlayer::GetErrorName(result.error));
    Check(result.played_samples > 0, "body_error/partial_audio_played",
          std::to_string(result.played_samples));
    Check(result.played_samples < kTotalSamples, "body_error/audio_truncated",
          std::to_string(result.played_samples));
    Check(requests.closes >= 2, "body_error/transport_released",
          std::to_string(requests.closes));
    Check(!rig.player.IsBusy(), "body_error/idle_after", "");
    Check(!rig.service.IsPcmPlaybackActive(), "body_error/audio_session_closed", "");
    Finish("body_error", before);
}

// A cancel while the body is still streaming: the request and the session stop promptly, the
// half-streamed audio is dropped, and exactly one cancelled completion is reported.
void RunCancelMidStream() {
    const int before = g_failures;
    const std::vector<uint8_t> pcm = MakePcm();
    std::printf("SCENARIO cancel_mid_stream\n");
    Rig rig(Ms(2), Credentials::kPresent);
    rig.script->Push(MakeToken("token-abc", 2592000));
    ScriptedResponse audio = MakeAudio(pcm);
    audio.chunk_bytes = 720;  // 15 ms of audio per read, slower than the 2 ms playback write
    audio.pace_ms = 20;
    rig.script->Push(audio);

    auto done = std::make_shared<Completion>();
    const uint32_t session_id = rig.player.Start(
        kSampleRate, kChannels,
        [&rig](std::vector<uint8_t>& chunk) { return rig.source.Read(chunk); },
        [done](uint32_t id, PcmTtsPlayer::Error error) { done->OnFinished(id, error); },
        [&rig]() { return rig.source.Open(kText); },
        [&rig]() { rig.source.Close(); });

    // Cancel the moment audio is flowing, the way Application::StopPcmTtsSession() does.
    const auto active_start = Clock::now();
    bool active = false;
    while (ElapsedMs(active_start) < 5000) {
        if (rig.service.IsPcmPlaybackActive() && rig.codec.frame_count() >= 1) {
            active = true;
            break;
        }
        std::this_thread::sleep_for(Ms(2));
    }
    const auto cancel_start = Clock::now();
    rig.source.Cancel();
    rig.player.Stop();
    const bool completed = done->Wait(Ms(5000));
    const int64_t cancel_ms = ElapsedMs(cancel_start);
    const RequestSummary requests = Summarize(*rig.script);
    SessionResult result;
    result.session_id = session_id;
    result.completed = completed;
    result.error = done->error();
    result.callbacks = done->count();
    result.frames = rig.codec.frame_count();
    result.played_samples = rig.codec.sample_count();
    result.elapsed_ms = cancel_ms;
    Report("cancel_mid_stream", result, requests, rig);
    std::printf("OBS cancel_mid_stream.session_id=%u\n", static_cast<unsigned>(session_id));
    std::printf("OBS cancel_mid_stream.cancel_ms=%lld\n", static_cast<long long>(cancel_ms));
    std::fflush(stdout);

    Check(session_id != 0, "cancel_mid_stream/start_returns_id",
          std::to_string(session_id));
    Check(active, "cancel_mid_stream/audio_started", "");
    Check(completed, "cancel_mid_stream/completed", "");
    Check(done->error() == PcmTtsPlayer::Error::kCancelled, "cancel_mid_stream/error_cancelled",
          PcmTtsPlayer::GetErrorName(done->error()));
    Check(done->count() == 1, "cancel_mid_stream/single_callback",
          std::to_string(done->count()));
    Check(cancel_ms < 1500, "cancel_mid_stream/prompt",
          "cancel_ms=" + std::to_string(cancel_ms));
    Check(rig.codec.sample_count() < kTotalSamples, "cancel_mid_stream/audio_truncated",
          "played=" + std::to_string(rig.codec.sample_count()));
    Check(requests.closes >= 2, "cancel_mid_stream/transport_released",
          std::to_string(requests.closes));
    Check(requests.secrets_in_url == 0, "cancel_mid_stream/no_secret_in_url",
          std::to_string(requests.secrets_in_url));
    Check(!rig.player.IsBusy(), "cancel_mid_stream/idle_after", "");
    Check(!rig.service.IsPcmPlaybackActive(), "cancel_mid_stream/audio_session_closed", "");
    Finish("cancel_mid_stream", before);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        std::printf("usage: %s <scenario>\n", argv[0]);
        return 2;
    }
    const std::string scenario = argv[1];
    if (scenario == "success") {
        RunSuccess();
    } else if (scenario == "voice_override") {
        RunVoiceOverride();
    } else if (scenario == "missing_credentials") {
        RunMissingCredentials();
    } else if (scenario == "token_cached") {
        RunTokenCached();
    } else if (scenario == "token_expired") {
        RunTokenExpired();
    } else if (scenario == "token_http_error") {
        RunTokenHttpError();
    } else if (scenario == "token_json_error") {
        RunTokenJsonError();
    } else if (scenario == "tts_http_error") {
        RunTtsHttpError();
    } else if (scenario == "tts_json_error") {
        RunTtsJsonError();
    } else if (scenario == "missing_content_type") {
        RunMissingContentType();
    } else if (scenario == "wrong_rate") {
        RunWrongSampleRate();
    } else if (scenario == "wrong_channel") {
        RunWrongChannels();
    } else if (scenario == "open_failure") {
        RunOpenFailure();
    } else if (scenario == "auth_retry") {
        RunAuthRetryOnce();
    } else if (scenario == "auth_retry_exhausted") {
        RunAuthRetryExhausted();
    } else if (scenario == "body_error") {
        RunBodyReadError();
    } else if (scenario == "cancel_mid_stream") {
        RunCancelMidStream();
    } else {
        std::printf("unknown scenario: %s\n", scenario.c_str());
        return 2;
    }
    return g_failures == 0 ? 0 : 1;
}
'''

ML307_INCLUDE = ROOT / "managed_components" / "78__esp-ml307" / "include"

# The shared stubs plus the one header the ml307 transport needs on the host.
EXTRA_STUBS = {
    "esp_err.h": r'''
        #pragma once
        // network_error.h only needs the typedef on the host: no ESP-IDF call is reached.
        typedef int esp_err_t;
        #define ESP_OK 0
        #define ESP_FAIL (-1)
    ''',
}

# managed_components/.../network_error.cc is not part of the host build; only Name() is reached
# from the source's transport-failure log line.
NETWORK_ERROR_HOST = r'''
#include "network_error.h"

const char* NetworkError::Name() const {
    switch (code) {
        case NetworkErrc::InvalidArgument:
            return "invalid-argument";
        case NetworkErrc::NotInitialized:
            return "not-initialized";
        case NetworkErrc::Timeout:
            return "timeout";
        case NetworkErrc::DnsFailed:
            return "dns-failed";
        case NetworkErrc::ConnectFailed:
            return "connect-failed";
        case NetworkErrc::TlsFailed:
            return "tls-failed";
        case NetworkErrc::ProtocolError:
            return "protocol-error";
        case NetworkErrc::AuthRejected:
            return "auth-rejected";
        case NetworkErrc::ServerDisconnected:
            return "server-disconnected";
        case NetworkErrc::NetworkUnavailable:
            return "network-unavailable";
        case NetworkErrc::TransmitFailed:
            return "transmit-failed";
        case NetworkErrc::ReceiveFailed:
            return "receive-failed";
        case NetworkErrc::AtCommandFailed:
            return "at-command-failed";
        case NetworkErrc::CmeError:
            return "cme-error";
        case NetworkErrc::HttpFailed:
            return "http-failed";
        case NetworkErrc::Unknown:
            return "unknown";
    }
    return "unknown";
}
'''

DRIVER_SOURCES = (
    DRIVER_HEAD,
    DRIVER_HTTP,
    DRIVER_ASSERTS,
    DRIVER_RIG,
)

# Verbatim copies of the files under test; quote-includes resolve the sibling stubs and the
# reminder/player sources from the work directory.
AUDIO_SOURCES = ("audio_service.cc", "audio_service.h", "audio_engine.h")
REMINDER_SOURCES = ("baidu_tts_source.cc", "baidu_tts_source.h", "pcm_tts_player.cc",
                    "pcm_tts_player.h")


def _tool(env_name, default):
    parts = shlex.split(os.environ.get(env_name, default))
    if not parts or shutil.which(parts[0]) is None:
        return None
    return parts


def _require_tool(env_name, default, language):
    tool = _tool(env_name, default)
    if tool is None:
        raise unittest.SkipTest(
            f"host {language} compiler not found: {default} "
            f"(set {env_name}=/path/to/{default} to run these tests)"
        )
    return tool


def _write_stubs(workdir):
    for name, content in {**STUBS, **EXTRA_STUBS}.items():
        path = workdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def _copy_sources(workdir):
    sources = []
    for directory, names in (("audio", AUDIO_SOURCES), ("reminder", REMINDER_SOURCES)):
        for name in names:
            target = workdir / name
            shutil.copyfile(ROOT / "main" / directory / name, target)
            if name.endswith(".cc"):
                sources.append(str(target))
    return sources


def _compile_cjson(cc, workdir):
    obj = workdir / "cJSON.o"
    command = cc + ["-O1", "-c", str(CJSON_DIR / "cJSON.c"), f"-I{CJSON_DIR}", "-o", str(obj)]
    compiled = subprocess.run(command, capture_output=True, text=True)
    if compiled.returncode != 0:
        raise AssertionError("failed to compile cJSON for the host test:\n" + compiled.stderr)
    return str(obj)


def build_driver(workdir):
    """Compiles the driver against the real Baidu source, PcmTtsPlayer and AudioService."""
    cxx = _require_tool("CXX", "c++", "C++")
    cc = _require_tool("CC", "cc", "C")

    _write_stubs(workdir)
    driver = workdir / "baidu_tts_source_driver.cc"
    driver.write_text(
        textwrap.dedent("\n".join(DRIVER_SOURCES)).lstrip("\n"), encoding="utf-8"
    )
    network_error = workdir / "host_network_error.cc"
    network_error.write_text(
        textwrap.dedent(NETWORK_ERROR_HOST).lstrip("\n"), encoding="utf-8"
    )
    object_path = _compile_cjson(cc, workdir)

    exe = workdir / ("baidu_tts_source" + (".exe" if os.name == "nt" else ""))
    command = cxx + [
        "-std=gnu++23",
        "-O1",
        "-g",
        "-pthread",
        f"-I{workdir}",
        f"-I{ROOT / 'main'}",
        f"-I{ROOT / 'main' / 'audio'}",
        f"-I{ROOT / 'main' / 'reminder'}",
        f"-I{ML307_INCLUDE}",
        f"-I{CJSON_DIR}",
        str(driver),
        str(network_error),
    ] + _copy_sources(workdir) + [
        object_path,
        "-o",
        str(exe),
    ]
    compiled = subprocess.run(command, capture_output=True, text=True)
    if compiled.returncode != 0:
        raise AssertionError("failed to build the Baidu TTS host test:\n" + compiled.stderr)
    return exe


class BaiduTtsSourceTest(unittest.TestCase):
    """Drives the real Baidu source through the real player and AudioService."""

    @classmethod
    def setUpClass(cls):
        cls._tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tempdir.cleanup)
        cls.exe_path = build_driver(Path(cls._tempdir.name))

    def _run(self, scenario):
        completed = subprocess.run(
            [str(self.exe_path), scenario], capture_output=True, text=True, timeout=180
        )
        output = completed.stdout + completed.stderr
        if os.environ.get("BAIDU_TTS_SOURCE_VERBOSE"):
            print(output)
        failures = [line for line in output.splitlines() if line.startswith("FAIL ")]
        self.assertEqual(0, completed.returncode, msg=output)
        self.assertIn(f"SCENARIO_DONE {scenario} ok", output, msg=output)
        self.assertEqual([], failures, msg=output)
        return output

    @staticmethod
    def _obs(output, key):
        prefix = f"OBS {key}="
        for line in output.splitlines():
            if line.startswith(prefix):
                return line[len(prefix) :].strip()
        raise AssertionError(f"missing {prefix} in output:\n{output}")

    def _assert_failure_scenario(self, scenario, expected_error, token_requests, tts_requests):
        output = self._run(scenario)
        self.assertEqual(expected_error, self._obs(output, f"{scenario}.error"))
        self.assertEqual(str(token_requests), self._obs(output, f"{scenario}.token_requests"))
        self.assertEqual(str(tts_requests), self._obs(output, f"{scenario}.tts_requests"))
        self.assertEqual("0", self._obs(output, f"{scenario}.frames"))
        self.assertEqual("0", self._obs(output, f"{scenario}.played_samples"))
        self.assertEqual("0", self._obs(output, f"{scenario}.active_at_open"))
        self.assertEqual("1", self._obs(output, f"{scenario}.player_idle"))
        self.assertEqual("0", self._obs(output, f"{scenario}.pcm_active"))
        return output

    def test_success_streams_verified_request_and_plays_every_sample(self):
        output = self._run("success")
        self.assertEqual("success", self._obs(output, "success.error"))
        self.assertEqual("1", self._obs(output, "success.callbacks"))
        self.assertEqual("8000", self._obs(output, "success.played_samples"))
        self.assertEqual(
            "1440,1440,1440,1440,1440,800", self._obs(output, "success.frame_sizes")
        )
        self.assertEqual("1", self._obs(output, "success.token_requests"))
        self.assertEqual("1", self._obs(output, "success.tts_requests"))
        # Token in the query, AK/SK only in the form body, verified JSON speech request.
        self.assertEqual("1", self._obs(output, "success.tts_url_ok"))
        self.assertEqual("1", self._obs(output, "success.tts_body_ok"))
        self.assertEqual("1", self._obs(output, "success.token_body_ok"))
        self.assertEqual("0", self._obs(output, "success.token_url_with_query"))
        self.assertEqual("0", self._obs(output, "success.secrets_in_url"))
        self.assertEqual("1", self._obs(output, "success.headers_ok"))
        # Both requests are issued before the playback path is opened.
        self.assertEqual("0", self._obs(output, "success.active_at_open"))
        self.assertEqual("2", self._obs(output, "success.closes"))
        self.assertEqual("1", self._obs(output, "success.player_idle"))
        self.assertEqual("0", self._obs(output, "success.pcm_active"))

    def test_voice_override_reaches_the_request_body(self):
        output = self._run("voice_override")
        self.assertEqual("success", self._obs(output, "voice_override.error"))
        self.assertEqual("1", self._obs(output, "voice_override.tts_body_ok"))
        self.assertEqual("8000", self._obs(output, "voice_override.played_samples"))

    def test_missing_credentials_issue_no_request(self):
        output = self._run("missing_credentials")
        self.assertEqual("api-key-missing", self._obs(output, "missing_credentials.error"))
        self.assertEqual("0", self._obs(output, "missing_credentials.opens"))
        self.assertEqual("0", self._obs(output, "missing_credentials.frames"))

    def test_token_is_reused_across_utterances(self):
        output = self._run("token_cached")
        self.assertEqual("success", self._obs(output, "token_cached.first_error"))
        self.assertEqual("success", self._obs(output, "token_cached.error"))
        self.assertEqual("1", self._obs(output, "token_cached.token_requests"))
        self.assertEqual("2", self._obs(output, "token_cached.tts_requests"))
        self.assertEqual("8000", self._obs(output, "token_cached.played_samples"))

    def test_expiring_token_is_requested_again(self):
        output = self._run("token_expired")
        self.assertEqual("success", self._obs(output, "token_expired.first_error"))
        self.assertEqual("success", self._obs(output, "token_expired.error"))
        self.assertEqual("2", self._obs(output, "token_expired.token_requests"))
        self.assertEqual("2", self._obs(output, "token_expired.tts_requests"))

    def test_token_http_error_stops_before_the_speech_endpoint(self):
        self._assert_failure_scenario("token_http_error", "http-failed", 1, 0)

    def test_token_json_error_is_reported_as_a_json_failure(self):
        self._assert_failure_scenario("token_json_error", "baidu-json-error", 1, 0)

    def test_gateway_failure_is_a_transport_error(self):
        self._assert_failure_scenario("tts_http_error", "http-failed", 1, 1)

    def test_api_rejection_json_is_never_played(self):
        output = self._assert_failure_scenario("tts_json_error", "baidu-json-error", 1, 1)
        self.assertEqual("1", self._obs(output, "tts_json_error.no_retry"))

    def test_missing_content_type_is_rejected_before_playback(self):
        self._assert_failure_scenario(
            "missing_content_type", "audio-format-error", 1, 1
        )

    def test_wrong_sample_rate_is_rejected_before_playback(self):
        self._assert_failure_scenario("wrong_rate", "audio-format-error", 1, 1)

    def test_wrong_channel_count_is_rejected_before_playback(self):
        self._assert_failure_scenario("wrong_channel", "audio-format-error", 1, 1)

    def test_transport_open_failure(self):
        self._assert_failure_scenario("open_failure", "http-failed", 1, 0)

    def test_revoked_token_is_refreshed_once_then_plays(self):
        output = self._run("auth_retry")
        self.assertEqual("success", self._obs(output, "auth_retry.error"))
        self.assertEqual("2", self._obs(output, "auth_retry.token_requests"))
        self.assertEqual("2", self._obs(output, "auth_retry.tts_requests"))
        self.assertEqual("8000", self._obs(output, "auth_retry.played_samples"))

    def test_rejected_token_is_not_retried_forever(self):
        self._assert_failure_scenario(
            "auth_retry_exhausted", "baidu-json-error", 2, 2
        )

    def test_body_failure_keeps_the_partial_audio_then_reports_http_failed(self):
        output = self._run("body_error")
        self.assertEqual("http-failed", self._obs(output, "body_error.error"))
        played = int(self._obs(output, "body_error.played_samples"))
        self.assertGreater(played, 0)
        self.assertLess(played, 8000)
        self.assertGreaterEqual(int(self._obs(output, "body_error.closes")), 2)
        self.assertEqual("1", self._obs(output, "body_error.player_idle"))
        self.assertEqual("0", self._obs(output, "body_error.pcm_active"))

    def test_cancel_mid_stream_stops_promptly(self):
        output = self._run("cancel_mid_stream")
        self.assertNotEqual("0", self._obs(output, "cancel_mid_stream.session_id"))
        self.assertEqual("cancelled", self._obs(output, "cancel_mid_stream.error"))
        self.assertEqual("1", self._obs(output, "cancel_mid_stream.callbacks"))
        self.assertLess(int(self._obs(output, "cancel_mid_stream.cancel_ms")), 1500)
        played = int(self._obs(output, "cancel_mid_stream.played_samples"))
        self.assertLess(played, 8000)
        self.assertGreaterEqual(int(self._obs(output, "cancel_mid_stream.closes")), 2)
        self.assertEqual("1", self._obs(output, "cancel_mid_stream.player_idle"))
        self.assertEqual("0", self._obs(output, "cancel_mid_stream.pcm_active"))


if __name__ == "__main__":
    unittest.main()
