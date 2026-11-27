# CI validation trigger for the reminder response lifecycle tests.
"""Host tests for ReminderManager (the 4c timed voice reminder system).

main/reminder/reminder_manager.cc is compiled unchanged against platform stubs (esp_timer,
esp_log, Settings/NVS, FreeRTOS and AudioService) together with the vendored cJSON sources. The
driver fakes both the monotonic clock and the wall clock, so relative/absolute/daily/weekly
schedules, persistence and restart handling are checked deterministically. These tests validate
the manager's logic only -- they are NOT an NVS, audio or TTS validation.
"""

import os
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CJSON_DIR = ROOT / "managed_components" / "espressif__cjson" / "cJSON"

STUBS = {
    "esp_log.h": r'''
        #pragma once
        #include <cstdio>
        #define ESP_LOGE(tag, fmt, ...) std::printf("E %s: " fmt "\n", tag, ##__VA_ARGS__)
        #define ESP_LOGW(tag, fmt, ...) std::printf("W %s: " fmt "\n", tag, ##__VA_ARGS__)
        #define ESP_LOGI(tag, fmt, ...) std::printf("I %s: " fmt "\n", tag, ##__VA_ARGS__)
        #define ESP_LOGD(tag, fmt, ...) std::printf("D %s: " fmt "\n", tag, ##__VA_ARGS__)
    ''',
    # esp_timer_get_time() reads the driver-controlled monotonic clock.
    "esp_timer.h": r'''
        #pragma once
        #include <atomic>
        #include <cstdint>
        extern std::atomic<uint64_t> g_fake_time_us;
        inline int64_t esp_timer_get_time() {
            return static_cast<int64_t>(g_fake_time_us.load());
        }
    ''',
    "nvs_flash.h": r'''
        #pragma once
        #include <cstdint>
        typedef uint32_t nvs_handle_t;
    ''',
    # Stands in for main/settings.cc: one in-memory map keyed by "<namespace>/<key>", so the driver
    # can inspect, corrupt and clear the "NVS" contents between simulated restarts.
    "settings.h": r'''
        #pragma once
        #include <cstdint>
        #include <cstdlib>
        #include <iterator>
        #include <map>
        #include <string>

        class Settings {
        public:
            inline static std::map<std::string, std::string> store;

            Settings(const std::string& ns, bool read_write = false)
                : ns_(ns), read_write_(read_write) {}
            ~Settings() = default;

            std::string GetString(const std::string& key, const std::string& default_value = "") {
                const auto it = store.find(ns_ + "/" + key);
                return it == store.end() ? default_value : it->second;
            }
            void SetString(const std::string& key, const std::string& value) {
                store[ns_ + "/" + key] = value;
            }
            int32_t GetInt(const std::string& key, int32_t default_value = 0) {
                const auto it = store.find(ns_ + "/" + key);
                return it == store.end() ? default_value : std::atoi(it->second.c_str());
            }
            void SetInt(const std::string& key, int32_t value) {
                store[ns_ + "/" + key] = std::to_string(value);
            }
            bool GetBool(const std::string& key, bool default_value = false) {
                const auto it = store.find(ns_ + "/" + key);
                return it == store.end() ? default_value : it->second == "1";
            }
            void SetBool(const std::string& key, bool value) {
                store[ns_ + "/" + key] = value ? "1" : "0";
            }
            void EraseKey(const std::string& key) { store.erase(ns_ + "/" + key); }
            void EraseAll() {
                for (auto it = store.begin(); it != store.end();) {
                    if (it->first.rfind(ns_ + "/", 0) == 0) {
                        it = store.erase(it);
                    } else {
                        ++it;
                    }
                }
            }

        private:
            std::string ns_;
            bool read_write_ = false;
        };
    ''',
    # reminder_manager.h includes pcm_tts_player.h, which only needs the type name here: the player
    # itself is never built for these tests.
    "audio_service.h": r'''
        #pragma once
        #include <cstdint>
        class AudioService;
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
        #define portMAX_DELAY ((TickType_t)0xFFFFFFFFU)
    ''',
    "freertos/task.h": r'''
        #pragma once
        #include <freertos/FreeRTOS.h>
    ''',
    # mcp_server.h declares Property, whose constructors abort on an inconsistent schema, so the
    # tool translation unit needs the declaration to build.
    "esp_system.h": r'''
        #pragma once
        #include <cstdio>
        #include <cstdlib>
        [[noreturn]] inline void esp_system_abort(const char* message) {
            std::fprintf(stderr, "esp_system_abort: %s\n", message);
            std::abort();
        }
    ''',
    # ImageContent base64-encodes its payload; the tools never build one, so the declaration is
    # enough to compile them.
    "mbedtls/base64.h": r'''
        #pragma once
        #include <cstddef>
        int mbedtls_base64_encode(unsigned char* dst, size_t dlen, size_t* olen,
                                  const unsigned char* src, size_t slen);
    ''',
}

DRIVER_HEAD = r'''
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <memory>
#include <string>
#include <vector>

#include <esp_timer.h>

#include "reminder_manager.h"
#include "settings.h"

// Simulated clocks: g_fake_time_us is the monotonic uptime in microseconds (esp_timer_get_time),
// g_fake_unix is what time(nullptr) returns.
std::atomic<uint64_t> g_fake_time_us{0};
std::atomic<int64_t> g_fake_unix{0};

// Replaces the CRT time() for this executable, which is how the firmware reads the wall clock.
extern "C" time_t time(time_t* out) noexcept {
    const auto value = static_cast<time_t>(g_fake_unix.load());
    if (out != nullptr) {
        *out = value;
    }
    return value;
}

// reminder_manager.cc logs each playback result through the player's name table. The player itself
// is not linked here (pcm_tts_player.cc needs the whole AudioService), so only that one helper is
// supplied; test_pcm_tts_player.py checks the real table.
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

namespace {

int g_failures = 0;

// 2026-01-01T00:00:00Z (a Thursday), the first "trusted" wall clock value of the scenarios.
constexpr int64_t kJan1 = 1767225600;
constexpr int64_t kDay = 86400;
constexpr int64_t kHour = 3600;

void Check(bool condition, const std::string& name, const std::string& detail = "") {
    std::printf("CHECK %s %s %s\n", condition ? "PASS" : "FAIL", name.c_str(), detail.c_str());
    if (!condition) {
        ++g_failures;
    }
}

void Finish(const char* scenario, int failures_before) {
    std::printf("RESULT %s %s\n", scenario, g_failures == failures_before ? "PASS" : "FAIL");
}

void ResetState() {
    Settings::store.clear();
    g_fake_time_us = 0;
    g_fake_unix = 0;
}

std::string StateOf(const ReminderTask& task) { return ReminderManager::StateName(task.state); }
std::string TypeOf(const ReminderTask& task) { return ReminderManager::TypeName(task.type); }
std::string Num(int64_t value) { return std::to_string(value); }

void RunRelativeAndDue();
void RunAbsoluteAndTimeSync();
void RunMultiTaskAndOrdering();
void RunPlayingLifecycle();
void RunResponseLifecycle();
void RunPlaybackFailedRetry();
void RunCancel();
void RunUpdate();
void RunList();
void RunDailyRecurring();
void RunWeeklyRecurring();
void RunPersistAndRestore();
void RunInvalidInput();

}  // namespace
'''

