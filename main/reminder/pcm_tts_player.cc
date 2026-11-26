#include "pcm_tts_player.h"

#include <algorithm>

#include <esp_log.h>

namespace {
// The next stage feeds this worker with blocking TLS reads, so keep the stack generous.
constexpr uint32_t kPcmTtsTaskStackSize = 6144;
constexpr UBaseType_t kPcmTtsTaskPriority = 2;
// The worker owns the only EndPcmPlayback() call; AudioService's default drain timeout applies.
constexpr uint32_t kPcmDrainTimeoutMs = PCM_PLAYBACK_DRAIN_TIMEOUT_MS;
constexpr size_t kMaxReadChunkBytes = 4096;
const char* TAG = "PcmTtsPlayer";
}  // namespace

PcmTtsPlayer::PcmTtsPlayer(AudioService& audio_service) : audio_service_(audio_service) {}

PcmTtsPlayer::~PcmTtsPlayer() { Stop(); }

uint32_t PcmTtsPlayer::Start(int sample_rate, int channels, ChunkReader reader,
                             FinishedCallback finished_callback, PrepareCallback prepare,
                             CloseCallback close) {
    if (!reader || !finished_callback) {
        ESP_LOGE(TAG, "PCM TTS start rejected: reader or finished callback is missing");
        return 0;
    }

    uint32_t session_id = 0;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (state_ != State::kIdle) {
            ESP_LOGW(TAG, "PCM TTS start rejected: player is %s", GetStateName(state_));
            return 0;
        }
        session_id_ = ++next_session_id_;
        if (session_id_ == 0) {
            session_id_ = ++next_session_id_;  // 0 is reserved for "no session"
        }
        session_id = session_id_;
        sample_rate_ = sample_rate;
        channels_ = channels;
        reader_ = std::move(reader);
        finished_callback_ = std::move(finished_callback);
        prepare_ = std::move(prepare);
        close_callback_ = std::move(close);
        pending_abort_ = false;
        cancelled_ = false;
        session_started_ = false;
        abort_issued_ = false;
        state_ = State::kBeginning;
    }

    if (xTaskCreate(WorkerEntry, "pcm_tts", kPcmTtsTaskStackSize, this, kPcmTtsTaskPriority,
                    nullptr) != pdPASS) {
        ESP_LOGE(TAG, "Failed to create the PCM TTS task");
        std::lock_guard<std::mutex> lock(mutex_);
        state_ = State::kIdle;
        session_id_ = 0;
        reader_ = nullptr;
        finished_callback_ = nullptr;
        prepare_ = nullptr;
        close_callback_ = nullptr;
        return 0;
    }

    ESP_LOGI(TAG, "PCM TTS session %lu started: %d Hz / %d channel",
             static_cast<unsigned long>(session_id), sample_rate, channels);
    return session_id;
}

void PcmTtsPlayer::Stop() {
    bool abort_now = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (state_ == State::kIdle) {
            return;
        }
        cancelled_ = true;
        if (state_ == State::kBeginning) {
            // BeginPcmPlayback() may still be running in the worker: calling Abort here would run
            // the two control-plane calls concurrently (§6.3). Record the request instead; the
            // worker aborts immediately after BeginPcmPlayback() returns.
            if (!pending_abort_) {
                pending_abort_ = true;
                ESP_LOGI(TAG, "PCM TTS cancel requested while beginning");
            }
            return;
        }
        if (abort_issued_) {
            return;  // already cancelled: never issue a second AbortPcmPlayback()
        }
        abort_issued_ = true;
        abort_now = true;
    }
    if (abort_now) {
        // Outside the state lock: AbortPcmPlayback() takes pcm_mutex_ and wakes a drain wait that
        // the worker may be blocked in, which then reports kCancelled (§9.1 path ②).
        ESP_LOGI(TAG, "Aborting PCM TTS playback");
        audio_service_.AbortPcmPlayback();
    }
}

bool PcmTtsPlayer::IsBusy() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_ != State::kIdle;
}

bool PcmTtsPlayer::IsActive() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_ == State::kActive;
}

PcmTtsPlayer::State PcmTtsPlayer::GetState() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return state_;
}

uint32_t PcmTtsPlayer::session_id() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return session_id_;
}

