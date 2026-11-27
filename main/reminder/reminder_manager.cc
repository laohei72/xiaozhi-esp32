#include "reminder_manager.h"

#include <esp_log.h>
#include <esp_timer.h>

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <cmath>

#include "cjson_utils.h"
#include "settings.h"

#define TAG "Reminder"

namespace {

constexpr size_t kMaxTasks = 16;
constexpr size_t kMaxTextBytes = 120;
constexpr int64_t kMinTrustedUnix = 1735689600;
constexpr int64_t kLateGraceSeconds = 300;
constexpr const char* kNvsNamespace = "reminder_tasks";
constexpr const char* kNvsKey = "tasks_v1";
constexpr int kJsonVersion = 1;
constexpr size_t kMaxRestoreEntries = 4 * kMaxTasks;

bool IsBlank(const std::string& text) {
    for (unsigned char c : text) {
        if (std::isspace(c) == 0) {
            return false;
        }
    }
    return true;
}

bool ParseTypeName(const char* name, ReminderType& type) {
    if (name == nullptr) return false;
    if (std::strcmp(name, "relative") == 0) type = ReminderType::kRelative;
    else if (std::strcmp(name, "absolute") == 0) type = ReminderType::kAbsolute;
    else if (std::strcmp(name, "daily") == 0) type = ReminderType::kDaily;
    else if (std::strcmp(name, "weekly") == 0) type = ReminderType::kWeekly;
    else return false;
    return true;
}

bool ParseStateName(const char* name, ReminderState& state) {
    if (name == nullptr) return false;
    if (std::strcmp(name, "pending") == 0) state = ReminderState::kPending;
    else if (std::strcmp(name, "due") == 0) state = ReminderState::kDue;
    else if (std::strcmp(name, "playing") == 0) state = ReminderState::kPlaying;
    else if (std::strcmp(name, "awaiting_response") == 0) state = ReminderState::kAwaitingResponse;
    else if (std::strcmp(name, "completed") == 0) state = ReminderState::kCompleted;
    else if (std::strcmp(name, "cancelled") == 0) state = ReminderState::kCancelled;
    else if (std::strcmp(name, "paused") == 0) state = ReminderState::kPaused;
    else return false;
    return true;
}

int64_t JsonNumber(const cJSON* object, const char* key, int64_t fallback) {
    const cJSON* item = cJSON_GetObjectItem(object, key);
    if (!cJSON_IsNumber(item)) return fallback;
    return static_cast<int64_t>(item->valuedouble);
}

}  // namespace

ReminderManager::ReminderManager() { ESP_LOGI(TAG, "ReminderManager created"); }
ReminderManager::~ReminderManager() = default;

uint64_t ReminderManager::NowMonotonicMs() {
    return static_cast<uint64_t>(esp_timer_get_time()) / 1000;
}
int64_t ReminderManager::NowUnixSeconds() { return static_cast<int64_t>(time(nullptr)); }
bool ReminderManager::IsSystemTimeSynced() { return NowUnixSeconds() > kMinTrustedUnix; }
bool ReminderManager::IsRecurring(ReminderType type) {
    return type == ReminderType::kDaily || type == ReminderType::kWeekly;
}

const char* ReminderManager::StateName(ReminderState state) {
    switch (state) {
        case ReminderState::kPending: return "pending";
        case ReminderState::kDue: return "due";
        case ReminderState::kPlaying: return "playing";
        case ReminderState::kAwaitingResponse: return "awaiting_response";
        case ReminderState::kCompleted: return "completed";
        case ReminderState::kCancelled: return "cancelled";
        case ReminderState::kPaused: return "paused";
    }
    return "unknown";
}

const char* ReminderManager::TypeName(ReminderType type) {
    switch (type) {
        case ReminderType::kRelative: return "relative";
        case ReminderType::kAbsolute: return "absolute";
        case ReminderType::kDaily: return "daily";
        case ReminderType::kWeekly: return "weekly";
    }
    return "unknown";
}

