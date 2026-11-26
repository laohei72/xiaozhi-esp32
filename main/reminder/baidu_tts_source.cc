#include "baidu_tts_source.h"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <utility>

#include <cJSON.h>
#include <esp_log.h>
#include <esp_timer.h>

#include "cjson_utils.h"
#include "http.h"

namespace {

const char* TAG = "BaiduTts";

// Verified against the live service: token in the query, JSON body, PCM 24 kHz mono back.
const char* kTokenUrl = "https://aip.baidubce.com/oauth/2.0/token";
const char* kSpeechUrl = "https://aip.baidubce.com/rest/2.0/speech/publiccloudspeech/v1/tts";

// Token exchange and the TTS request before the body starts; the body is streamed with a per-read
// timeout so a stalled sentence cannot hang the worker task for the whole utterance.
constexpr int kTokenTimeoutMs = 5000;
constexpr int kSpeechTimeoutMs = 5000;
constexpr int kBodyReadTimeoutMs = 1000;

constexpr size_t kReadChunkBytes = 4096;
constexpr size_t kMaxStatusBodyBytes = 512;
// Renew the token one minute before its advertised expiry so no request starts with a dying one.
constexpr int64_t kTokenExpiryMarginUs = 60LL * 1000 * 1000;
constexpr const char* kDefaultVoice = "5003";

// "Access token invalid or no longer valid" and "invalid client id": a cached token may have been
// revoked before expires_in, which is the only case worth one retry.
constexpr int kAuthErrorCodes[] = {100001, 100020};

std::string Trim(const std::string& value) {
    const size_t begin = value.find_first_not_of(" \t\r\n");
    if (begin == std::string::npos) {
        return std::string();
    }
    const size_t end = value.find_last_not_of(" \t\r\n");
    return value.substr(begin, end - begin + 1);
}

std::string ToLower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

// Escapes everything but the unreserved set, so the token, AK and SK cannot break the URL or the
// form body and never have to be re-encoded.
std::string UrlEncode(const std::string& value) {
    static const char kHex[] = "0123456789ABCDEF";
    std::string encoded;
    encoded.reserve(value.size() * 3);
    for (unsigned char c : value) {
        if (std::isalnum(c) != 0 || c == '-' || c == '_' || c == '.' || c == '~') {
            encoded.push_back(static_cast<char>(c));
        } else {
            encoded.push_back('%');
            encoded.push_back(kHex[c >> 4]);
            encoded.push_back(kHex[c & 0x0F]);
        }
    }
    return encoded;
}

const char* JsonString(const cJSON* item) {
    if (!cJSON_IsString(item) || item->valuestring == nullptr) {
        return nullptr;
    }
    return item->valuestring;
}

bool IsAuthError(int error_code) {
    for (int code : kAuthErrorCodes) {
        if (code == error_code) {
            return true;
        }
    }
    return false;
}

/**
 * Matches "audio/basic;codec=pcm;rate=24000;channel=1" (case-insensitive, parameters in any
 * order) and reports the announced rate and channel count. A missing channel parameter is treated
 * as mono; a missing codec or rate is rejected because the format would then be unverifiable.
 */
bool ParseAudioContentType(const std::string& value, int& sample_rate, int& channels) {
    sample_rate = 0;
    channels = 0;
    std::vector<std::string> parameters;
    size_t start = 0;
    while (true) {
        const size_t separator = value.find(';', start);
        parameters.push_back(Trim(value.substr(
            start, separator == std::string::npos ? std::string::npos : separator - start)));
        if (separator == std::string::npos) {
            break;
        }
        start = separator + 1;
    }
    if (ToLower(parameters[0]) != "audio/basic") {
        return false;
    }
    bool has_codec = false;
    bool has_rate = false;
    for (size_t index = 1; index < parameters.size(); ++index) {
        const size_t equals = parameters[index].find('=');
        const std::string key = ToLower(Trim(parameters[index].substr(0, equals)));
        const std::string parameter =
            equals == std::string::npos
                ? std::string()
                : ToLower(Trim(parameters[index].substr(equals + 1)));
        if (key == "codec") {
            if (parameter != "pcm") {
                return false;
            }
            has_codec = true;
        } else if (key == "rate") {
            sample_rate = std::atoi(parameter.c_str());
            has_rate = true;
        } else if (key == "channel" || key == "channels") {
            channels = std::atoi(parameter.c_str());
        }
    }
    return has_codec && has_rate;
}

bool IsJsonContentType(const std::string& value) {
    return ToLower(Trim(value)).rfind("application/json", 0) == 0;
}

/**
 * Extracts the status of a Baidu JSON reply. Both the speech API (err_no/err_msg) and the OAuth
 * endpoint (error/error_description) are understood, plus the error_code form.
 */
bool ParseJsonStatus(const std::string& body, int& error_code, std::string& error_message) {
    CJsonUniquePtr root(cJSON_Parse(body.c_str()));
    if (!root) {
        return false;
    }
    int code = 0;
    for (const char* key : {"error_code", "err_no"}) {
        const cJSON* item = cJSON_GetObjectItemCaseSensitive(root.get(), key);
        if (cJSON_IsNumber(item)) {
            code = item->valueint;
            break;
        }
        if (const char* text = JsonString(item); text != nullptr && text[0] != '\0') {
            code = std::atoi(text);
            break;
        }
    }
    for (const char* key : {"error_msg", "err_msg", "error", "error_description"}) {
        if (const char* text = JsonString(cJSON_GetObjectItemCaseSensitive(root.get(), key));
            text != nullptr) {
            error_message = text;
            break;
        }
    }
    if (code == 0 && error_message.empty()) {
        return false;
    }
    error_code = code;
    return true;
}

// The verified JSON request body: PCM, 24 kHz, speed 1, pitch 0, volume 1.
std::string BuildSpeechBody(const std::string& text, const std::string& voice) {
    CJsonUniquePtr root(cJSON_CreateObject());
    if (!root) {
        return std::string();
    }
    cJSON_AddStringToObject(root.get(), "text", text.c_str());
    cJSON_AddStringToObject(root.get(), "voice", voice.c_str());
    cJSON_AddStringToObject(root.get(), "lang", "zh");
    cJSON_AddStringToObject(root.get(), "format", "pcm");
    cJSON_AddNumberToObject(root.get(), "sample_rate", BaiduTtsSource::kSampleRate);
    cJSON_AddNumberToObject(root.get(), "speed", 1);
    cJSON_AddNumberToObject(root.get(), "pitch", 0);
    cJSON_AddNumberToObject(root.get(), "volume", 1);
    CJsonStringUniquePtr printed(cJSON_PrintUnformatted(root.get()));
    if (!printed) {
        return std::string();
    }
    return std::string(printed.get());
}

// Reads a bounded prefix of a status body; the caller closes the response afterwards.
std::string ReadStatusBody(Http& http, size_t limit) {
    std::string body;
    char buffer[128];
    while (body.size() < limit) {
        auto read = http.Read(buffer, std::min(sizeof(buffer), limit - body.size()));
        if (!read || read.value() <= 0) {
            break;
        }
        body.append(buffer, static_cast<size_t>(read.value()));
    }
    return body;
}

void LogTransportFailure(const char* stage, const NetworkError& error) {
    if (error.empty()) {
        ESP_LOGW(TAG, "Baidu %s request failed", stage);
    } else {
        ESP_LOGW(TAG, "Baidu %s request failed: %s (code %d, native %d)", stage, error.Name(),
                 static_cast<int>(error.code), error.native);
    }
}

}  // namespace