bool PcmTtsPlayer::IsCancelled(uint32_t session_id) const {
    std::lock_guard<std::mutex> lock(mutex_);
    return cancelled_ || session_id == 0 || session_id != session_id_;
}

void PcmTtsPlayer::WorkerEntry(void* arg) {
    auto* player = static_cast<PcmTtsPlayer*>(arg);
    uint32_t session_id = 0;
    {
        std::lock_guard<std::mutex> lock(player->mutex_);
        session_id = player->session_id_;
    }
    player->WorkerTask(session_id);
    vTaskDelete(nullptr);
}

void PcmTtsPlayer::WorkerTask(uint32_t session_id) {
    int sample_rate = 0;
    int channels = 0;
    ChunkReader reader;
    PrepareCallback prepare;
    bool cancelled_before_begin = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        sample_rate = sample_rate_;
        channels = channels_;
        reader = reader_;
        prepare = prepare_;
        cancelled_before_begin = cancelled_ || session_id != session_id_;
    }
    if (cancelled_before_begin) {
        // Stop() arrived before BeginPcmPlayback(): no session exists yet, so no Abort is needed.
        FinishSession(session_id, Error::kCancelled);
        return;
    }

    if (prepare) {
        // The source validates credentials, the request and the announced audio format before the
        // shared playback path is opened, so a failure here leaves no AudioService session behind.
        const Error prepare_error = prepare();
        if (IsCancelled(session_id)) {
            // A Stop() that landed while the source was blocked (DNS, TLS, a token request) wins
            // over whatever the source reported.
            FinishSession(session_id, Error::kCancelled);
            return;
        }
        if (prepare_error != Error::kSuccess) {
            ESP_LOGE(TAG, "PCM TTS session %lu: source prepare failed (%s)",
                     static_cast<unsigned long>(session_id), GetErrorName(prepare_error));
            FinishSession(session_id, prepare_error);
            return;
        }
    }

    if (!audio_service_.BeginPcmPlayback(sample_rate, channels, 0)) {
        // BeginPcmPlayback() rejects every format but the codec's own 24 kHz mono output and
        // creates no session when it fails (§3.1), so there is nothing to End or Abort.
        ESP_LOGE(TAG, "PCM TTS session %lu rejected by AudioService (%d Hz / %d channel)",
                 static_cast<unsigned long>(session_id), sample_rate, channels);
        FinishSession(session_id, Error::kSampleRateMismatch);
        return;
    }

    bool state_lost = false;
    bool abort_after_begin = false;
    bool cancelled_after_begin = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (session_id != session_id_ || state_ != State::kBeginning) {
            state_lost = true;
        } else {
            session_started_ = true;
            if (pending_abort_ || cancelled_) {
                pending_abort_ = false;
                cancelled_after_begin = true;
                if (!abort_issued_) {
                    abort_issued_ = true;
                    abort_after_begin = true;
                }
            } else {
                state_ = State::kActive;
            }
        }
    }
    if (state_lost) {
        // Unreachable while Start()/Stop() stay serialized on the main task: close the session
        // this worker just opened so the shared playback path is not left writable.
        ESP_LOGE(TAG, "PCM TTS session %lu lost its state while beginning",
                 static_cast<unsigned long>(session_id));
        audio_service_.AbortPcmPlayback();
        FinishSession(session_id, Error::kCancelled);
        return;
    }
    if (abort_after_begin) {
        // Stop() could not issue this while BeginPcmPlayback() was in flight.
        ESP_LOGI(TAG, "Aborting PCM TTS playback after begin");
        audio_service_.AbortPcmPlayback();
    }
    if (cancelled_after_begin) {
        FinishSession(session_id, Error::kCancelled);
        return;
    }

    Error error = Error::kSuccess;
    std::vector<uint8_t> chunk;
    chunk.reserve(kMaxReadChunkBytes);  // one allocation per session, reused for every read

    while (true) {
        if (IsCancelled(session_id)) {
            error = Error::kCancelled;  // Stop() already issued the Abort for kActive
            break;
        }
        chunk.clear();
        const ReadResult read = reader(chunk);
        if (read != ReadResult::kOk) {
            // A cancel that landed while the reader was blocked wins over the reader's own result:
            // a stop must not be reported as a clean end of stream or as a transport failure.
            if (IsCancelled(session_id)) {
                error = Error::kCancelled;
            } else if (read == ReadResult::kHttpFailed) {
                ESP_LOGE(TAG, "PCM TTS session %lu: source failed",
                         static_cast<unsigned long>(session_id));
                error = Error::kHttpFailed;
            }
            break;
        }
        if (chunk.empty()) {
            // Keep a source that returns "no data yet" from spinning the worker flat out.
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }
        if (!audio_service_.WritePcmData(chunk.data(), chunk.size(), false)) {
            // Rejected because the session was cancelled (Abort/Stop/ResetDecoder) or because the
            // playback queue was reset; an empty-block write never reaches this point (§7.2).
            const bool cancelled = IsCancelled(session_id);
            error = cancelled ? Error::kCancelled : Error::kDrainTimeout;
            if (!cancelled) {
                ESP_LOGE(TAG, "PCM TTS session %lu: write rejected by AudioService",
                         static_cast<unsigned long>(session_id));
            }
            break;
        }
    }

    bool abort_needed = false;
    bool end_needed = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (session_id != session_id_) {
            // Superseded session: never touch AudioService state owned by another worker.
            return;
        }
        if (cancelled_ && !abort_issued_) {
            // Only reachable when the cancel arrived between IsCancelled() and this block.
            abort_issued_ = true;
            abort_needed = true;
        }
        if (!abort_issued_ && session_started_) {
            state_ = State::kEnding;
            end_needed = true;
        }
    }
    if (abort_needed) {
        audio_service_.AbortPcmPlayback();
    }
    if (end_needed) {
        // Pushes the tail frame, then waits for the shared playback path to drain. A Stop() issued
        // while we wait aborts and wakes this call, which then returns false (§9.1 path ②).
        const bool drained = audio_service_.EndPcmPlayback(kPcmDrainTimeoutMs);
        if (!drained) {
            ESP_LOGW(TAG, "PCM TTS session %lu: EndPcmPlayback() returned false",
                     static_cast<unsigned long>(session_id));
            if (error == Error::kSuccess) {
                error = IsCancelled(session_id) ? Error::kCancelled : Error::kDrainTimeout;
            }
        }
    }

    FinishSession(session_id, error);
}