int64_t ReminderManager::ComputeNextDaily(int hour, int minute, int64_t after_unix) {
    std::time_t base = static_cast<std::time_t>(after_unix);
    struct tm local = {};
    if (localtime_r(&base, &local) == nullptr) return 0;
    local.tm_hour = hour;
    local.tm_min = minute;
    local.tm_sec = 0;
    local.tm_isdst = -1;
    std::time_t candidate = mktime(&local);
    if (candidate == static_cast<std::time_t>(-1)) return 0;
    if (static_cast<int64_t>(candidate) > after_unix) return static_cast<int64_t>(candidate);
    struct tm next = {};
    if (localtime_r(&candidate, &next) == nullptr) return 0;
    next.tm_mday += 1;
    next.tm_hour = hour;
    next.tm_min = minute;
    next.tm_sec = 0;
    next.tm_isdst = -1;
    candidate = mktime(&next);
    if (candidate == static_cast<std::time_t>(-1)) return 0;
    return static_cast<int64_t>(candidate);
}

int64_t ReminderManager::ComputeNextWeekly(int weekday, int hour, int minute, int64_t after_unix) {
    std::time_t base = static_cast<std::time_t>(after_unix);
    struct tm local = {};
    if (localtime_r(&base, &local) == nullptr) return 0;
    const int delta_days = (weekday - local.tm_wday + 7) % 7;
    local.tm_mday += delta_days;
    local.tm_hour = hour;
    local.tm_min = minute;
    local.tm_sec = 0;
    local.tm_isdst = -1;
    std::time_t candidate = mktime(&local);
    if (candidate == static_cast<std::time_t>(-1)) return 0;
    if (static_cast<int64_t>(candidate) > after_unix) return static_cast<int64_t>(candidate);
    struct tm next = {};
    if (localtime_r(&candidate, &next) == nullptr) return 0;
    next.tm_mday += 7;
    next.tm_hour = hour;
    next.tm_min = minute;
    next.tm_sec = 0;
    next.tm_isdst = -1;
    candidate = mktime(&next);
    if (candidate == static_cast<std::time_t>(-1)) return 0;
    return static_cast<int64_t>(candidate);
}

int64_t ReminderManager::ComputeNextTrigger(const ReminderTask& task, int64_t after_unix) {
    switch (task.type) {
        case ReminderType::kDaily:
            return ComputeNextDaily(task.daily_hour, task.daily_minute, after_unix);
        case ReminderType::kWeekly:
            return ComputeNextWeekly(task.weekly_weekday, task.weekly_hour, task.weekly_minute,
                                     after_unix);
        default:
            return 0;
    }
}

std::string ReminderManager::BuildSchedule(ReminderType type, uint64_t delay_ms,
                                           int64_t trigger_unix, int daily_hour, int daily_minute,
                                           int weekly_weekday, int weekly_hour, int weekly_minute,
                                           uint64_t created_ms, ReminderTask& task) {
    task.type = type;
    task.recurring_active = IsRecurring(type);
    task.trigger_ms = 0;
    task.trigger_unix = 0;
    task.daily_hour = 0;
    task.daily_minute = 0;
    task.weekly_weekday = 0;
    task.weekly_hour = 0;
    task.weekly_minute = 0;

    switch (type) {
        case ReminderType::kRelative:
            if (delay_ms == 0) return "invalid-time";
            task.trigger_ms = created_ms + delay_ms;
            if (task.trigger_ms < created_ms) return "invalid-time";
            task.recurring_active = false;
            return "";
        case ReminderType::kAbsolute:
            if (!IsSystemTimeSynced()) return "time-not-synced";
            if (trigger_unix <= 0) return "invalid-time";
            task.trigger_unix = trigger_unix;
            task.recurring_active = false;
            return "";
        case ReminderType::kDaily: {
            if (!IsSystemTimeSynced()) return "time-not-synced";
            if (daily_hour < 0 || daily_hour > 23 || daily_minute < 0 || daily_minute > 59)
                return "invalid-time";
            const int64_t next = ComputeNextDaily(daily_hour, daily_minute, NowUnixSeconds());
            if (next <= 0) return "invalid-time";
            task.daily_hour = daily_hour;
            task.daily_minute = daily_minute;
            task.trigger_unix = next;
            return "";
        }
        case ReminderType::kWeekly: {
            if (!IsSystemTimeSynced()) return "time-not-synced";
            if (weekly_weekday < 0 || weekly_weekday > 6 || weekly_hour < 0 || weekly_hour > 23 ||
                weekly_minute < 0 || weekly_minute > 59)
                return "invalid-time";
            const int64_t next =
                ComputeNextWeekly(weekly_weekday, weekly_hour, weekly_minute, NowUnixSeconds());
            if (next <= 0) return "invalid-time";
            task.weekly_weekday = weekly_weekday;
            task.weekly_hour = weekly_hour;
            task.weekly_minute = weekly_minute;
            task.trigger_unix = next;
            return "";
        }
    }
    return "invalid-time";
}