DRIVER_SCENARIOS_A = r'''
namespace {

// 1. A relative reminder is pending until its deadline, then due exactly once.
void RunRelativeAndDue() {
    const int before = g_failures;
    std::printf("SCENARIO relative_and_due\n");
    ResetState();
    g_fake_time_us = 1000ULL * 1000;  // 1 s uptime
    ReminderManager manager;
    std::string error;

    const uint32_t id = manager.CreateReminder("喝水", ReminderType::kRelative, 60000, 0, 0, 0, 0, 0,
                                               0, error);
    Check(id == 1, "relative/created_id_is_1", std::to_string(id));
    Check(error.empty(), "relative/no_error", error);
    std::vector<ReminderTask> tasks = manager.ListReminders();
    Check(tasks.size() == 1, "relative/one_task", std::to_string(tasks.size()));
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kPending,
          "relative/pending_initially", tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(tasks.size() == 1 && tasks[0].type == ReminderType::kRelative, "relative/type_relative",
          tasks.empty() ? "missing" : TypeOf(tasks[0]));
    Check(tasks.size() == 1 && tasks[0].text == "喝水", "relative/text_kept",
          tasks.empty() ? "missing" : tasks[0].text);
    Check(tasks.size() == 1 && tasks[0].trigger_ms == 61000, "relative/trigger_ms",
          tasks.empty() ? "missing" : Num(static_cast<int64_t>(tasks[0].trigger_ms)));
    Check(tasks.size() == 1 && !tasks[0].recurring_active, "relative/not_recurring", "");

    Check(manager.MarkDueTasks() == 0, "relative/not_due_immediately", "");
    g_fake_time_us += 59000ULL * 1000;  // 59 s later
    Check(manager.MarkDueTasks() == 0, "relative/not_due_at_59s", "");
    g_fake_time_us += 1000ULL * 1000;  // exactly 60 s after creation
    Check(manager.MarkDueTasks() == 1, "relative/due_at_60s", "");
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kDue, "relative/state_due",
          tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(manager.MarkDueTasks() == 0, "relative/due_is_idempotent", "");
    Finish("relative_and_due", before);
}

// 2. Absolute/daily/weekly reminders need a synced wall clock; relative ones do not.
void RunAbsoluteAndTimeSync() {
    const int before = g_failures;
    std::printf("SCENARIO absolute_and_time_sync\n");
    ResetState();
    ReminderManager manager;
    std::string error;
    const int64_t trigger = kJan1 + 2 * kHour;  // 2026-01-01T02:00:00Z

    Check(manager.CreateReminder("绝对", ReminderType::kAbsolute, 0, trigger, 0, 0, 0, 0, 0,
                                 error) == 0,
          "sync/absolute_rejected_before_sync", "");
    Check(error == "time-not-synced", "sync/absolute_error_code", error);
    Check(manager.CreateReminder("每天", ReminderType::kDaily, 0, 0, 9, 0, 0, 0, 0, error) == 0,
          "sync/daily_rejected_before_sync", "");
    Check(error == "time-not-synced", "sync/daily_error_code", error);
    Check(manager.CreateReminder("每周", ReminderType::kWeekly, 0, 0, 9, 0, 1, 9, 0, error) == 0,
          "sync/weekly_rejected_before_sync", "");
    Check(error == "time-not-synced", "sync/weekly_error_code", error);
    Check(manager.ListReminders().empty(), "sync/nothing_saved", "");

    const uint32_t relative_id =
        manager.CreateReminder("相对", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0, error);
    Check(relative_id > 0, "sync/relative_allowed_without_clock", error);

    g_fake_unix = kJan1;  // SNTP finished
    const uint32_t absolute_id =
        manager.CreateReminder("绝对", ReminderType::kAbsolute, 0, trigger, 0, 0, 0, 0, 0, error);
    Check(absolute_id > 0, "sync/absolute_allowed_after_sync", error);
    std::vector<ReminderTask> tasks = manager.ListReminders();
    Check(tasks.size() == 2, "sync/two_tasks", std::to_string(tasks.size()));
    if (tasks.size() == 2) {
        Check(tasks[1].trigger_unix == trigger, "sync/absolute_trigger_unix",
              Num(tasks[1].trigger_unix));
        Check(tasks[1].state == ReminderState::kPending, "sync/absolute_pending",
              StateOf(tasks[1]));
        Check(!tasks[1].recurring_active, "sync/absolute_not_recurring", "");
        Check(tasks[1].trigger_ms == 0, "sync/absolute_has_no_monotonic_trigger",
              Num(static_cast<int64_t>(tasks[1].trigger_ms)));
    }
    Check(manager.MarkDueTasks() == 0, "sync/absolute_not_due_before_trigger", "");
    g_fake_unix = trigger;
    Check(manager.MarkDueTasks() == 1, "sync/absolute_due_at_trigger", "");
    Finish("absolute_and_time_sync", before);
}

}  // namespace
'''
DRIVER_SCENARIOS_B = r'''
namespace {

// 3. Several tasks due at once are handed out one by one, lowest id first.
void RunMultiTaskAndOrdering() {
    const int before = g_failures;
    std::printf("SCENARIO multi_task_and_ordering\n");
    ResetState();
    ReminderManager manager;
    std::string error;

    const uint32_t first = manager.CreateReminder("第一", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0,
                                                 0, error);
    const uint32_t second = manager.CreateReminder("第二", ReminderType::kRelative, 1000, 0, 0, 0, 0,
                                                  0, 0, error);
    const uint32_t third = manager.CreateReminder("第三", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0,
                                                 0, error);
    Check(first == 1 && second == 2 && third == 3, "multi/ids_increase",
          std::to_string(first) + "," + std::to_string(second) + "," + std::to_string(third));
    Check(manager.ListReminders().size() == 3, "multi/three_tasks", "");

    ReminderTask task;
    Check(!manager.TakeNextDueTask(task), "multi/nothing_before_deadline", "");
    g_fake_time_us = 2000ULL * 1000;
    Check(manager.MarkDueTasks() == 3, "multi/all_due", "");

    // TakeNextDueTask() is read-only, so the same task keeps coming back until MarkPlaying() runs.
    Check(manager.TakeNextDueTask(task) && task.id == first, "multi/first_taken",
          std::to_string(task.id));
    Check(manager.TakeNextDueTask(task) && task.id == first, "multi/take_is_read_only",
          std::to_string(task.id));
    manager.MarkPlaying(first);
    Check(manager.TakeNextDueTask(task) && task.id == second, "multi/second_taken",
          std::to_string(task.id));
    manager.MarkPlaying(second);
    Check(manager.TakeNextDueTask(task) && task.id == third, "multi/third_taken",
          std::to_string(task.id));
    manager.MarkPlaying(third);
    Check(!manager.TakeNextDueTask(task), "multi/queue_empty", "");
    Finish("multi_task_and_ordering", before);
}

}  // namespace
'''
DRIVER_SCENARIOS_C = r'''
namespace {

// 4. Successful playback opens a response window; only explicit confirmation completes a task.
void RunPlayingLifecycle() {
    const int before = g_failures;
    std::printf("SCENARIO playing_lifecycle\n");
    ResetState();
    g_fake_unix = kJan1;
    ReminderManager manager;
    std::string error;

    const uint32_t one_shot =
        manager.CreateReminder("一次性", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0, error);
    const uint32_t daily =
        manager.CreateReminder("每天", ReminderType::kDaily, 0, 0, 9, 0, 0, 0, 0, error);
    Check(one_shot == 1 && daily == 2, "lifecycle/created",
          std::to_string(one_shot) + "," + std::to_string(daily));

    g_fake_time_us = 2000ULL * 1000;
    g_fake_unix = kJan1 + 9 * kHour;
    Check(manager.MarkDueTasks() == 2, "lifecycle/both_due", "");

    ReminderTask task;
    Check(manager.TakeNextDueTask(task) && task.id == one_shot, "lifecycle/one_shot_first", "");
    manager.MarkPlaying(one_shot);
    std::vector<ReminderTask> tasks = manager.ListReminders();
    Check(tasks.size() == 2 && tasks[0].state == ReminderState::kPlaying,
          "lifecycle/playing_state", tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(tasks.size() == 2 && tasks[0].last_fired_unix == kJan1 + 9 * kHour,
          "lifecycle/last_fired_recorded",
          tasks.empty() ? "missing" : Num(tasks[0].last_fired_unix));
    Check(manager.TakeNextDueTask(task) && task.id == daily, "lifecycle/playing_task_skipped", "");

    manager.OnPlaybackFinished(one_shot, PcmTtsPlayer::Error::kSuccess);
    tasks = manager.ListReminders();
    Check(tasks.size() == 2 && tasks[0].state == ReminderState::kAwaitingResponse,
          "lifecycle/one_shot_awaiting_response", tasks.empty() ? "missing" : StateOf(tasks[0]));
    ReminderTask awaiting;
    Check(manager.GetAwaitingResponseTask(awaiting) && awaiting.id == one_shot,
          "lifecycle/active_response_task", "");
    Check(manager.ResolveAwaitingResponse(true, 0, error), "lifecycle/explicit_complete", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 2 && tasks[0].state == ReminderState::kCompleted,
          "lifecycle/one_shot_completed", tasks.empty() ? "missing" : StateOf(tasks[0]));

    Check(manager.TakeNextDueTask(task) && task.id == daily, "lifecycle/daily_taken", "");
    manager.MarkPlaying(daily);
    manager.OnPlaybackFinished(daily, PcmTtsPlayer::Error::kSuccess);
    tasks = manager.ListReminders();
    Check(tasks.size() == 2 && tasks[1].state == ReminderState::kAwaitingResponse,
          "lifecycle/daily_awaiting_response", tasks.size() < 2 ? "missing" : StateOf(tasks[1]));
    Check(manager.ResolveAwaitingResponse(true, 0, error), "lifecycle/daily_confirmed", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 2 && tasks[1].state == ReminderState::kPending,
          "lifecycle/daily_back_to_pending", tasks.size() < 2 ? "missing" : StateOf(tasks[1]));
    Check(tasks.size() == 2 && tasks[1].trigger_unix == kJan1 + 9 * kHour + kDay,
          "lifecycle/daily_next_day", tasks.size() < 2 ? "missing" : Num(tasks[1].trigger_unix));
    Check(tasks.size() == 2 && tasks[1].recurring_active, "lifecycle/daily_still_active", "");
    Check(!manager.TakeNextDueTask(task), "lifecycle/queue_empty_after_success", "");
    Finish("playing_lifecycle", before);
}

}  // namespace
'''
DRIVER_SCENARIOS_D = r'''
namespace {

// 5. Snooze and no-response retries delay only that task; other due reminders can proceed.
void RunResponseLifecycle() {
    const int before = g_failures;
    std::printf("SCENARIO response_lifecycle\n");
    ResetState();
    ReminderManager manager;
    std::string error;

    const uint32_t first = manager.CreateReminder("先处理第一件事", ReminderType::kRelative,
                                                  1000, 0, 0, 0, 0, 0, 0, error);
    const uint32_t second = manager.CreateReminder("第二件事", ReminderType::kRelative,
                                                   1000, 0, 0, 0, 0, 0, 0, error);
    g_fake_time_us = 2000ULL * 1000;
    Check(manager.MarkDueTasks() == 2, "response/both_due", "");

    ReminderTask task;
    Check(manager.TakeNextDueTask(task) && task.id == first, "response/first_taken", "");
    manager.MarkPlaying(first);
    manager.OnPlaybackFinished(first, PcmTtsPlayer::Error::kSuccess);
    Check(manager.ResolveAwaitingResponse(false, 5000, error), "response/snoozed", error);
    auto tasks = manager.ListReminders();
    Check(tasks.size() == 2 && tasks[0].state == ReminderState::kDue,
          "response/snooze_returns_due", tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(!manager.GetAwaitingResponseTask(task), "response/no_longer_awaiting", "");

    // The second task must not be blocked by the first task's snooze deadline.
    Check(manager.TakeNextDueTask(task) && task.id == second, "response/second_not_blocked", "");
    manager.MarkPlaying(second);
    manager.OnPlaybackFinished(second, PcmTtsPlayer::Error::kSuccess);
    Check(manager.ResolveAwaitingResponse(true, 0, error), "response/second_completed", error);

    // The first task remains unavailable until its five-second snooze expires.
    Check(!manager.TakeNextDueTask(task), "response/snooze_gate_active", "");
    g_fake_time_us += 5000ULL * 1000;
    Check(manager.TakeNextDueTask(task) && task.id == first, "response/snooze_expired", "");
    manager.MarkPlaying(first);
    manager.OnPlaybackFinished(first, PcmTtsPlayer::Error::kSuccess);

    // No response returns it to due but applies a fresh retry delay.
    Check(manager.RetryAwaitingResponse(first, 3000), "response/timeout_retry_scheduled", "");
    Check(!manager.TakeNextDueTask(task), "response/timeout_gate_active", "");
    g_fake_time_us += 3000ULL * 1000;
    Check(manager.TakeNextDueTask(task) && task.id == first, "response/timeout_retried", "");
    manager.MarkPlaying(first);
    manager.OnPlaybackFinished(first, PcmTtsPlayer::Error::kSuccess);
    Check(manager.ResolveAwaitingResponse(true, 0, error), "response/final_confirmation", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 2 && tasks[0].state == ReminderState::kCompleted,
          "response/first_completed_only_after_confirmation",
          tasks.empty() ? "missing" : StateOf(tasks[0]));
    Finish("response_lifecycle", before);
}

// 6. A failed or cancelled utterance leaves the task retryable instead of losing it.
void RunPlaybackFailedRetry() {
    const int before = g_failures;
    std::printf("SCENARIO playback_failed_retry\n");
    ResetState();
    ReminderManager manager;
    std::string error;

    const uint32_t id = manager.CreateReminder("重试", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0,
                                               error);
    g_fake_time_us = 2000ULL * 1000;
    Check(manager.MarkDueTasks() == 1, "retry/due", "");

    // StartPcmTtsSession() failed: the task never became kPlaying, so it must stay due for the next
    // tick and the failed start must not be mistaken for a played reminder.
    ReminderTask task;
    Check(manager.TakeNextDueTask(task) && task.id == id, "retry/taken_before_start", "");
    manager.MarkPlaybackFailed(id);
    std::vector<ReminderTask> tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kDue,
          "retry/failed_start_keeps_due", tasks.empty() ? "missing" : StateOf(tasks[0]));

    // An HTTP/format error reported by the player: back to due, playable again.
    Check(manager.TakeNextDueTask(task) && task.id == id, "retry/taken_again", "");
    manager.MarkPlaying(id);
    manager.OnPlaybackFinished(id, PcmTtsPlayer::Error::kHttpFailed);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kDue, "retry/http_error_back_to_due",
          tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(manager.MarkDueTasks() == 0, "retry/no_duplicate_due_transition", "");
    Check(manager.TakeNextDueTask(task) && task.id == id, "retry/due_task_retaken",
          std::to_string(task.id));

    // A cancelled session is retried later as well, and completion needs an explicit success.
    manager.MarkPlaying(id);
    manager.OnPlaybackFinished(id, PcmTtsPlayer::Error::kCancelled);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kDue, "retry/cancelled_back_to_due",
          tasks.empty() ? "missing" : StateOf(tasks[0]));

    manager.MarkPlaying(id);
    manager.OnPlaybackFinished(id, PcmTtsPlayer::Error::kSuccess);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kAwaitingResponse,
          "retry/success_awaits_response", tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(manager.ResolveAwaitingResponse(true, 0, error), "retry/explicit_completion", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kCompleted,
          "retry/confirmed_completed", tasks.empty() ? "missing" : StateOf(tasks[0]));
    Finish("playback_failed_retry", before);
}

}  // namespace
'''
DRIVER_SCENARIOS_E = r'''
namespace {

// 6. Cancelling removes a task from the list and from the due queue, once per id.
void RunCancel() {
    const int before = g_failures;
    std::printf("SCENARIO cancel\n");
    ResetState();
    ReminderManager manager;
    std::string error;

    const uint32_t due_id =
        manager.CreateReminder("已到期", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0, error);
    const uint32_t future_id =
        manager.CreateReminder("未来", ReminderType::kRelative, 60000, 0, 0, 0, 0, 0, 0, error);
    Check(due_id == 1 && future_id == 2, "cancel/created", "");

    g_fake_time_us = 2000ULL * 1000;
    Check(manager.MarkDueTasks() == 1, "cancel/one_due", "");
    Check(manager.CancelReminder(due_id, error), "cancel/returns_true", error);
    Check(error.empty(), "cancel/no_error", error);

    std::vector<ReminderTask> tasks = manager.ListReminders();
    Check(tasks.size() == 1, "cancel/removed_from_list", std::to_string(tasks.size()));
    Check(tasks.size() == 1 && tasks[0].id == future_id, "cancel/other_task_kept",
          tasks.empty() ? "missing" : std::to_string(tasks[0].id));
    ReminderTask task;
    Check(!manager.TakeNextDueTask(task), "cancel/cancelled_task_not_played", "");

    Check(!manager.CancelReminder(9999, error) && error == "not-found", "cancel/unknown_id", error);
    error.clear();
    Check(!manager.CancelReminder(due_id, error) && error == "not-found",
          "cancel/second_cancel_rejected", error);
    error.clear();
    Check(!manager.UpdateReminder(due_id, "复活", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0,
                                  error) &&
              error == "not-found",
          "cancel/cancelled_task_cannot_be_updated", error);

    error.clear();
    Check(manager.CancelAll(error), "cancel/cancel_all_returns_true", error);
    Check(manager.ListReminders().empty(), "cancel/all_list_empty", "");
    error.clear();
    Check(manager.CancelAll(error), "cancel/cancel_all_on_empty_list", error);
    Finish("cancel", before);
}

// 7. Updating reschedules a task, keeps the old text when none is given and survives bad input.
void RunUpdate() {
    const int before = g_failures;
    std::printf("SCENARIO update\n");
    ResetState();
    ReminderManager manager;
    std::string error;

    const uint32_t id = manager.CreateReminder("原始提醒", ReminderType::kRelative, 60000, 0, 0, 0, 0, 0,
                                               0, error);
    Check(id == 1, "update/created", error);
    Check(manager.UpdateReminder(id, "新提醒", ReminderType::kRelative, 120000, 0, 0, 0, 0, 0, 0, error),
          "update/returns_true", error);
    std::vector<ReminderTask> tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].text == "新提醒", "update/text_replaced",
          tasks.empty() ? "missing" : tasks[0].text);
    Check(tasks.size() == 1 && tasks[0].trigger_ms == tasks[0].created_ms + 120000,
          "update/trigger_from_created_ms",
          tasks.empty() ? "missing" : Num(static_cast<int64_t>(tasks[0].trigger_ms)));
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kPending &&
              tasks[0].type == ReminderType::kRelative,
          "update/stays_pending_relative", tasks.empty() ? "missing" : StateOf(tasks[0]));

    Check(manager.UpdateReminder(id, "", ReminderType::kRelative, 30000, 0, 0, 0, 0, 0, 0, error),
          "update/empty_text_accepted", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].text == "新提醒", "update/empty_text_keeps_old",
          tasks.empty() ? "missing" : tasks[0].text);
    Check(tasks.size() == 1 && tasks[0].trigger_ms == tasks[0].created_ms + 30000,
          "update/trigger_shortened", "");

    g_fake_time_us = 30000ULL * 1000;
    Check(manager.MarkDueTasks() == 1, "update/task_due", "");
    Check(manager.UpdateReminder(id, "", ReminderType::kRelative, 90000, 0, 0, 0, 0, 0, 0, error),
          "update/due_task_rescheduled", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kPending,
          "update/due_task_back_to_pending", tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(tasks.size() == 1 && tasks[0].trigger_ms == tasks[0].created_ms + 90000,
          "update/trigger_recomputed", "");
    ReminderTask task;
    Check(!manager.TakeNextDueTask(task), "update/rescheduled_task_not_due", "");

    Check(!manager.UpdateReminder(9999, "", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0, error) &&
              error == "not-found",
          "update/unknown_id", error);
    error.clear();
    // No wall clock here, so a daily rule cannot be built; the stored task must stay untouched.
    Check(!manager.UpdateReminder(id, "", ReminderType::kDaily, 0, 0, 9, 0, 0, 0, 0, error) &&
              error == "time-not-synced",
          "update/invalid_schedule_rejected", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].type == ReminderType::kRelative &&
              tasks[0].trigger_ms == tasks[0].created_ms + 90000,
          "update/rejected_update_keeps_task", tasks.empty() ? "missing" : TypeOf(tasks[0]));
    Finish("update", before);
}

}  // namespace
'''
DRIVER_SCENARIOS_F = r'''
namespace {

// 8. The list hides cancelled tasks, keeps id order and exposes the next trigger and the rule.
void RunList() {
    const int before = g_failures;
    std::printf("SCENARIO list\n");
    ResetState();
    ReminderManager manager;
    std::string error;

    const uint32_t a =
        manager.CreateReminder("任务一", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0, error);
    const uint32_t b =
        manager.CreateReminder("任务二", ReminderType::kRelative, 60000, 0, 0, 0, 0, 0, 0, error);
    const uint32_t c =
        manager.CreateReminder("任务三", ReminderType::kRelative, 120000, 0, 0, 0, 0, 0, 0, error);
    Check(a == 1 && b == 2 && c == 3, "list/created", "");
    Check(manager.CancelReminder(b, error), "list/cancel_second", error);

    std::vector<ReminderTask> tasks = manager.ListReminders();
    Check(tasks.size() == 2, "list/cancelled_entry_hidden", std::to_string(tasks.size()));
    Check(tasks.size() == 2 && tasks[0].id == a && tasks[1].id == c, "list/sorted_by_id", "");
    Check(tasks.size() == 2 && tasks[0].text == "任务一" &&
              tasks[0].type == ReminderType::kRelative &&
              tasks[0].state == ReminderState::kPending,
          "list/fields", tasks.empty() ? "missing"
                                       : tasks[0].text + "/" + TypeOf(tasks[0]) + "/" +
                                             StateOf(tasks[0]));
    Check(tasks.size() == 2 && tasks[0].trigger_ms > tasks[0].created_ms,
          "list/relative_has_deadline", "");
    Check(tasks.size() == 2 && tasks[0].trigger_unix == 0, "list/relative_has_no_unix_trigger", "");
    Check(tasks.size() == 2 && !tasks[0].recurring_active && !tasks[1].recurring_active,
          "list/one_shot_not_recurring", "");

    g_fake_unix = kJan1;
    const uint32_t daily =
        manager.CreateReminder("每天提醒", ReminderType::kDaily, 0, 0, 7, 30, 0, 0, 0, error);
    Check(daily == 4, "list/daily_created", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 3, "list/three_visible", std::to_string(tasks.size()));
    Check(tasks.size() == 3 && tasks[2].type == ReminderType::kDaily && tasks[2].recurring_active,
          "list/daily_recurring", tasks.size() < 3 ? "missing" : TypeOf(tasks[2]));
    Check(tasks.size() == 3 && tasks[2].trigger_unix == kJan1 + 7 * kHour + 30 * 60,
          "list/daily_next_trigger", tasks.size() < 3 ? "missing" : Num(tasks[2].trigger_unix));
    Check(tasks.size() == 3 && tasks[2].daily_hour == 7 && tasks[2].daily_minute == 30,
          "list/daily_rule_visible", "");
    Finish("list", before);
}

}  // namespace
'''
DRIVER_SCENARIOS_G = r'''
namespace {

// 9. A daily reminder fires once per day and is rescheduled for the next day (TZ=UTC).
void RunDailyRecurring() {
    const int before = g_failures;
    std::printf("SCENARIO daily_recurring\n");
    ResetState();
    g_fake_unix = kJan1;  // 2026-01-01T00:00:00Z
    ReminderManager manager;
    std::string error;

    const uint32_t id =
        manager.CreateReminder("每天早上九点", ReminderType::kDaily, 0, 0, 9, 0, 0, 0, 0, error);
    Check(id == 1, "daily/created", error);
    std::vector<ReminderTask> tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].trigger_unix == kJan1 + 9 * kHour, "daily/today_nine",
          tasks.empty() ? "missing" : Num(tasks[0].trigger_unix));
    Check(tasks.size() == 1 && tasks[0].daily_hour == 9 && tasks[0].daily_minute == 0,
          "daily/rule_stored", "");
    Check(tasks.size() == 1 && tasks[0].recurring_active, "daily/recurring_active", "");
    Check(manager.MarkDueTasks() == 0, "daily/not_due_before_nine", "");

    g_fake_unix = kJan1 + 9 * kHour;
    Check(manager.MarkDueTasks() == 1, "daily/due_at_nine", "");
    ReminderTask task;
    Check(manager.TakeNextDueTask(task) && task.id == id, "daily/taken", "");
    manager.MarkPlaying(id);
    manager.OnPlaybackFinished(id, PcmTtsPlayer::Error::kSuccess);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kAwaitingResponse,
          "daily/awaiting_response", tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(manager.ResolveAwaitingResponse(true, 0, error), "daily/explicit_confirmation", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kPending,
          "daily/back_to_pending", tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(tasks.size() == 1 && tasks[0].recurring_active, "daily/still_active", "");
    Check(tasks.size() == 1 && tasks[0].trigger_unix == kJan1 + 9 * kHour + kDay, "daily/next_day",
          tasks.empty() ? "missing" : Num(tasks[0].trigger_unix));
    Check(tasks.size() == 1 && tasks[0].last_fired_unix == kJan1 + 9 * kHour, "daily/last_fired",
          tasks.empty() ? "missing" : Num(tasks[0].last_fired_unix));
    Check(manager.MarkDueTasks() == 0, "daily/not_due_again_right_after", "");

    // Created at 23:59 for 00:05: the next slot is tomorrow 00:05, never a time in the past.
    g_fake_unix = kJan1 + kDay + 86340;  // 2026-01-02T23:59:00Z
    const uint32_t cross =
        manager.CreateReminder("跨日", ReminderType::kDaily, 0, 0, 0, 5, 0, 0, 0, error);
    Check(cross == 2, "daily/cross_day_created", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 2 && tasks[1].trigger_unix == kJan1 + 2 * kDay + 5 * 60,
          "daily/cross_day_next",
          tasks.size() < 2 ? "missing" : Num(tasks[1].trigger_unix));
    Finish("daily_recurring", before);
}

// 10. A weekly reminder follows its weekday rule and skips to the next week once it fired.
void RunWeeklyRecurring() {
    const int before = g_failures;
    std::printf("SCENARIO weekly_recurring\n");
    ResetState();
    g_fake_unix = kJan1;  // 2026-01-01T00:00:00Z, a Thursday
    ReminderManager manager;
    std::string error;

    // weekday 1 = Monday; the upcoming Monday is 4 days later.
    const uint32_t id =
        manager.CreateReminder("每周一", ReminderType::kWeekly, 0, 0, 9, 0, 1, 9, 0, error);
    Check(id == 1, "weekly/created", error);
    std::vector<ReminderTask> tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].trigger_unix == kJan1 + 4 * kDay + 9 * kHour,
          "weekly/next_monday", tasks.empty() ? "missing" : Num(tasks[0].trigger_unix));
    Check(tasks.size() == 1 && tasks[0].weekly_weekday == 1 && tasks[0].weekly_hour == 9 &&
              tasks[0].weekly_minute == 0,
          "weekly/rule_stored", "");
    Check(manager.MarkDueTasks() == 0, "weekly/not_due_before_monday", "");

    g_fake_unix = kJan1 + 4 * kDay + 9 * kHour;
    Check(manager.MarkDueTasks() == 1, "weekly/due_at_monday_nine", "");
    ReminderTask task;
    Check(manager.TakeNextDueTask(task) && task.id == id, "weekly/taken", "");
    manager.MarkPlaying(id);
    manager.OnPlaybackFinished(id, PcmTtsPlayer::Error::kSuccess);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kAwaitingResponse,
          "weekly/awaiting_response", tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(manager.ResolveAwaitingResponse(true, 0, error), "weekly/explicit_confirmation", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 1 && tasks[0].state == ReminderState::kPending && tasks[0].recurring_active,
          "weekly/rescheduled", tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(tasks.size() == 1 && tasks[0].trigger_unix == kJan1 + 11 * kDay + 9 * kHour,
          "weekly/next_week", tasks.empty() ? "missing" : Num(tasks[0].trigger_unix));

    // Same weekday but an earlier time than now: the following week is used.
    g_fake_unix = kJan1 + 3 * kDay + 10 * kHour;  // Sunday 2026-01-04T10:00:00Z
    const uint32_t sunday =
        manager.CreateReminder("每周日", ReminderType::kWeekly, 0, 0, 8, 0, 0, 8, 0, error);
    Check(sunday == 2, "weekly/same_day_created", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 2 && tasks[1].trigger_unix == kJan1 + 10 * kDay + 8 * kHour,
          "weekly/same_day_passed_uses_next_week",
          tasks.size() < 2 ? "missing" : Num(tasks[1].trigger_unix));

    // The same weekday later today still fires today.
    g_fake_unix = kJan1;  // Thursday 00:00
    const uint32_t thursday =
        manager.CreateReminder("每周四", ReminderType::kWeekly, 0, 0, 9, 0, 4, 9, 0, error);
    Check(thursday == 3, "weekly/today_created", error);
    tasks = manager.ListReminders();
    Check(tasks.size() == 3 && tasks[2].trigger_unix == kJan1 + 9 * kHour,
          "weekly/later_today_used", tasks.size() < 3 ? "missing" : Num(tasks[2].trigger_unix));
    Finish("weekly_recurring", before);
}

}  // namespace
'''
DRIVER_SCENARIOS_H = r'''
namespace {

constexpr const char* kNvsKey = "reminder_tasks/tasks_v1";

// 11. Tasks survive a restart: ids stay monotonic, a playing task is retried, missed occurrences are
//     not replayed in a burst, and a corrupted store is discarded instead of crashing.
void RunPersistAndRestore() {
    const int before = g_failures;
    std::printf("SCENARIO persist_and_restore\n");
    ResetState();
    g_fake_unix = kJan1;
    g_fake_time_us = kHour * 1000ULL * 1000;  // device has been up for an hour
    std::string error;
    {
        ReminderManager manager;
        const uint32_t relative = manager.CreateReminder("存活的相对任务", ReminderType::kRelative,
                                                         60000, 0, 0, 0, 0, 0, 0, error);
        Check(relative == 1, "persist/relative_created", error);
        const uint32_t daily = manager.CreateReminder("存活的每日任务", ReminderType::kDaily, 0, 0, 9,
                                                      0, 0, 0, 0, error);
        Check(daily == 2, "persist/daily_created", error);
    }
    Check(!Settings::store[kNvsKey].empty(), "persist/written_to_nvs", "");

    // Reboot: the monotonic clock restarted while the wall clock kept its value.
    g_fake_time_us = 500ULL * 1000;
    ReminderManager restored;
    restored.Init();
    std::vector<ReminderTask> tasks = restored.ListReminders();
    Check(tasks.size() == 2, "persist/restored_size", std::to_string(tasks.size()));
    Check(tasks.size() == 2 && tasks[0].id == 1 && tasks[0].text == "存活的相对任务" &&
              tasks[0].type == ReminderType::kRelative,
          "persist/relative_fields", tasks.empty() ? "missing" : TypeOf(tasks[0]));
    Check(tasks.size() == 2 && tasks[0].state == ReminderState::kPending, "persist/relative_pending",
          tasks.empty() ? "missing" : StateOf(tasks[0]));
    Check(tasks.size() == 2 && tasks[0].trigger_ms == 60500,
          "persist/relative_rearmed_after_boot",
          tasks.empty() ? "missing" : Num(static_cast<int64_t>(tasks[0].trigger_ms)));
    Check(tasks.size() == 2 && tasks[0].created_ms == 500, "persist/relative_created_ms_rebased",
          tasks.empty() ? "missing" : Num(static_cast<int64_t>(tasks[0].created_ms)));
    Check(tasks.size() == 2 && tasks[1].id == 2 && tasks[1].type == ReminderType::kDaily &&
              tasks[1].recurring_active,
          "persist/daily_fields", tasks.size() < 2 ? "missing" : TypeOf(tasks[1]));
    Check(tasks.size() == 2 && tasks[1].trigger_unix == kJan1 + 9 * kHour,
          "persist/daily_trigger_unix",
          tasks.size() < 2 ? "missing" : Num(tasks[1].trigger_unix));

    g_fake_time_us = 60500ULL * 1000;  // exactly the original 60 s delay after this boot
    Check(restored.MarkDueTasks() == 1, "persist/relative_fires_after_reboot", "");
    const uint32_t next =
        restored.CreateReminder("新任务", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0, error);
    Check(next == 3, "persist/ids_continue_after_restore", std::to_string(next));

    auto init_from = [](const std::string& raw) {
        Settings::store[kNvsKey] = raw;
        auto manager = std::make_unique<ReminderManager>();
        manager->Init();
        return manager;
    };
    auto absolute_json = [](int64_t trigger_unix, const char* state) {
        return std::string("{\"version\":1,\"tasks\":[{\"id\":1,\"text\":\"绝对任务\",\"type\":"
                           "\"absolute\",\"state\":\"") +
               state + "\",\"trigger_unix\":" + std::to_string(trigger_unix) + "}]}";
    };
    auto daily_json = [](int64_t trigger_unix, int hour, const char* state) {
        return std::string("{\"version\":1,\"tasks\":[{\"id\":1,\"text\":\"每日任务\",\"type\":"
                           "\"daily\",\"state\":\"") +
               state + "\",\"trigger_unix\":" + std::to_string(trigger_unix) +
               ",\"daily_hour\":" + std::to_string(hour) + "}]}";
    };
    ReminderTask task;
    g_fake_time_us = 5000ULL * 1000;
    g_fake_unix = kJan1;

    {
        auto manager = init_from(
            R"({"version":1,"tasks":[{"id":1,"text":"已完成","type":"relative","state":"completed","trigger_ms":1000}]})");
        tasks = manager->ListReminders();
        Check(tasks.size() == 1 && tasks[0].state == ReminderState::kCompleted,
              "restart/completed_not_replayed", tasks.empty() ? "missing" : StateOf(tasks[0]));
        Check(!manager->TakeNextDueTask(task), "restart/completed_not_due", "");
        Check(manager->CreateReminder("继续编号", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0,
                                      error) == 2,
              "restart/next_id_after_restore", error);
    }
    {
        auto manager = init_from(
            R"({"version":1,"tasks":[{"id":4,"text":"播放中被重启","type":"relative","state":"playing","trigger_ms":1000}]})");
        tasks = manager->ListReminders();
        Check(tasks.size() == 1 && tasks[0].state == ReminderState::kDue,
              "restart/playing_restored_as_due", tasks.empty() ? "missing" : StateOf(tasks[0]));
        Check(manager->TakeNextDueTask(task) && task.id == 4, "restart/playing_task_retried",
              std::to_string(task.id));
    }
    {
        auto manager = init_from(
            R"({"version":1,"tasks":[{"id":5,"text":"等待回应","type":"relative","state":"awaiting_response","trigger_ms":1000}]})");
        tasks = manager->ListReminders();
        Check(tasks.size() == 1 && tasks[0].state == ReminderState::kDue,
              "restart/awaiting_response_replayed", tasks.empty() ? "missing" : StateOf(tasks[0]));
        Check(manager->TakeNextDueTask(task) && task.id == 5,
              "restart/awaiting_response_can_be_replayed", std::to_string(task.id));
    }
    {
        auto manager = init_from(absolute_json(kJan1 - 60, "pending"));
        tasks = manager->ListReminders();
        Check(tasks.size() == 1 && tasks[0].state == ReminderState::kDue,
              "restart/recently_expired_absolute_is_due",
              tasks.empty() ? "missing" : StateOf(tasks[0]));
    }
    {
        auto manager = init_from(absolute_json(kJan1 - 600, "pending"));
        tasks = manager->ListReminders();
        Check(tasks.size() == 1 && tasks[0].state == ReminderState::kCompleted,
              "restart/long_expired_absolute_completed",
              tasks.empty() ? "missing" : StateOf(tasks[0]));
    }
    {
        // 09:00 was missed while the device was off, so the next run is tomorrow's 09:00.
        g_fake_unix = kJan1 + 10 * kHour;
        auto manager = init_from(daily_json(kJan1 + 9 * kHour, 9, "pending"));
        tasks = manager->ListReminders();
        Check(tasks.size() == 1 && tasks[0].state == ReminderState::kPending &&
                  tasks[0].recurring_active,
              "restart/missed_daily_rescheduled", tasks.empty() ? "missing" : StateOf(tasks[0]));
        Check(tasks.size() == 1 && tasks[0].trigger_unix == kJan1 + kDay + 9 * kHour,
              "restart/missed_daily_next_run",
              tasks.empty() ? "missing" : Num(tasks[0].trigger_unix));

        // Only two minutes late is inside the grace window, so it is played instead of skipped.
        g_fake_unix = kJan1 + 9 * kHour + 120;
        auto late = init_from(daily_json(kJan1 + 9 * kHour, 9, "pending"));
        tasks = late->ListReminders();
        Check(tasks.size() == 1 && tasks[0].state == ReminderState::kDue,
              "restart/slightly_late_daily_is_due", tasks.empty() ? "missing" : StateOf(tasks[0]));
        g_fake_unix = kJan1;
    }
    {
        auto manager = init_from("{not json");
        Check(manager->ListReminders().empty(), "restart/corrupt_json_discarded", "");
    }
    {
        auto manager = init_from(R"({"version":2,"tasks":[]})");
        Check(manager->ListReminders().empty(), "restart/unsupported_version_discarded", "");
    }
    {
        auto manager = init_from(
            R"({"version":1,"tasks":[{"id":1,"text":"坏了","type":"bogus","state":"pending"}]})");
        Check(manager->ListReminders().empty(), "restart/invalid_entry_discarded", "");
    }
    {
        auto manager = init_from("[]");
        Check(manager->ListReminders().empty(), "restart/non_object_discarded", "");
        Check(manager->CreateReminder("重启后新建", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0,
                                      error) == 1,
              "restart/create_after_bad_restore", error);
    }
    Finish("persist_and_restore", before);
}

}  // namespace
'''
DRIVER_SCENARIOS_I = r'''
namespace {

// 12. Input validation and the 16 task limit, including slot reuse after a task finished.
void RunInvalidInput() {
    const int before = g_failures;
    std::printf("SCENARIO invalid_input\n");
    ResetState();
    g_fake_unix = kJan1;
    ReminderManager manager;
    std::string error;

    Check(manager.CreateReminder("", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0, error) == 0 &&
              error == "empty-text",
          "invalid/empty_text", error);
    error.clear();
    Check(manager.CreateReminder("   ", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0, error) == 0 &&
              error == "empty-text",
          "invalid/blank_text", error);
    error.clear();
    Check(manager.CreateReminder(std::string(121, 'a'), ReminderType::kRelative, 1000, 0, 0, 0, 0, 0,
                                 0, error) == 0 &&
              error == "text-too-long",
          "invalid/text_too_long", error);
    error.clear();
    Check(manager.CreateReminder(std::string(120, 'a'), ReminderType::kRelative, 1000, 0, 0, 0, 0, 0,
                                 0, error) == 1,
          "invalid/text_at_limit_accepted", error);
    error.clear();
    Check(manager.CreateReminder("零延迟", ReminderType::kRelative, 0, 0, 0, 0, 0, 0, 0, error) == 0 &&
              error == "invalid-time",
          "invalid/zero_delay", error);
    error.clear();
    Check(manager.CreateReminder("小时越界", ReminderType::kDaily, 0, 0, 24, 0, 0, 0, 0, error) == 0 &&
              error == "invalid-time",
          "invalid/hour_out_of_range", error);
    error.clear();
    Check(manager.CreateReminder("分钟越界", ReminderType::kDaily, 0, 0, 9, 60, 0, 0, 0, error) == 0 &&
              error == "invalid-time",
          "invalid/minute_out_of_range", error);
    error.clear();
    Check(manager.CreateReminder("星期越界", ReminderType::kWeekly, 0, 0, 9, 0, 7, 9, 0, error) == 0 &&
              error == "invalid-time",
          "invalid/weekday_out_of_range", error);
    error.clear();
    Check(manager.CreateReminder("绝对时间缺失", ReminderType::kAbsolute, 0, 0, 0, 0, 0, 0, 0,
                                 error) == 0 &&
              error == "invalid-time",
          "invalid/absolute_without_trigger", error);
    Check(manager.ListReminders().size() == 1, "invalid/only_valid_task_stored",
          std::to_string(manager.ListReminders().size()));

    // Full table: an awaiting-response task still occupies a slot; explicit completion frees it.
    g_fake_time_us = 0;
    ReminderManager full;
    int created = 0;
    for (int i = 0; i < 16; ++i) {
        error.clear();
        if (full.CreateReminder("任务" + std::to_string(i), ReminderType::kRelative, 1000ULL * (i + 1),
                                0, 0, 0, 0, 0, 0, error) > 0) {
            ++created;
        }
    }
    Check(created == 16, "invalid/sixteen_tasks_allowed", std::to_string(created));
    error.clear();
    Check(full.CreateReminder("超出上限", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0, error) == 0 &&
              error == "task-limit-reached",
          "invalid/seventeenth_rejected", error);
    Check(full.ListReminders().size() == 16, "invalid/list_capped",
          std::to_string(full.ListReminders().size()));

    g_fake_time_us = 16000ULL * 1000;
    Check(full.MarkDueTasks() == 16, "invalid/all_sixteen_due", "");
    full.MarkPlaying(1);
    full.OnPlaybackFinished(1, PcmTtsPlayer::Error::kSuccess);
    Check(full.ListReminders()[0].state == ReminderState::kAwaitingResponse,
          "invalid/awaiting_still_occupies_slot", "");
    Check(full.ResolveAwaitingResponse(true, 0, error), "invalid/explicit_completion_frees_slot", error);
    error.clear();
    const uint32_t reused =
        full.CreateReminder("复用槽位", ReminderType::kRelative, 1000, 0, 0, 0, 0, 0, 0, error);
    Check(reused == 17, "invalid/slot_reused_with_new_id", std::to_string(reused));
    Check(full.ListReminders().size() == 16, "invalid/pruned_back_to_limit",
          std::to_string(full.ListReminders().size()));
    Finish("invalid_input", before);
}

}  // namespace

int main(int argc, char** argv) {
    // The daily/weekly rules go through localtime_r()/mktime(), so pin the zone to make the
    // expected timestamps reproducible.
#if defined(_WIN32)
    _putenv_s("TZ", "UTC0");
    _tzset();
#else
    setenv("TZ", "UTC", 1);
    tzset();
#endif
    if (argc < 2) {
        std::printf("usage: %s <scenario>\n", argv[0]);
        return 2;
    }
    const std::string scenario = argv[1];
    if (scenario == "relative_and_due") {
        RunRelativeAndDue();
    } else if (scenario == "absolute_and_time_sync") {
        RunAbsoluteAndTimeSync();
    } else if (scenario == "multi_task_and_ordering") {
        RunMultiTaskAndOrdering();
    } else if (scenario == "playing_lifecycle") {
        RunPlayingLifecycle();
    } else if (scenario == "response_lifecycle") {
        RunResponseLifecycle();
    } else if (scenario == "playback_failed_retry") {
        RunPlaybackFailedRetry();
    } else if (scenario == "cancel") {
        RunCancel();
    } else if (scenario == "update") {
        RunUpdate();
    } else if (scenario == "list") {
        RunList();
    } else if (scenario == "daily_recurring") {
        RunDailyRecurring();
    } else if (scenario == "weekly_recurring") {
        RunWeeklyRecurring();
    } else if (scenario == "persist_and_restore") {
        RunPersistAndRestore();
    } else if (scenario == "invalid_input") {
        RunInvalidInput();
    } else {
        std::printf("unknown scenario: %s\n", scenario.c_str());
        return 2;
    }
    return g_failures == 0 ? 0 : 1;
}
'''