void PcmTtsPlayer::FinishSession(uint32_t session_id, Error error) {
    FinishedCallback callback;
    CloseCallback close;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (session_id == 0 || session_id != session_id_) {
            ESP_LOGW(TAG, "Ignoring completion of superseded PCM TTS session %lu",
                     static_cast<unsigned long>(session_id));
            return;
        }
        state_ = State::kIdle;
        session_id_ = 0;
        pending_abort_ = false;
        cancelled_ = false;
        session_started_ = false;
        abort_issued_ = false;
        reader_ = nullptr;
        prepare_ = nullptr;
        callback = std::move(finished_callback_);
        finished_callback_ = nullptr;
        close = std::move(close_callback_);
        close_callback_ = nullptr;
    }
    ESP_LOGI(TAG, "PCM TTS session %lu finished: %s", static_cast<unsigned long>(session_id),
             GetErrorName(error));
    if (close) {
        // Worker-task context: the source releases its transport on the thread that used it, and
        // the release happens before the main task is told the session ended.
        close();
    }
    if (callback) {
        // Worker-task context: the callee must schedule state/UI changes on the main task.
        callback(session_id, error);
    }
}

const char* PcmTtsPlayer::GetStateName(State state) {
    switch (state) {
        case State::kIdle:
            return "idle";
        case State::kBeginning:
            return "beginning";
        case State::kActive:
            return "active";
        case State::kEnding:
            return "ending";
    }
    return "unknown";
}

const char* PcmTtsPlayer::GetErrorName(Error error) {
    switch (error) {
        case Error::kSuccess:
            return "success";
        case Error::kCancelled:
            return "cancelled";
        case Error::kDrainTimeout:
            return "drain-timeout";
        case Error::kSampleRateMismatch:
            return "sample-rate-mismatch";
        case Error::kApiKeyMissing:
            return "api-key-missing";
        case Error::kHttpFailed:
            return "http-failed";
        case Error::kBaiduJsonError:
            return "baidu-json-error";
        case Error::kAudioFormatError:
            return "audio-format-error";
    }
    return "unknown";
}