namespace {

bool ParseStoredTask(const cJSON* item, ReminderTask& task) {
    if (!cJSON_IsObject(item)) return false;
    const cJSON* id = cJSON_GetObjectItem(item, "id");
    const cJSON* text = cJSON_GetObjectItem(item, "text");
    const cJSON* type = cJSON_GetObjectItem(item, "type");
    const cJSON* state = cJSON_GetObjectItem(item, "state");
    if (!cJSON_IsNumber(id) || !cJSON_IsString(text) || !cJSON_IsString(type) ||
        !cJSON_IsString(state))
        return false;
    const double raw_id = id->valuedouble;
    if (raw_id < 1 || raw_id > static_cast<double>(UINT32_MAX)) return false;
    task = ReminderTask{};
    task.id = static_cast<uint32_t>(raw_id);
    task.text = text->valuestring != nullptr ? text->valuestring : "";
    if (task.text.empty() || task.text.size() > kMaxTextBytes || IsBlank(task.text)) return false;
    if (!ParseTypeName(type->valuestring, task.type) ||
        !ParseStateName(state->valuestring, task.state))
        return false;
    task.recurring_active = cJSON_IsTrue(cJSON_GetObjectItem(item, "recurring_active"));
    task.trigger_ms = static_cast<uint64_t>(JsonNumber(item, "trigger_ms", 0));
    task.trigger_unix = JsonNumber(item, "trigger_unix", 0);
    task.created_ms = static_cast<uint64_t>(JsonNumber(item, "created_ms", 0));
    const cJSON* stored_retry_count = cJSON_GetObjectItem(item, "retry_count");
    double retry_count = cJSON_IsNumber(stored_retry_count) ? stored_retry_count->valuedouble : 0.0;
    if (!std::isfinite(retry_count)) retry_count = 0.0;
    task.retry_count = static_cast<uint8_t>(std::clamp(retry_count, 0.0, 255.0));
    task.last_fired_unix = JsonNumber(item, "last_fired_unix", 0);
    task.daily_hour = static_cast<int>(JsonNumber(item, "daily_hour", 0));
    task.daily_minute = static_cast<int>(JsonNumber(item, "daily_minute", 0));
    task.weekly_weekday = static_cast<int>(JsonNumber(item, "weekly_weekday", 0));
    task.weekly_hour = static_cast<int>(JsonNumber(item, "weekly_hour", 0));
    task.weekly_minute = static_cast<int>(JsonNumber(item, "weekly_minute", 0));

    const bool recurring_type =
        task.type == ReminderType::kDaily || task.type == ReminderType::kWeekly;
    const bool finished =
        task.state == ReminderState::kCompleted || task.state == ReminderState::kCancelled;
    if (!recurring_type) task.recurring_active = false;
    switch (task.type) {
        case ReminderType::kRelative:
            return task.trigger_ms > 0;
        case ReminderType::kAbsolute:
            return task.trigger_unix > 0;
        case ReminderType::kDaily:
            if (task.trigger_unix <= 0 || task.daily_hour < 0 || task.daily_hour > 23 ||
                task.daily_minute < 0 || task.daily_minute > 59)
                return false;
            task.recurring_active = !finished;
            return true;
        case ReminderType::kWeekly:
            if (task.trigger_unix <= 0 || task.weekly_weekday < 0 || task.weekly_weekday > 6 ||
                task.weekly_hour < 0 || task.weekly_hour > 23 || task.weekly_minute < 0 ||
                task.weekly_minute > 59)
                return false;
            task.recurring_active = !finished;
            return true;
    }
    return false;
}

}  // namespace