DRIVER = (
    DRIVER_HEAD
    + DRIVER_SCENARIOS_A
    + DRIVER_SCENARIOS_B
    + DRIVER_SCENARIOS_C
    + DRIVER_SCENARIOS_D
    + DRIVER_SCENARIOS_E
    + DRIVER_SCENARIOS_F
    + DRIVER_SCENARIOS_G
    + DRIVER_SCENARIOS_H
    + DRIVER_SCENARIOS_I
)


def SplitCompiler(value):
    """Splits a CXX value into argv, keeping Windows backslash paths intact."""
    return shlex.split(value, posix=os.name != "nt")


def WriteStubs(build):
    """Materialises the platform stubs both host builds compile against into `build`."""
    for name, content in STUBS.items():
        path = build / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


class ReminderManagerTest(unittest.TestCase):
    """Drives reminder_manager.cc with a fake monotonic clock, a fake wall clock and fake NVS."""

    @classmethod
    def setUpClass(cls):
        compiler = SplitCompiler(os.environ.get("CXX", "c++"))
        if not compiler or shutil.which(compiler[0]) is None:
            raise unittest.SkipTest(
                f"host C++ compiler not found: {(compiler or ['c++'])[0]} "
                "(set CXX=/path/to/g++ to run these tests)"
            )
        cjson_source = CJSON_DIR / "cJSON.c"
        if not cjson_source.is_file():
            raise unittest.SkipTest(f"vendored cJSON sources not found: {cjson_source}")

        cls._tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tempdir.cleanup)
        build = Path(cls._tempdir.name)
        cls.compiler = compiler
        cls.build_dir = build

        WriteStubs(build)

        driver = build / "reminder_manager_driver.cc"
        driver.write_text(textwrap.dedent(DRIVER).lstrip("\n"), encoding="utf-8")

        # reminder_manager.cc is compiled verbatim; pcm_tts_player.h is only needed for its Error
        # enum, and its own audio_service.h resolves to the stub in the build directory.
        sources = [str(ROOT / "main" / "reminder" / "reminder_manager.cc"), str(cjson_source)]
        cls.exe_path = build / ("reminder_manager" + (".exe" if os.name == "nt" else ""))
        command = compiler + [
            "-std=gnu++23",
            "-O1",
            "-g",
            "-pthread",
            f"-I{build}",
            f"-I{ROOT / 'main'}",
            f"-I{ROOT / 'main' / 'reminder'}",
            f"-I{CJSON_DIR}",
            str(driver),
        ] + sources + [
            "-o",
            str(cls.exe_path),
        ]
        compiled = subprocess.run(command, capture_output=True, text=True)
        if compiled.returncode != 0:
            raise AssertionError(
                "failed to build the reminder manager host test:\n" + compiled.stderr
            )

    def _run(self, scenario):
        completed = subprocess.run(
            [str(self.exe_path), scenario],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "TZ": "UTC"},
        )
        output = completed.stdout + completed.stderr
        if os.environ.get("REMINDER_MANAGER_VERBOSE"):
            print(output)
        self.assertEqual(0, completed.returncode, msg=output)
        self.assertIn(f"RESULT {scenario} PASS", output, msg=output)
        self.assertNotIn("CHECK FAIL", output, msg=output)
        return output

    def test_relative_reminder_turns_due_at_its_deadline(self):
        self._run("relative_and_due")

    def test_absolute_and_recurring_tasks_need_a_synced_clock(self):
        self._run("absolute_and_time_sync")

    def test_due_tasks_are_handed_out_lowest_id_first(self):
        self._run("multi_task_and_ordering")

    def test_playing_task_enters_response_wait_until_explicit_confirmation(self):
        self._run("playing_lifecycle")

    def test_response_completion_snooze_and_timeout_retry(self):
        self._run("response_lifecycle")

    def test_failed_playback_keeps_the_task_retryable(self):
        self._run("playback_failed_retry")

    def test_cancel_removes_the_task_from_the_queue(self):
        self._run("cancel")

    def test_update_reschedules_a_task_and_keeps_it_listed(self):
        self._run("update")

    def test_list_hides_cancelled_tasks(self):
        self._run("list")

    def test_daily_reminder_fires_once_per_day(self):
        self._run("daily_recurring")

    def test_weekly_reminder_follows_the_weekday_rule(self):
        self._run("weekly_recurring")

    def test_tasks_survive_a_restart(self):
        self._run("persist_and_restore")

    def test_invalid_input_and_the_task_limit_are_rejected(self):
        self._run("invalid_input")