BaiduTtsSource::BaiduTtsSource(ConfigLoader config_loader, HttpFactory http_factory)
    : config_loader_(std::move(config_loader)), http_factory_(std::move(http_factory)) {}

BaiduTtsSource::~BaiduTtsSource() { CloseTransport(); }

PcmTtsPlayer::Error BaiduTtsSource::Open(const std::string& text) {
    // A cancelled session may have left a transport behind; the worker closes it before a new
    // request so at most one streamed body is ever open.
    CloseTransport();
    if (text.empty()) {
        ESP_LOGE(TAG, "Refusing a TTS request with an empty text");
        return PcmTtsPlayer::Error::kAudioFormatError;
    }
    session_generation_ = cancel_generation_.load(std::memory_order_acquire);

    BaiduTtsConfig config;
    if (!config_loader_ || !config_loader_(config) || config.api_key.empty() ||
        config.secret_key.empty()) {
        ESP_LOGW(TAG, "Baidu TTS is not configured (tts_api_key / tts_secret_key)");
        return PcmTtsPlayer::Error::kApiKeyMissing;
    }
    if (config.voice.empty()) {
        config.voice = kDefaultVoice;
    }

    for (int attempt = 0; attempt < 2; ++attempt) {
        std::string token;
        PcmTtsPlayer::Error error = ObtainToken(config, token);
        if (error != PcmTtsPlayer::Error::kSuccess) {
            return error;
        }
        if (IsCancelRequested()) {
            return PcmTtsPlayer::Error::kCancelled;
        }

        bool auth_error = false;
        error = RequestSpeech(config, token, text, auth_error);
        if (error == PcmTtsPlayer::Error::kSuccess) {
            return error;
        }
        if (!auth_error || attempt > 0 || IsCancelRequested()) {
            return error;
        }
        // A cached token can be revoked before its advertised expiry: refresh it exactly once,
        // then report the rejection instead of hammering the service.
        ESP_LOGW(TAG, "Baidu rejected the access token; refreshing it once");
        InvalidateToken();
    }
    return PcmTtsPlayer::Error::kBaiduJsonError;
}