bool ReminderManager::LoadLocked() {
    tasks_.clear();
    next_id_ = 1;
    Settings settings(kNvsNamespace, false);
    const std::string raw = settings.GetString(kNvsKey);
    if (raw.empty()) return true;
    CJsonUniquePtr root(cJSON_Parse(raw.c_str()));
    if (root == nullptr || !cJSON_IsObject(root.get())) {
        ESP_LOGW(TAG, "Discarding stored reminders: value is not a JSON object");
        return false;
    }
    const cJSON* version = cJSON_GetObjectItem(root.get(), "version");
    if (!cJSON_IsNumber(version) || version->valueint != kJsonVersion) {
        ESP_LOGW(TAG, "Discarding stored reminders: unsupported version");
        return false;
    }
    const cJSON* array = cJSON_GetObjectItem(root.get(), "tasks");
    if (!cJSON_IsArray(array)) {
        ESP_LOGW(TAG, "Discarding stored reminders: \"tasks\" is not an array");
        return false;
    }
    for (const cJSON* item = array->child; item != nullptr; item = item->next) {
        if (tasks_.size() >= kMaxRestoreEntries) {
            ESP_LOGW(TAG, "Stored reminder list is longer than %u entries, ignoring the rest",
                     static_cast<unsigned>(kMaxRestoreEntries));
            break;
        }
        ReminderTask task;
        if (!ParseStoredTask(item, task)) {
            ESP_LOGW(TAG, "Ignoring an invalid stored reminder");
            continue;
        }
        tasks_.push_back(task);
        next_id_ = std::max(next_id_, task.id + 1);
    }
    return true;
}

// Keep the persisted state compatible with the stable firmware: an unresolved response is
// stored as "playing", which older firmware already recovers as due after reboot. The model-facing
// task list still exposes the richer "awaiting_response" runtime state through StateName().
static const char* StoredStateName(ReminderState state) {
    return state == ReminderState::kAwaitingResponse ? "playing"
                                                      : ReminderManager::StateName(state);
}

// Serializes tasks_ to JSON. Returns false on allocation or serialization failure.
static bool BuildTasksJson(const std::vector<ReminderTask>& tasks, std::string& out) {
    CJsonUniquePtr root(cJSON_CreateObject());
    CJsonUniquePtr array(cJSON_CreateArray());
    if (root == nullptr || array == nullptr) return false;
    cJSON_AddNumberToObject(root.get(), "version", kJsonVersion);
    for (const auto& task : tasks) {
        CJsonUniquePtr item(cJSON_CreateObject());
        if (item == nullptr) return false;
        cJSON_AddNumberToObject(item.get(), "id", task.id);
        cJSON_AddStringToObject(item.get(), "text", task.text.c_str());
        cJSON_AddStringToObject(item.get(), "type", ReminderManager::TypeName(task.type));
        cJSON_AddStringToObject(item.get(), "state", StoredStateName(task.state));
        cJSON_AddBoolToObject(item.get(), "recurring_active", task.recurring_active);
        cJSON_AddNumberToObject(item.get(), "trigger_ms", static_cast<double>(task.trigger_ms));
        cJSON_AddNumberToObject(item.get(), "trigger_unix", static_cast<double>(task.trigger_unix));
        cJSON_AddNumberToObject(item.get(), "created_ms", static_cast<double>(task.created_ms));
        cJSON_AddNumberToObject(item.get(), "retry_count", task.retry_count);
        cJSON_AddNumberToObject(item.get(), "last_fired_unix",
                                static_cast<double>(task.last_fired_unix));
        cJSON_AddNumberToObject(item.get(), "daily_hour", task.daily_hour);
        cJSON_AddNumberToObject(item.get(), "daily_minute", task.daily_minute);
        cJSON_AddNumberToObject(item.get(), "weekly_weekday", task.weekly_weekday);
        cJSON_AddNumberToObject(item.get(), "weekly_hour", task.weekly_hour);
        cJSON_AddNumberToObject(item.get(), "weekly_minute", task.weekly_minute);
        cJSON_AddItemToArray(array.get(), item.release());
    }
    cJSON_AddItemToObject(root.get(), "tasks", array.release());
    CJsonStringUniquePtr json(cJSON_PrintUnformatted(root.get()));
    if (json == nullptr) return false;
    out.assign(json.get());
    return true;
}

