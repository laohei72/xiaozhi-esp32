#ifndef BAIDU_TTS_SOURCE_H_
#define BAIDU_TTS_SOURCE_H_

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "pcm_tts_player.h"

class Http;

/**
 * Credentials of the Baidu speech API, read once per utterance.
 *
 * Stored read-only in the "reminder" NVS namespace: tts_api_key (AK), tts_secret_key (SK) and
 * tts_voice (speaker id, "5003" when unset).
 */
struct BaiduTtsConfig {
    std::string api_key;
    std::string secret_key;
    std::string voice{"5003"};
    std::string cuid;  // per-device client id; the MAC address is used when available
};

/**
 * Streams the PCM body of one Baidu TTS utterance into PcmTtsPlayer, unchanged.
 *
 * Wire format (verified against the live service, see docs/PCM_PLAYBACK_DESIGN.md):
 *  1. POST https://aip.baidubce.com/oauth/2.0/token with a form body
 *     "grant_type=client_credentials&client_id=<AK>&client_secret=<SK>"; the reply carries
 *     access_token and expires_in.
 *  2. POST https://aip.baidubce.com/rest/2.0/speech/publiccloudspeech/v1/tts?access_token=<token>
 *     with a JSON body (text/voice/lang/format=pcm/sample_rate=24000/speed/pitch/volume).
 *  3. The reply is a streamed "audio/basic;codec=pcm;rate=24000;channel=1" body of s16le PCM.
 *
 * Concurrency contract:
 *  - Open()/Read()/Close() run on the PcmTtsPlayer worker task. Http has no documented
 *    thread-safety guarantee, so a transport is created, used and destroyed by one thread only.
 *    PcmTtsPlayer::CloseCallback is what guarantees Close() runs on that same worker.
 *  - Cancel() is the only method the main task may call while a session is streaming. It only
 *    advances an atomic generation counter: the worker notices it before the next request, and at
 *    the latest after one body-read timeout, and closes the transport itself.
 *  - The access token is cached across utterances behind token_mutex_; no I/O is done under it.
 */
class BaiduTtsSource {
public:
    // Runs on the worker task. Returns false when the credentials are not configured, which is
    // reported as PcmTtsPlayer::Error::kApiKeyMissing.
    using ConfigLoader = std::function<bool(BaiduTtsConfig& config)>;
    // Creates the transport of exactly one request. May return nullptr when the network is not up.
    using HttpFactory = std::function<std::unique_ptr<Http>()>;

    // The only audio format AudioService::BeginPcmPlayback() accepts.
    static constexpr int kSampleRate = 24000;
    static constexpr int kChannels = 1;

    BaiduTtsSource(ConfigLoader config_loader, HttpFactory http_factory);
    ~BaiduTtsSource();

    BaiduTtsSource(const BaiduTtsSource&) = delete;
    BaiduTtsSource& operator=(const BaiduTtsSource&) = delete;

    /**
     * Fetches (or reuses) the access token and opens the audio stream for |text|.
     *
     * Returns kSuccess only when the response is a 200 with a PCM 24 kHz mono content type, so
     * BeginPcmPlayback() is never called for a request that cannot be played. Every other
     * PcmTtsPlayer::Error value means no transport is left behind.
     */
    PcmTtsPlayer::Error Open(const std::string& text);

    // Streams the body. kEndOfStream on a clean EOF, kHttpFailed when the transport breaks.
    PcmTtsPlayer::ReadResult Read(std::vector<uint8_t>& chunk);

    // Releases the transport. Safe to call when Open() failed or was never called.
    void Close();

    // Requests cancellation; callable from the main task while the worker is streaming.
    void Cancel();

private:
    PcmTtsPlayer::Error ObtainToken(const BaiduTtsConfig& config, std::string& token);
    PcmTtsPlayer::Error FetchToken(const BaiduTtsConfig& config, std::string& token);
    PcmTtsPlayer::Error RequestSpeech(const BaiduTtsConfig& config, const std::string& token,
                                      const std::string& text, bool& auth_error);
    void InvalidateToken();
    void CloseTransport();
    bool IsCancelRequested() const;

    ConfigLoader config_loader_;
    HttpFactory http_factory_;
    // Transport of the streaming response. Owned by the worker task that opened it.
    std::unique_ptr<Http> http_;

    std::mutex token_mutex_;
    std::string access_token_;
    int64_t token_expires_at_us_ = 0;

    // Cancel() bumps the request counter; Open() snapshots it for the session and the worker
    // compares the two. No lock is needed, so Cancel() never blocks on a blocked body read.
    std::atomic<uint32_t> cancel_generation_{0};
    uint32_t session_generation_ = 0;
};

#endif  // BAIDU_TTS_SOURCE_H_