PcmTtsPlayer::ReadResult BaiduTtsSource::Read(std::vector<uint8_t>& chunk) {
    if (!http_) {
        chunk.clear();
        return PcmTtsPlayer::ReadResult::kEndOfStream;
    }
    chunk.resize(kReadChunkBytes);
    auto read = http_->Read(reinterpret_cast<char*>(chunk.data()), chunk.size());
    if (!read) {
        if (!IsCancelRequested()) {
            LogTransportFailure("TTS body", read.error());
        }
        chunk.clear();
        CloseTransport();
        return PcmTtsPlayer::ReadResult::kHttpFailed;
    }
    if (read.value() <= 0) {
        chunk.clear();
        CloseTransport();  // the body ended: release the connection before the drain wait
        return PcmTtsPlayer::ReadResult::kEndOfStream;
    }
    chunk.resize(static_cast<size_t>(read.value()));
    return PcmTtsPlayer::ReadResult::kOk;
}

void BaiduTtsSource::Close() { CloseTransport(); }

void BaiduTtsSource::Cancel() { cancel_generation_.fetch_add(1, std::memory_order_acq_rel); }

bool BaiduTtsSource::IsCancelRequested() const {
    return cancel_generation_.load(std::memory_order_acquire) != session_generation_;
}

void BaiduTtsSource::CloseTransport() {
    if (!http_) {
        return;
    }
    http_->Close();
    http_.reset();
}

void BaiduTtsSource::InvalidateToken() {
    std::lock_guard<std::mutex> lock(token_mutex_);
    access_token_.clear();
    token_expires_at_us_ = 0;
}

PcmTtsPlayer::Error BaiduTtsSource::ObtainToken(const BaiduTtsConfig& config,
                                                std::string& token) {
    {
        // Read-only reuse: the token is shared by every utterance, so this lock is held only for
        // the copy and never across a request.
        std::lock_guard<std::mutex> lock(token_mutex_);
        if (!access_token_.empty() && esp_timer_get_time() < token_expires_at_us_) {
            token = access_token_;
            ESP_LOGD(TAG, "Reusing the cached Baidu access token");
            return PcmTtsPlayer::Error::kSuccess;
        }
    }
    return FetchToken(config, token);
}