// Drops the oldest finished tasks (keeping the newest id as the monotonic anchor) until the
// list fits below the limit, then tries to persist again. Returns true only when a write succeeded.
static bool PruneAndSave(std::vector<ReminderTask>& tasks) {
    while (true) {
        std::string json;
        if (!BuildTasksJson(tasks, json)) {
            ESP_LOGE(TAG, "Failed to persist reminders: serialization failed");
            return false;
        }
        Settings settings(kNvsNamespace, true);
        if (settings.SetString(kNvsKey, json)) {
            return true;
        }
        // The write failed, most likely because the NVS partition is full: drop the oldest
        // finished task and retry. If nothing can be dropped, give up instead of aborting.
        uint32_t newest_id = 0;
        for (const auto& task : tasks) {
            newest_id = std::max(newest_id, task.id);
        }
        auto victim = tasks.end();
        for (auto it = tasks.begin(); it != tasks.end(); ++it) {
            const bool finished =
                it->state == ReminderState::kCompleted || it->state == ReminderState::kCancelled;
            if (finished && it->id != newest_id) {
                victim = it;
                break;
            }
        }
        if (victim == tasks.end()) {
            ESP_LOGE(TAG, "Failed to persist reminders: NVS full and nothing left to drop");
            return false;
        }
        ESP_LOGW(TAG, "NVS full, dropping finished reminder %u to free space",
                 static_cast<unsigned>(victim->id));
        tasks.erase(victim);
    }
}

void ReminderManager::SaveLocked() {
    if (!PruneAndSave(tasks_)) {
        ESP_LOGE(TAG, "Reminder changes could not be persisted");
    }
}

void ReminderManager::Init() {
    std::lock_guard<std::mutex> lock(mutex_);
    LoadLocked();

    const uint64_t now_ms = NowMonotonicMs();
    const bool time_synced = IsSystemTimeSynced();
    const int64_t now_unix = time_synced ? NowUnixSeconds() : 0;
    bool changed = false;
    for (auto& task : tasks_) {
        if (task.state == ReminderState::kPlaying ||
            task.state == ReminderState::kAwaitingResponse) {
            task.state = ReminderState::kDue;
            changed = true;
            ESP_LOGW(TAG, "Reminder %u was active at restart, marking it due", task.id);
            continue;
        }
        if (task.state != ReminderState::kPending) continue;
        if (task.type == ReminderType::kRelative) {
            // trigger_ms counts from esp_timer_get_time(), which restarts at 0 on every boot, so a
            // stored deadline cannot be compared with the new uptime. Re-arm the original delay
            // from now instead: the reminder may be up to one delay late, but never hours late.
            const uint64_t delay_ms = task.trigger_ms > task.created_ms
                                          ? task.trigger_ms - task.created_ms
                                          : task.trigger_ms;
            task.trigger_ms = now_ms + delay_ms;
            task.created_ms = now_ms;
            changed = true;
            ESP_LOGI(TAG, "Reminder %u re-armed for %llu ms after boot", task.id,
                     static_cast<unsigned long long>(delay_ms));
            continue;
        }
        bool expired = false;
        int64_t overdue_seconds = 0;
        if (time_synced && task.trigger_unix > 0) {
            expired = now_unix >= task.trigger_unix;
            if (expired) overdue_seconds = now_unix - task.trigger_unix;
        }
        if (!expired) continue;
        if (overdue_seconds <= kLateGraceSeconds) {
            task.state = ReminderState::kDue;
            changed = true;
            ESP_LOGI(TAG, "Reminder %u expired while the device was off, it will play now",
                     task.id);
            continue;
        }
        if (IsRecurring(task.type)) {
            const int64_t next = ComputeNextTrigger(task, now_unix);
            if (next > 0) {
                task.trigger_unix = next;
                task.recurring_active = true;
                changed = true;
                ESP_LOGW(TAG, "Reminder %u missed, next run scheduled", task.id);
            } else {
                task.state = ReminderState::kCancelled;
                task.recurring_active = false;
                changed = true;
                ESP_LOGE(TAG, "Reminder %u has no future occurrence, cancelled", task.id);
            }
        } else {
            task.state = ReminderState::kCompleted;
            task.recurring_active = false;
            changed = true;
            ESP_LOGW(TAG, "One-shot reminder %u expired while the device was off, marked completed",
                     task.id);
        }
    }
    if (changed) SaveLocked();
    ESP_LOGI(TAG, "Loaded %u reminder task(s)", static_cast<unsigned>(tasks_.size()));
}

ReminderTask* ReminderManager::FindLocked(uint32_t id) {
    for (auto& task : tasks_) {
        if (task.id == id) return &task;
    }
    return nullptr;
}
const ReminderTask* ReminderManager::FindLocked(uint32_t id) const {
    for (const auto& task : tasks_) {
        if (task.id == id) return &task;
    }
    return nullptr;
}
size_t ReminderManager::ActiveCountLocked() const {
    size_t count = 0;
    for (const auto& task : tasks_) {
        if (task.state != ReminderState::kCompleted && task.state != ReminderState::kCancelled)
            ++count;
    }
    return count;
}

