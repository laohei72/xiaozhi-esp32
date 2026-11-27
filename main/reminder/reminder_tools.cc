#include "reminder_tools.h"

#include <esp_log.h>
#include <esp_timer.h>

#include <cJSON.h>

#include <cstdint>
#include <ctime>
#include <limits>
#include <string>

#include "cjson_utils.h"
#include "mcp_server.h"
#include "reminder_manager.h"

#define TAG "Reminder"

namespace {

// Same limit as ReminderManager: the text is sent to the TTS service unchanged, so 120 bytes is
// about 40 Chinese characters or 120 Latin ones.
constexpr int kMaxTextBytes = 120;
// ReminderManager refuses to trust the wall clock before 2025-01-01 UTC, because the device time is
// received over the network and reads 1970 until then.
constexpr int64_t kMinTrustedUnix = 1735689600;
// Room for "YYYY-MM-DD HH:MM:SS".
constexpr size_t kLocalTimeBufferSize = 24;

uint64_t NowMonotonicMs() { return static_cast<uint64_t>(esp_timer_get_time()) / 1000; }

// The wall clock, or 0 when the device has not received the network time yet.
int64_t TrustedNowUnix() {
    const std::time_t now = std::time(nullptr);
    const int64_t now_unix = static_cast<int64_t>(now);
    return now_unix > kMinTrustedUnix ? now_unix : 0;
}

// Always the device's local time: Application applies the TZ of the "reminder" NVS namespace at
// boot, so the daily/weekly schedules and this string agree.
bool FormatLocalTime(int64_t unix_seconds, char* buffer, size_t size) {
    const std::time_t seconds = static_cast<std::time_t>(unix_seconds);
    std::tm local{};
    if (localtime_r(&seconds, &local) == nullptr) {
        return false;
    }
    return std::strftime(buffer, size, "%Y-%m-%d %H:%M:%S", &local) > 0;
}

// ReminderManager reports failures as short codes; these are the sentences the model gets to read.
std::string ErrorText(const std::string& code) {
    if (code == "empty-text") {
        return "the reminder text is empty";
    }
    if (code == "text-too-long") {
        return "the reminder text is longer than 120 bytes";
    }
    if (code == "task-limit-reached") {
        return "the device already holds 16 active reminders, cancel one first";
    }
    if (code == "time-not-synced") {
        return "the device clock is not synchronized yet, so only relative reminders work";
    }
    if (code == "invalid-time") {
        return "the requested time is not valid";
    }
    if (code == "not-found") {
        return "no reminder with that id";
    }
    return code.empty() ? std::string("the reminder could not be saved") : code;
}

bool ParseType(const std::string& name, ReminderType& type) {
    if (name == "relative") {
        type = ReminderType::kRelative;
        return true;
    }
    if (name == "absolute") {
        type = ReminderType::kAbsolute;
        return true;
    }
    if (name == "daily") {
        type = ReminderType::kDaily;
        return true;
    }
    if (name == "weekly") {
        type = ReminderType::kWeekly;
        return true;
    }
    return false;
}

// Tool arguments mapped to the slots ReminderManager::CreateReminder()/UpdateReminder() expect.
struct Schedule {
    ReminderType type = ReminderType::kRelative;
    uint64_t delay_ms = 0;
    int64_t trigger_unix = 0;
    int daily_hour = 0;
    int daily_minute = 0;
    int weekly_weekday = 0;
    int weekly_hour = 0;
    int weekly_minute = 0;
};

// Returns an empty string on success, otherwise the message to answer the model with. The schema
// stays flat on purpose: hour/minute mean the daily rule or the weekly rule depending on |type|, so
// the model never has to nest objects.
std::string BuildScheduleFromProperties(const PropertyList& properties, Schedule& schedule) {
    if (!ParseType(properties["type"].value<std::string>(), schedule.type)) {
        return "type must be one of: relative, absolute, daily, weekly";
    }
    const int delay_seconds = properties["delay_seconds"].value<int>();
    const int trigger_unix = properties["trigger_unix"].value<int>();
    const int hour = properties["hour"].value<int>();
    const int minute = properties["minute"].value<int>();
    const int weekday = properties["weekday"].value<int>();

    switch (schedule.type) {
        case ReminderType::kRelative:
            if (delay_seconds <= 0) {
                return "delay_seconds must be greater than 0 for a relative reminder";
            }
            schedule.delay_ms = static_cast<uint64_t>(delay_seconds) * 1000;
            break;
        case ReminderType::kAbsolute:
            if (trigger_unix <= 0) {
                return "trigger_unix must be a Unix timestamp in seconds for an absolute reminder";
            }
            schedule.trigger_unix = trigger_unix;
            break;
        case ReminderType::kDaily:
            schedule.daily_hour = hour;
            schedule.daily_minute = minute;
            break;
        case ReminderType::kWeekly:
            schedule.weekly_weekday = weekday;
            schedule.weekly_hour = hour;
            schedule.weekly_minute = minute;
            break;
    }
    return "";
}

// One task as the model sees it. |now_unix| is 0 when the clock is not synchronized; |now_ms| is
// the monotonic clock relative reminders are measured against.
cJSON* TaskToJson(const ReminderTask& task, int64_t now_unix, uint64_t now_ms) {
    cJSON* item = cJSON_CreateObject();
    if (item == nullptr) {
        return nullptr;
    }
    cJSON_AddNumberToObject(item, "id", task.id);
    cJSON_AddStringToObject(item, "text", task.text.c_str());
    cJSON_AddStringToObject(item, "type", ReminderManager::TypeName(task.type));
    cJSON_AddStringToObject(item, "state", ReminderManager::StateName(task.state));

    int64_t next_unix = 0;
    if (task.type == ReminderType::kRelative) {
        // trigger_ms counts from boot, so only the remaining delay is meaningful here.
        const uint64_t remaining_ms = task.trigger_ms > now_ms ? task.trigger_ms - now_ms : 0;
        cJSON_AddNumberToObject(item, "remaining_seconds",
                                static_cast<double>((remaining_ms + 999) / 1000));
        if (now_unix > 0) {
            next_unix = now_unix + static_cast<int64_t>(remaining_ms / 1000);
        }
    } else {
        next_unix = task.trigger_unix;
    }
    if (next_unix > 0) {
        cJSON_AddNumberToObject(item, "next_trigger_unix", static_cast<double>(next_unix));
        char buffer[kLocalTimeBufferSize];
        if (FormatLocalTime(next_unix, buffer, sizeof(buffer))) {
            cJSON_AddStringToObject(item, "next_trigger_local", buffer);
        }
    }

    switch (task.type) {
        case ReminderType::kDaily:
            cJSON_AddNumberToObject(item, "hour", task.daily_hour);
            cJSON_AddNumberToObject(item, "minute", task.daily_minute);
            break;
        case ReminderType::kWeekly:
            cJSON_AddNumberToObject(item, "weekday", task.weekly_weekday);
            cJSON_AddNumberToObject(item, "hour", task.weekly_hour);
            cJSON_AddNumberToObject(item, "minute", task.weekly_minute);
            break;
        case ReminderType::kRelative:
        case ReminderType::kAbsolute:
            break;
    }
    return item;
}

// The stored task after a create/update, so the model can confirm what was actually scheduled: the
// manager recomputes the next daily/weekly occurrence itself.
ToolResult DescribeTask(ReminderManager& manager, uint32_t id) {
    const int64_t now_unix = TrustedNowUnix();
    const uint64_t now_ms = NowMonotonicMs();
    for (const auto& task : manager.ListReminders()) {
        if (task.id != id) {
            continue;
        }
        CJsonUniquePtr json(TaskToJson(task, now_unix, now_ms));
        if (json == nullptr) {
            return std::unexpected("Failed to describe the reminder");
        }
        return json.release();
    }
    return std::unexpected("no reminder with id " + std::to_string(id));
}

// Flat schema, see BuildScheduleFromProperties(): every schedule field has a default, so the model
// only sends the ones its chosen type needs. String defaults are built from std::string on purpose:
// a bare literal would be stored in the bool alternative of Property's variant.
// |text_required| puts `text` on the schema's required list (create) instead of giving it a default
// (update, where omitting it keeps the current text).
PropertyList CreateProperties(bool text_required = true) {
    Property text = text_required ? Property("text", kPropertyTypeString)
                                  : Property("text", kPropertyTypeString, std::string());
    text.SetMaxLength(kMaxTextBytes);
    const int int_max = std::numeric_limits<int>::max();
    return PropertyList({
        text,
        Property("type", kPropertyTypeString, std::string("relative")),
        Property("delay_seconds", kPropertyTypeInteger, 0, 0, 30 * 24 * 60 * 60),
        Property("trigger_unix", kPropertyTypeInteger, 0, 0, int_max),
        Property("hour", kPropertyTypeInteger, 0, 0, 23),
        Property("minute", kPropertyTypeInteger, 0, 0, 59),
        Property("weekday", kPropertyTypeInteger, 0, 0, 6),
    });
}

// Update needs the id on top of the create fields; |text| stays optional so it can be left out to
// keep the current text.
PropertyList UpdateProperties() {
    PropertyList properties = CreateProperties(false);
    properties.AddProperty(
        Property("id", kPropertyTypeInteger, 1, std::numeric_limits<int>::max()));
    return properties;
}

}  // namespace