PcmTtsPlayer::Error BaiduTtsSource::FetchToken(const BaiduTtsConfig& config,
                                               std::string& token) {
    std::unique_ptr<Http> http = http_factory_ ? http_factory_() : nullptr;
    if (!http) {
        ESP_LOGE(TAG, "No network transport for the Baidu token request");
        return PcmTtsPlayer::Error::kHttpFailed;
    }
    // The SK stays in the POST body: a query parameter would end up in transport and access logs.
    std::string body = "grant_type=client_credentials&client_id=" + UrlEncode(config.api_key) +
                       "&client_secret=" + UrlEncode(config.secret_key);
    http->SetTimeout(kTokenTimeoutMs);
    http->SetKeepAlive(false);
    http->SetHeader("Content-Type", "application/x-www-form-urlencoded");
    http->SetContent(std::move(body));

    auto opened = http->Open("POST", kTokenUrl);
    if (!opened) {
        LogTransportFailure("token", opened.error());
        return IsCancelRequested() ? PcmTtsPlayer::Error::kCancelled
                                   : PcmTtsPlayer::Error::kHttpFailed;
    }
    auto status = http->GetStatusCode();
    if (!status) {
        LogTransportFailure("token", status.error());
        http->Close();
        return IsCancelRequested() ? PcmTtsPlayer::Error::kCancelled
                                   : PcmTtsPlayer::Error::kHttpFailed;
    }
    if (status.value() != 200) {
        // The OAuth endpoint answers 400/401 with a JSON body; it holds no token to use.
        ESP_LOGW(TAG, "Baidu token request rejected: http status %d", status.value());
        http->Close();
        return PcmTtsPlayer::Error::kHttpFailed;
    }

    const std::string payload = http->ReadAll();
    http->Close();
    if (IsCancelRequested()) {
        return PcmTtsPlayer::Error::kCancelled;
    }

    CJsonUniquePtr root(cJSON_Parse(payload.c_str()));
    if (!root) {
        ESP_LOGW(TAG, "Baidu token response is not valid JSON (%zu bytes)", payload.size());
        return PcmTtsPlayer::Error::kBaiduJsonError;
    }
    const char* access_token =
        JsonString(cJSON_GetObjectItemCaseSensitive(root.get(), "access_token"));
    if (access_token == nullptr || access_token[0] == '\0') {
        const char* error = JsonString(cJSON_GetObjectItemCaseSensitive(root.get(), "error"));
        const char* description =
            JsonString(cJSON_GetObjectItemCaseSensitive(root.get(), "error_description"));
        ESP_LOGW(TAG, "Baidu token request failed: %s (%s)",
                 error != nullptr ? error : "no error field",
                 description != nullptr ? description : "no description");
        return PcmTtsPlayer::Error::kBaiduJsonError;
    }

    const cJSON* expires = cJSON_GetObjectItemCaseSensitive(root.get(), "expires_in");
    const int64_t expires_in = cJSON_IsNumber(expires) ? expires->valueint : 0;
    token = access_token;
    {
        std::lock_guard<std::mutex> lock(token_mutex_);
        access_token_ = token;
        // expires_in <= 60 s is not worth caching: the request would start with a dying token.
        token_expires_at_us_ =
            expires_in > 60 ? esp_timer_get_time() + expires_in * 1000000LL - kTokenExpiryMarginUs
                            : 0;
    }
    ESP_LOGI(TAG, "Obtained a Baidu access token (%lld s)", static_cast<long long>(expires_in));
    return PcmTtsPlayer::Error::kSuccess;
}