void ReminderManager::PruneFinishedLocked() {
    while (tasks_.size() >= kMaxTasks) {
        uint32_t newest_id = 0;
        for (const auto& task : tasks_) newest_id = std::max(newest_id, task.id);
        auto victim = tasks_.end();
        for (auto it = tasks_.begin(); it != tasks_.end(); ++it) {
            const bool finished =
                it->state == ReminderState::kCompleted || it->state == ReminderState::kCancelled;
            if (finished && it->id != newest_id) {
                victim = it;
                break;
            }
        }
        if (victim == tasks_.end()) return;
        tasks_.erase(victim);
    }
}

uint32_t ReminderManager::CreateReminder(const std::string& text, ReminderType type,
                                         uint64_t delay_ms, int64_t trigger_unix, int daily_hour,
                                         int daily_minute, int weekly_weekday, int weekly_hour,
                                         int weekly_minute, std::string& out_error) {
    out_error.clear();
    if (text.empty() || IsBlank(text)) {
        out_error = "empty-text";
        return 0;
    }
    if (text.size() > kMaxTextBytes) {
        out_error = "text-too-long";
        return 0;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    if (ActiveCountLocked() >= kMaxTasks) {
        out_error = "task-limit-reached";
        return 0;
    }
    ReminderTask task;
    task.text = text;
    task.state = ReminderState::kPending;
    task.created_ms = NowMonotonicMs();
    task.last_fired_unix = 0;
    out_error = BuildSchedule(type, delay_ms, trigger_unix, daily_hour, daily_minute, weekly_weekday,
                              weekly_hour, weekly_minute, task.created_ms, task);
    if (!out_error.empty()) return 0;

    PruneFinishedLocked();
    task.id = next_id_++;
    if (next_id_ == 0) next_id_ = 1;
    const uint32_t id = task.id;
    tasks_.push_back(task);
    SaveLocked();
    ESP_LOGI(TAG, "Created reminder %u (%s): %s", id, TypeName(type), text.c_str());
    return id;
}

bool ReminderManager::UpdateReminder(uint32_t id, const std::string& text, ReminderType type,
                                     uint64_t delay_ms, int64_t trigger_unix, int daily_hour,
                                     int daily_minute, int weekly_weekday, int weekly_hour,
                                     int weekly_minute, std::string& out_error) {
    out_error.clear();
    if (!text.empty() && IsBlank(text)) {
        out_error = "empty-text";
        return false;
    }
    if (text.size() > kMaxTextBytes) {
        out_error = "text-too-long";
        return false;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    ReminderTask* task = FindLocked(id);
    if (task == nullptr || task->state == ReminderState::kCancelled) {
        out_error = "not-found";
        return false;
    }
    ReminderTask updated = *task;
    out_error = BuildSchedule(type, delay_ms, trigger_unix, daily_hour, daily_minute, weekly_weekday,
                              weekly_hour, weekly_minute, updated.created_ms, updated);
    if (!out_error.empty()) return false;
    if (!text.empty()) updated.text = text;
    updated.state = ReminderState::kPending;
    updated.retry_not_before_ms = 0;
    updated.retry_count = 0;
    *task = updated;
    SaveLocked();
    ESP_LOGI(TAG, "Updated reminder %u (%s): %s", id, TypeName(type), updated.text.c_str());
    return true;
}

bool ReminderManager::CancelReminder(uint32_t id, std::string& out_error) {
    out_error.clear();
    std::lock_guard<std::mutex> lock(mutex_);
    ReminderTask* task = FindLocked(id);
    if (task == nullptr || task->state == ReminderState::kCancelled) {
        out_error = "not-found";
        return false;
    }
    task->state = ReminderState::kCancelled;
    task->recurring_active = false;
    SaveLocked();
    ESP_LOGI(TAG, "Cancelled reminder %u", id);
    return true;
}

bool ReminderManager::CancelAll(std::string& out_error) {
    out_error.clear();
    std::lock_guard<std::mutex> lock(mutex_);
    bool changed = false;
    for (auto& task : tasks_) {
        if (task.state == ReminderState::kCancelled) continue;
        task.state = ReminderState::kCancelled;
        task.recurring_active = false;
        changed = true;
    }
    if (changed) SaveLocked();
    ESP_LOGI(TAG, "Cancelled all reminders (%u task(s))", static_cast<unsigned>(tasks_.size()));
    return true;
}

std::vector<ReminderTask> ReminderManager::ListReminders() const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<ReminderTask> result;
    result.reserve(tasks_.size());
    for (const auto& task : tasks_) {
        if (task.state != ReminderState::kCancelled) result.push_back(task);
    }
    std::sort(result.begin(), result.end(),
              [](const ReminderTask& lhs, const ReminderTask& rhs) { return lhs.id < rhs.id; });
    return result;
}

size_t ReminderManager::MarkDueTasks() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (tasks_.empty()) return 0;
    const uint64_t now_ms = NowMonotonicMs();
    int64_t now_unix = NowUnixSeconds();
    if (now_unix <= kMinTrustedUnix) now_unix = 0;
    size_t marked = 0;
    for (auto& task : tasks_) {
        if (task.state != ReminderState::kPending) continue;
        bool due = false;
        if (task.type == ReminderType::kRelative) {
            due = now_ms >= task.trigger_ms;
        } else if (now_unix > 0 && task.trigger_unix > 0) {
            due = now_unix >= task.trigger_unix;
        }
        if (!due) continue;
        task.state = ReminderState::kDue;
        if (!IsRecurring(task.type)) task.recurring_active = false;
        ++marked;
        ESP_LOGI(TAG, "Reminder %u is due", task.id);
    }
    if (marked > 0) SaveLocked();
    return marked;
}

