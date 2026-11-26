#ifndef PCM_TTS_PLAYER_H_
#define PCM_TTS_PLAYER_H_

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <cstdint>
#include <functional>
#include <mutex>
#include <vector>

#include "audio_service.h"

/**
 * Streams 24 kHz mono s16le PCM into AudioService's pass-through playback path.
 *
 * The player owns no transport: the caller hands in a chunk reader (the stage-4b self test passes
 * BaiduTtsSource, which streams the Baidu TTS HTTP body). Every AudioService call runs on the
 * worker task and never while mutex_ is held.
 *
 * Concurrency contract (docs/PCM_PLAYBACK_DESIGN.md §6.3, §9.1, §9.5):
 *  - Start()/Stop() are called from the main task; the reader and the finished callback run on
 *    the worker task, so the callback must schedule any state/UI change.
 *  - BeginPcmPlayback() and AbortPcmPlayback() are never issued concurrently: while the state
 *    is kBeginning a Stop() only records pending_abort_, and the worker aborts right after
 *    BeginPcmPlayback() returns (§6.2.1, §6.3).
 *  - EndPcmPlayback() may run while Stop() issues AbortPcmPlayback(): AudioService wakes the
 *    drain wait and returns false, which is reported as kCancelled (§9.1 path ②).
 *  - End() and Abort() are issued at most once per session. A callback that refers to a session
 *    id newer than the one it belongs to is ignored, so stale completions never touch a live one.
 */
class PcmTtsPlayer {
public:
    enum class State {
        kIdle,       // no session; Start() is allowed
        kBeginning,  // worker is about to call BeginPcmPlayback() / is inside it
        kActive,     // session is writable; the worker streams chunks
        kEnding,     // worker is inside EndPcmPlayback() (drain wait)
    };

    /**
     * Reported through FinishedCallback.
     *
     * kDrainTimeout also covers a write that AudioService rejected without a cancel request
     * (playback queue reset by Stop()/ResetDecoder()), because then the tail can never drain.
     * kApiKeyMissing, kHttpFailed, kBaiduJsonError and kAudioFormatError come from the source's
     * prepare step (BaiduTtsSource) and are reported before any AudioService session exists.
     */
    enum class Error {
        kSuccess = 0,
        kCancelled,
        kDrainTimeout,
        kSampleRateMismatch,
        kApiKeyMissing,
        kHttpFailed,
        kBaiduJsonError,
        kAudioFormatError,
    };

    enum class ReadResult {
        kOk,           // chunk holds the next slice; an empty chunk is allowed (no data yet)
        kEndOfStream,  // source finished; the player flushes the tail frame and drains
        kHttpFailed,   // source failed; the player stops reading and reports kHttpFailed
    };

    // Fills |chunk| with the next slice of little-endian s16le PCM. Runs on the worker task.
    using ChunkReader = std::function<ReadResult(std::vector<uint8_t>& chunk)>;
    // Runs on the worker task; must not call SetDeviceState() or touch the UI directly.
    using FinishedCallback = std::function<void(uint32_t session_id, Error error)>;
    // Runs on the worker task before BeginPcmPlayback(): fetch credentials, open the transport and
    // verify the audio format. A returned error is reported as-is, and because no AudioService
    // session exists yet, a failing validation never has to be End()-ed or Abort()-ed.
    using PrepareCallback = std::function<Error()>;
    // Runs on the worker task exactly once per started session, after Abort/End and before the
    // finished callback: releases the transport the reader owns, on the thread that used it.
    using CloseCallback = std::function<void()>;

    explicit PcmTtsPlayer(AudioService& audio_service);
    ~PcmTtsPlayer();

    PcmTtsPlayer(const PcmTtsPlayer&) = delete;
    PcmTtsPlayer& operator=(const PcmTtsPlayer&) = delete;

    /**
     * Starts a session and returns its id (> 0).
     *
     * Returns 0 when a session is already running, when the reader or the finished callback is
     * missing, or when the worker task cannot be created. |sample_rate| and |channels| are passed
     * to BeginPcmPlayback(), which rejects everything but the codec's own 24 kHz mono format.
     *
     * |prepare| runs first on the worker task, so a source that fails to open (credentials, token,
     * HTTP status, audio format) reports that error without ever creating an AudioService session.
     * |close| is called on the worker task once the session ended, whichever way it ended.
     */
    uint32_t Start(int sample_rate, int channels, ChunkReader reader,
                   FinishedCallback finished_callback, PrepareCallback prepare = nullptr,
                   CloseCallback close = nullptr);

    /**
     * Requests cancellation. Never blocks on BeginPcmPlayback()/EndPcmPlayback() and is safe to
     * call repeatedly: at most one AbortPcmPlayback() is issued per session. The finished
     * callback still fires once (with kCancelled).
     */
    void Stop();

    bool IsBusy() const;    // kBeginning / kActive / kEnding
    bool IsActive() const;  // kActive only (WritePcmData() is accepted in this state)
    State GetState() const;
    uint32_t session_id() const;

    static const char* GetStateName(State state);
    static const char* GetErrorName(Error error);

private:
    AudioService& audio_service_;
    mutable std::mutex mutex_;
    ChunkReader reader_;
    FinishedCallback finished_callback_;
    PrepareCallback prepare_;
    CloseCallback close_callback_;
    // State lock scope: only the fields below. No AudioService call is made while it is held.
    uint32_t session_id_ = 0;
    uint32_t next_session_id_ = 0;
    int sample_rate_ = 0;
    int channels_ = 0;
    State state_ = State::kIdle;
    bool pending_abort_ = false;    // Stop() arrived while kBeginning
    bool cancelled_ = false;        // Stop() was called for this session
    bool session_started_ = false;  // BeginPcmPlayback() returned true
    bool abort_issued_ = false;     // AbortPcmPlayback() was issued for this session

    static void WorkerEntry(void* arg);
    void WorkerTask(uint32_t session_id);
    bool IsCancelled(uint32_t session_id) const;
    void FinishSession(uint32_t session_id, Error error);
};

#endif  // PCM_TTS_PLAYER_H_