void RegisterReminderTools(ReminderManager& manager) {
    auto& mcp_server = McpServer::GetInstance();

    mcp_server.AddTool(
        "self.reminders.create",
        "Create a time reminder. The device speaks `text` out loud by itself when the reminder is "
        "due, so write it as one natural spoken sentence (no emoji, at most 120 bytes, about 40 "
        "Chinese characters).\n"
        "Choose the schedule with `type`:\n"
        "- relative: `delay_seconds` from now. The most reliable kind, and the right choice for "
        "every request phrased as a duration (\"in 30 minutes\", \"in 2 hours\").\n"
        "- absolute: `trigger_unix`, a Unix timestamp in seconds, for one specific moment.\n"
        "- daily: `hour` and `minute`, every day at that time.\n"
        "- weekly: `weekday` (0 = Sunday, 6 = Saturday), `hour` and `minute`, every week.\n"
        "Daily, weekly and absolute times are the device's local time. Call `self.reminders.list` "
        "first when the request needs the current time, and check the existing reminders so the "
        "same task is not created twice.",
        CreateProperties(),
        [&manager](const PropertyList& properties) -> ToolResult {
            Schedule schedule;
            std::string error = BuildScheduleFromProperties(properties, schedule);
            if (!error.empty()) {
                return std::unexpected(error);
            }
            const std::string text = properties["text"].value<std::string>();
            const uint32_t id =
                manager.CreateReminder(text, schedule.type, schedule.delay_ms, schedule.trigger_unix,
                                       schedule.daily_hour, schedule.daily_minute,
                                       schedule.weekly_weekday, schedule.weekly_hour,
                                       schedule.weekly_minute, error);
            if (id == 0) {
                return std::unexpected(ErrorText(error));
            }
            return DescribeTask(manager, id);
        });

    mcp_server.AddTool(
        "self.reminders.list",
        "List the reminders stored on the device and report the device clock (`now_unix`, "
        "`now_local`). `remaining_seconds` counts down a relative reminder and "
        "`next_trigger_local` shows when its next run is due in the device's local time. Use it "
        "before creating an absolute/daily/weekly reminder, and before cancelling or changing one. "
        "After the device speaks a reminder, check for a task in `awaiting_response` state before "
        "interpreting the user's next utterance, even when it is short (such as `好的` or `知道了`); it may be a confirmation, a snooze "
        "request, or a question about the reminder.",
        PropertyList(), [&manager](const PropertyList&) -> ToolResult {
            const auto tasks = manager.ListReminders();
            CJsonUniquePtr root(cJSON_CreateObject());
            CJsonUniquePtr array(cJSON_CreateArray());
            if (root == nullptr || array == nullptr) {
                return std::unexpected("Failed to build the reminder list");
            }
            const int64_t now_unix = TrustedNowUnix();
            cJSON_AddBoolToObject(root.get(), "clock_synced", now_unix > 0);
            if (now_unix > 0) {
                cJSON_AddNumberToObject(root.get(), "now_unix", static_cast<double>(now_unix));
                char buffer[kLocalTimeBufferSize];
                if (FormatLocalTime(now_unix, buffer, sizeof(buffer))) {
                    cJSON_AddStringToObject(root.get(), "now_local", buffer);
                }
            }
            cJSON_AddNumberToObject(root.get(), "count", static_cast<double>(tasks.size()));
            const uint64_t now_ms = NowMonotonicMs();
            for (const auto& task : tasks) {
                cJSON* item = TaskToJson(task, now_unix, now_ms);
                if (item == nullptr) {
                    return std::unexpected("Failed to build the reminder list");
                }
                if (!cJSON_AddItemToArray(array.get(), item)) {
                    cJSON_Delete(item);
                    return std::unexpected("Failed to build the reminder list");
                }
            }
            if (!cJSON_AddItemToObject(root.get(), "tasks", array.get())) {
                return std::unexpected("Failed to build the reminder list");
            }
            array.release();
            return root.release();
        });

    mcp_server.AddTool(
        "self.reminders.respond",
        "Resolve one specific reminder after first calling `self.reminders.list` and reading its "
        "id and state. Pass that exact `id`. Use action `complete` ONLY when the user clearly "
        "confirms that reminder has been handled (for example, 'done', 'I did it', or Chinese "
        "'已经处理了'); '好的' or '知道了' alone are NOT confirmation. Use action `snooze` when "
        "the user explicitly says they will handle it later (for example, '我等会再喝', "
        "'稍后看', '半小时后再提醒我'); provide `delay_seconds` in seconds. If no delay is "
        "specified, use 300 seconds. This tool can resolve a task in `awaiting_response` or "
        "`paused` state. If the user asks a question, discusses the reminder, or says unrelated "
        "things without asking to postpone it, DO NOT call this tool; continue the conversation "
        "and leave the reminder unresolved. Never infer completion or snooze from arbitrary speech.",
        PropertyList({
            Property("id", kPropertyTypeInteger, 1, 65535),
            Property("action", kPropertyTypeString),
            Property("delay_seconds", kPropertyTypeInteger, 1, 7 * 24 * 60 * 60),
        }),
        [&manager](const PropertyList& properties) -> ToolResult {
            const uint32_t id = static_cast<uint32_t>(properties["id"].value<int>());
            const std::string action = properties["action"].value<std::string>();
            if (action != "complete" && action != "snooze") {
                return std::unexpected("action must be complete or snooze");
            }
            uint64_t delay_ms = 0;
            if (action == "snooze") {
                const int delay_seconds = properties["delay_seconds"].value<int>();
                if (delay_seconds < 1 || delay_seconds > 7 * 24 * 60 * 60) {
                    return std::unexpected("snooze requires delay_seconds from 1 to 604800");
                }
                delay_ms = static_cast<uint64_t>(delay_seconds) * 1000;
            }
            std::string error;
            if (!manager.ResolveReminderResponse(id, action == "complete", delay_ms, error)) {
                return std::unexpected(error);
            }
            return true;
        });

    mcp_server.AddTool(
        "self.reminders.update",
        "Change an existing reminder and keep its id, for example when the user asks to move it to "
        "another time. The arguments are the ones of `self.reminders.create` plus `id`, so the "
        "schedule must be sent in full again: read the current one with `self.reminders.list` "
        "first. Leave `text` out to keep the current text. A one-shot reminder that already played "
        "comes back and plays again at the new time. If the task is currently `awaiting_response`, "
        "use `self.reminders.respond` with action `snooze` instead of changing its schedule.",
        UpdateProperties(), [&manager](const PropertyList& properties) -> ToolResult {
            Schedule schedule;
            std::string error = BuildScheduleFromProperties(properties, schedule);
            if (!error.empty()) {
                return std::unexpected(error);
            }
            const uint32_t id = static_cast<uint32_t>(properties["id"].value<int>());
            const std::string text = properties["text"].value<std::string>();
            if (!manager.UpdateReminder(id, text, schedule.type, schedule.delay_ms,
                                        schedule.trigger_unix, schedule.daily_hour,
                                        schedule.daily_minute, schedule.weekly_weekday,
                                        schedule.weekly_hour, schedule.weekly_minute, error)) {
                return std::unexpected(ErrorText(error));
            }
            return DescribeTask(manager, id);
        });

    mcp_server.AddTool("self.reminders.cancel",
                       "Cancel one reminder by `id` only when the user explicitly asks to cancel it. Read the id "
                       "with `self.reminders.list` first; never guess it. Do not use cancel to mark a "
                       "reminder as handled; use `self.reminders.respond` with action `complete`.",
                       PropertyList({Property("id", kPropertyTypeInteger, 1,
                                              std::numeric_limits<int>::max())}),
                       [&manager](const PropertyList& properties) -> ToolResult {
                           const uint32_t id =
                               static_cast<uint32_t>(properties["id"].value<int>());
                           std::string error;
                           if (!manager.CancelReminder(id, error)) {
                               return std::unexpected(ErrorText(error));
                           }
                           return true;
                       });

    mcp_server.AddTool("self.reminders.cancel_all",
                       "Cancel every reminder stored on the device. Only use it when the user asks "
                       "to clear all of them.",
                       PropertyList(), [&manager](const PropertyList&) -> ToolResult {
                           std::string error;
                           if (!manager.CancelAll(error)) {
                               return std::unexpected(ErrorText(error));
                           }
                           return true;
                       });

    ESP_LOGI(TAG, "Reminder tools registered");
}