bool ReminderManager::TakeNextDueTask(ReminderTask& out) {
    std::lock_guard<std::mutex> lock(mutex_);
    // An unresolved reminder owns the response channel; never start another reminder beside it.
    for (const auto& task : tasks_) {
        if (task.state == ReminderState::kAwaitingResponse) return false;
    }
    const uint64_t now_ms = NowMonotonicMs();
    const ReminderTask* next = nullptr;
    for (const auto& task : tasks_) {
        if (task.state != ReminderState::kDue || task.retry_not_before_ms > now_ms) continue;
        if (next == nullptr || task.id < next->id) next = &task;
    }
    if (next == nullptr) return false;
    out = *next;
    return true;
}

void ReminderManager::MarkPlaying(uint32_t id) {
    std::lock_guard<std::mutex> lock(mutex_);
    ReminderTask* task = FindLocked(id);
    if (task == nullptr) {
        ESP_LOGW(TAG, "Reminder %u no longer exists, cannot start playing it", id);
        return;
    }
    if (task->state != ReminderState::kDue) {
        ESP_LOGW(TAG, "Reminder %u is %s, not starting it", id, StateName(task->state));
        return;
    }
    task->state = ReminderState::kPlaying;
    task->retry_not_before_ms = 0;
    const int64_t now_unix = NowUnixSeconds();
    if (now_unix > kMinTrustedUnix) task->last_fired_unix = now_unix;
    SaveLocked();
    ESP_LOGI(TAG, "Reminder %u is playing", id);
}

void ReminderManager::MarkPlaybackFailed(uint32_t id) {
    std::lock_guard<std::mutex> lock(mutex_);
    ReminderTask* task = FindLocked(id);
    if (task == nullptr) {
        ESP_LOGW(TAG, "Reminder %u no longer exists, nothing to roll back", id);
        return;
    }
    if (task->state != ReminderState::kPlaying) {
        ESP_LOGW(TAG, "Reminder %u is %s, nothing to roll back", id, StateName(task->state));
        return;
    }
    task->state = ReminderState::kDue;
    SaveLocked();
    ESP_LOGW(TAG, "Reminder %u could not be played, it stays due", id);
}

void ReminderManager::OnPlaybackFinished(uint32_t id, PcmTtsPlayer::Error error) {
    std::lock_guard<std::mutex> lock(mutex_);
    ReminderTask* task = FindLocked(id);
    if (task == nullptr) {
        ESP_LOGW(TAG, "Reminder %u no longer exists, ignoring the playback result", id);
        return;
    }
    if (task->state != ReminderState::kPlaying) {
        ESP_LOGW(TAG, "Reminder %u is %s, ignoring the playback result", id,
                 StateName(task->state));
        return;
    }
    if (error == PcmTtsPlayer::Error::kSuccess) {
        task->state = ReminderState::kAwaitingResponse;
        ESP_LOGI(TAG, "Reminder %u playback finished; awaiting user response", id);
    } else if (error == PcmTtsPlayer::Error::kCancelled) {
        if (IsRecurring(task->type)) {
            task->state = ReminderState::kPending;
            task->recurring_active = true;
        } else {
            task->state = ReminderState::kDue;
        }
        ESP_LOGW(TAG, "Reminder %u was interrupted before it finished, it stays active", id);
    } else {
        task->state = ReminderState::kDue;
        ESP_LOGW(TAG, "Reminder %u playback failed (%s), it stays due", id,
                 PcmTtsPlayer::GetErrorName(error));
    }
    SaveLocked();
}