PcmTtsPlayer::Error BaiduTtsSource::RequestSpeech(const BaiduTtsConfig& config,
                                                  const std::string& token,
                                                  const std::string& text, bool& auth_error) {
    auth_error = false;
    std::unique_ptr<Http> http = http_factory_ ? http_factory_() : nullptr;
    if (!http) {
        ESP_LOGE(TAG, "No network transport for the Baidu TTS request");
        return PcmTtsPlayer::Error::kHttpFailed;
    }
    std::string body = BuildSpeechBody(text, config.voice);
    if (body.empty()) {
        ESP_LOGE(TAG, "Failed to build the Baidu TTS request body");
        return PcmTtsPlayer::Error::kBaiduJsonError;
    }
    http->SetTimeout(kSpeechTimeoutMs);
    http->SetKeepAlive(false);
    http->SetHeader("Content-Type", "application/json");
    http->SetContent(std::move(body));

    // The verified interface carries the token in the query; no Authorization header and no legacy
    // tok/per/aue form parameters.
    const std::string url = std::string(kSpeechUrl) + "?access_token=" + UrlEncode(token);
    auto opened = http->Open("POST", url);
    if (!opened) {
        LogTransportFailure("TTS", opened.error());
        return IsCancelRequested() ? PcmTtsPlayer::Error::kCancelled
                                   : PcmTtsPlayer::Error::kHttpFailed;
    }
    auto status = http->GetStatusCode();
    if (!status) {
        LogTransportFailure("TTS", status.error());
        http->Close();
        return IsCancelRequested() ? PcmTtsPlayer::Error::kCancelled
                                   : PcmTtsPlayer::Error::kHttpFailed;
    }

    const int status_code = status.value();
    const std::string content_type = http->GetResponseHeader("Content-Type");
    if (status_code != 200) {
        const std::string payload = ReadStatusBody(*http, kMaxStatusBodyBytes);
        http->Close();
        int error_code = 0;
        std::string error_message;
        if (ParseJsonStatus(payload, error_code, error_message)) {
            ESP_LOGW(TAG, "Baidu TTS rejected the request: http status %d, code %d (%s)",
                     status_code, error_code, error_message.c_str());
            auth_error = IsAuthError(error_code);
            return IsCancelRequested() ? PcmTtsPlayer::Error::kCancelled
                                       : PcmTtsPlayer::Error::kBaiduJsonError;
        }
        // No readable status body: a gateway or server failure, not an API-level rejection.
        ESP_LOGW(TAG, "Baidu TTS request failed: http status %d", status_code);
        return IsCancelRequested() ? PcmTtsPlayer::Error::kCancelled
                                   : PcmTtsPlayer::Error::kHttpFailed;
    }
    if (IsJsonContentType(content_type)) {
        // A 200 with a JSON body describes a rejection, never audio: playing it would be silence.
        const std::string payload = ReadStatusBody(*http, kMaxStatusBodyBytes);
        http->Close();
        int error_code = 0;
        std::string error_message;
        if (ParseJsonStatus(payload, error_code, error_message)) {
            ESP_LOGW(TAG, "Baidu TTS returned status JSON: code %d (%s)", error_code,
                     error_message.c_str());
            auth_error = IsAuthError(error_code);
        } else {
            ESP_LOGW(TAG, "Baidu TTS returned an unreadable status body instead of PCM audio");
        }
        return IsCancelRequested() ? PcmTtsPlayer::Error::kCancelled
                                   : PcmTtsPlayer::Error::kBaiduJsonError;
    }

    int sample_rate = 0;
    int channels = 0;
    const bool parsed = ParseAudioContentType(content_type, sample_rate, channels);
    if (!parsed || sample_rate != kSampleRate || (channels != 0 && channels != kChannels)) {
        ESP_LOGE(TAG, "Baidu TTS audio format not supported: '%s' (%d Hz / %d channel)",
                 content_type.c_str(), sample_rate, channels);
        http->Close();
        return PcmTtsPlayer::Error::kAudioFormatError;
    }

    // The body is already streaming: keep the connection open and hand the reads to the worker.
    http->SetTimeout(kBodyReadTimeoutMs);
    http_ = std::move(http);
    ESP_LOGI(TAG, "Streaming Baidu TTS audio: %d Hz / %d channel", sample_rate,
             channels == 0 ? kChannels : channels);
    return PcmTtsPlayer::Error::kSuccess;
}