class ReminderToolsCompileTest(unittest.TestCase):
    """Compiles reminder_tools.cc verbatim against the real mcp_server.h.

    The MCP server itself cannot be linked on the host (it pulls in Application, Board and LVGL),
    so this is a compile check: it catches schema/Property/AddTool misuse and the ReminderManager
    call signatures, but exercises no tool call at runtime. Nothing is executed, so any C++ compiler
    will do -- a cross toolchain such as xtensa-esp32s3-elf-g++ is enough when the host has none.
    """

    @classmethod
    def setUpClass(cls):
        candidates = [os.environ["CXX"]] if os.environ.get("CXX") else []
        candidates += ["c++", "g++", "clang++", "xtensa-esp32s3-elf-g++"]
        compiler = None
        for candidate in candidates:
            argv = SplitCompiler(candidate)
            if argv and shutil.which(argv[0]) is not None:
                compiler = argv
                break
        if compiler is None:
            raise unittest.SkipTest(
                "no C++ compiler found (set CXX=/path/to/g++ to run this test)"
            )
        if not (CJSON_DIR / "cJSON.h").is_file():
            raise unittest.SkipTest(f"vendored cJSON sources not found: {CJSON_DIR}")

        cls._tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tempdir.cleanup)
        cls.build_dir = Path(cls._tempdir.name)
        cls.compiler = compiler
        WriteStubs(cls.build_dir)

    def test_tools_compile_against_the_real_mcp_schema_api(self):
        object_path = self.build_dir / "reminder_tools.o"
        command = self.compiler + [
            "-std=gnu++23",
            "-c",
            f"-I{self.build_dir}",
            f"-I{ROOT / 'main'}",
            f"-I{ROOT / 'main' / 'reminder'}",
            f"-I{CJSON_DIR}",
            str(ROOT / "main" / "reminder" / "reminder_tools.cc"),
            "-o",
            str(object_path),
        ]
        compiled = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, compiled.returncode,
                         msg="reminder_tools.cc does not compile:\n" + compiled.stderr)
        self.assertTrue(object_path.is_file())


if __name__ == "__main__":
    unittest.main()