bool ReminderManager::GetAwaitingResponseTask(ReminderTask& out) const {
    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& task : tasks_) {
        if (task.state == ReminderState::kAwaitingResponse) {
            out = task;
            return true;
        }
    }
    return false;
}

bool ReminderManager::ResolveReminderResponse(uint32_t id, bool completed, uint64_t delay_ms,
                                                std::string& out_error) {
    std::lock_guard<std::mutex> lock(mutex_);
    ReminderTask* task = FindLocked(id);
    if (task == nullptr) {
        out_error = "reminder id not found";
        return false;
    }
    if (task->state != ReminderState::kAwaitingResponse &&
        task->state != ReminderState::kPaused) {
        out_error = "reminder is not awaiting a response or paused";
        return false;
    }
    if (task->state == ReminderState::kAwaitingResponse) {
        for (const auto& candidate : tasks_) {
            if (candidate.id != id && candidate.state == ReminderState::kAwaitingResponse) {
                out_error = "another reminder currently owns the response channel";
                return false;
            }
        }
    }

    if (!completed) {
        const uint64_t now = NowMonotonicMs();
        if (delay_ms == 0 || delay_ms > UINT64_MAX - now) {
            out_error = "invalid snooze delay";
            return false;
        }
        task->state = ReminderState::kDue;
        task->retry_not_before_ms = now + delay_ms;
        task->retry_count = 0;
        ESP_LOGI(TAG, "Reminder %u snoozed by user", task->id);
    } else if (IsRecurring(task->type)) {
        const int64_t after = std::max(task->last_fired_unix, NowUnixSeconds());
        const int64_t next = ComputeNextTrigger(*task, after);
        if (next <= 0) {
            out_error = "failed to calculate the next recurring reminder";
            return false;
        }
        task->trigger_unix = next;
        task->state = ReminderState::kPending;
        task->recurring_active = true;
        task->retry_count = 0;
        task->retry_not_before_ms = 0;
        ESP_LOGI(TAG, "Reminder %u confirmed; next recurring run scheduled", task->id);
    } else {
        task->state = ReminderState::kCompleted;
        task->recurring_active = false;
        task->retry_count = 0;
        task->retry_not_before_ms = 0;
        ESP_LOGI(TAG, "Reminder %u explicitly completed", task->id);
    }
    SaveLocked();
    out_error.clear();
    return true;
}

bool ReminderManager::RetryAwaitingResponse(uint32_t id, uint64_t delay_ms) {
    std::lock_guard<std::mutex> lock(mutex_);
    ReminderTask* task = FindLocked(id);
    if (task == nullptr || task->state != ReminderState::kAwaitingResponse) return false;
    if (task->retry_count >= 2) {
        task->state = ReminderState::kPaused;
        task->retry_not_before_ms = 0;
        SaveLocked();
        ESP_LOGW(TAG, "Reminder %u paused after two automatic retries", id);
        return false;
    }
    const uint64_t now = NowMonotonicMs();
    if (delay_ms > UINT64_MAX - now) return false;
    ++task->retry_count;
    task->state = ReminderState::kDue;
    task->retry_not_before_ms = now + delay_ms;
    SaveLocked();
    ESP_LOGI(TAG, "Reminder %u response timed out; retry %u scheduled", id,
             static_cast<unsigned>(task->retry_count));
    return true;
}

bool ReminderManager::DeferAwaitingResponse(uint32_t id, uint64_t delay_ms) {
    std::lock_guard<std::mutex> lock(mutex_);
    ReminderTask* task = FindLocked(id);
    if (task == nullptr || task->state != ReminderState::kAwaitingResponse) return false;
    const uint64_t now = NowMonotonicMs();
    if (delay_ms > UINT64_MAX - now) return false;
    task->state = ReminderState::kDue;
    task->retry_not_before_ms = now + delay_ms;
    SaveLocked();
    ESP_LOGW(TAG, "Reminder %u deferred after response-listening failure", id);
    return true;
}

