#ifndef REMINDER_MANAGER_H_
#define REMINDER_MANAGER_H_

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

// OnPlaybackFinished() reports the outcome of a reminder utterance with the enum the PCM player
// already uses, so Application forwards its finished callback without a translation table.
#include "pcm_tts_player.h"

enum class ReminderType {
    kRelative,  // 相对时间：delay_ms 后触发
    kAbsolute,  // 绝对时间：trigger_unix 触发
    kDaily,     // 每天：每天 hour:minute 触发
    kWeekly,    // 每周：每周 weekday 的 hour:minute 触发
};

enum class ReminderState {
    kPending,    // 等待执行（未到期）
    kDue,        // 已到期，等待播放
    kPlaying,    // 正在播放
    kAwaitingResponse, // 播报完成，等待用户明确回应
    kCompleted,  // 已完成
    kCancelled,  // 已取消
    kPaused,     // 自动重播次数耗尽，等待用户主动处理
};

struct ReminderTask {
    uint32_t id = 0;                 // 1 起单调递增，0 无效
    std::string text;                // UTF-8，≤120 字节
    ReminderType type = ReminderType::kRelative;
    ReminderState state = ReminderState::kPending;
    bool recurring_active = false;   // 周期任务本身是否仍有效；一次性任务恒为 false
    uint64_t trigger_ms = 0;         // 相对时间任务：esp_timer_get_time()/1000 + delay_ms
    int64_t trigger_unix = 0;        // 绝对/每天/每周：下次触发的 Unix 秒
    uint64_t created_ms = 0;         // 创建时的 esp_timer_get_time()/1000
    uint64_t retry_not_before_ms = 0; // 仅运行期：超时/稍后提醒的重播门限，不写入 NVS
    uint8_t retry_count = 0;           // 自动重播次数，持久化到 NVS
    int64_t last_fired_unix = 0;     // 最近一次触发完成的 Unix 秒，0 表示从未触发
    // 周期规则参数
    int daily_hour = 0;      // 0-23，kDaily 用
    int daily_minute = 0;    // 0-59，kDaily 用
    int weekly_weekday = 0;  // 0=周日…6=周六，kWeekly 用
    int weekly_hour = 0;
    int weekly_minute = 0;
};

/**
 * Owns the reminder tasks: create, list, update, cancel, due detection, recurring schedules and
 * NVS persistence (namespace "reminder_tasks", key "tasks_v1").
 *
 * It never plays audio, holds no Application pointer and never calls Schedule(). The main task
 * drives it from the clock tick and feeds PCM playback results back through
 * OnPlaybackFinished(). Every public method takes the internal mutex, so MCP tool callbacks and
 * the main task may call it concurrently.
 *
 * State transitions:
 *  kPending -[trigger time reached]-> kDue -[MarkPlaying]-> kPlaying
 *  kPlaying -[OnPlaybackFinished(kSuccess)]-> kAwaitingResponse
 *  kAwaitingResponse -[explicit complete]-> kCompleted (one shot) / kPending (recurring)
 *  kAwaitingResponse -[snooze or no response]-> kDue (retried after a delay)
 *  kPlaying -[OnPlaybackFinished(other)]-> kDue
 */
class ReminderManager {
public:
    ReminderManager();
    ~ReminderManager();

    ReminderManager(const ReminderManager&) = delete;
    ReminderManager& operator=(const ReminderManager&) = delete;

    // Restores the tasks from NVS and settles the ones that expired while the device was off.
    // Relative tasks are re-armed for their original delay, because trigger_ms counts from boot.
    void Init();

    // Creates a task and returns its id (> 0), or 0 with out_error set to one of:
    // empty-text, text-too-long, task-limit-reached, time-not-synced, invalid-time.
    uint32_t CreateReminder(const std::string& text, ReminderType type, uint64_t delay_ms,
                            int64_t trigger_unix, int daily_hour, int daily_minute,
                            int weekly_weekday, int weekly_hour, int weekly_minute,
                            std::string& out_error);

    // Rewrites the schedule of an existing task and returns it to kPending.
    // out_error: not-found, plus the CreateReminder() codes for invalid input.
    bool UpdateReminder(uint32_t id, const std::string& text, ReminderType type, uint64_t delay_ms,
                        int64_t trigger_unix, int daily_hour, int daily_minute, int weekly_weekday,
                        int weekly_hour, int weekly_minute, std::string& out_error);

    // out_error: not-found when the id is unknown or already cancelled.
    bool CancelReminder(uint32_t id, std::string& out_error);
    bool CancelAll(std::string& out_error);

    // Snapshot of every task that is not cancelled, oldest id first.
    std::vector<ReminderTask> ListReminders() const;

    // Marks the pending tasks whose trigger time has passed as due, persists the changes and
    // returns how many tasks changed state.
    size_t MarkDueTasks();

    // Copies the lowest-id due task into out. Read-only: only MarkPlaying() moves it to kPlaying,
    // so a failed playback start leaves the task due for the next tick.
    bool TakeNextDueTask(ReminderTask& out);

    // kDue -> kPlaying, recording last_fired_unix.
    void MarkPlaying(uint32_t id);

    // Rolls kPlaying back to kDue so the task is retried on a later tick.
    void MarkPlaybackFailed(uint32_t id);

    // A successful utterance starts a response window; it does not complete the reminder.
    void OnPlaybackFinished(uint32_t id, PcmTtsPlayer::Error error);
    bool GetAwaitingResponseTask(ReminderTask& out) const;
    // Resolves the specified awaiting/paused reminder. Snooze returns it to due after delay_ms.
    bool ResolveReminderResponse(uint32_t id, bool completed, uint64_t delay_ms,
                                 std::string& out_error);
    // A genuine response timeout increments retry_count; after two retries the task is paused.
    bool RetryAwaitingResponse(uint32_t id, uint64_t delay_ms);
    // Defers an awaiting task after an infrastructure/listening failure without consuming a retry.
    bool DeferAwaitingResponse(uint32_t id, uint64_t delay_ms);

    // "pending"/"due"/"playing"/"awaiting_response"/"completed"/"cancelled"/"paused".
    static const char* StateName(ReminderState state);
    // "relative"/"absolute"/"daily"/"weekly".
    static const char* TypeName(ReminderType type);

private:
    static uint64_t NowMonotonicMs();
    static int64_t NowUnixSeconds();
    // The device clock is only trusted past 2025-01-01 UTC, like the rest of the firmware.
    static bool IsSystemTimeSynced();
    static bool IsRecurring(ReminderType type);
    static int64_t ComputeNextDaily(int hour, int minute, int64_t after_unix);
    static int64_t ComputeNextWeekly(int weekday, int hour, int minute, int64_t after_unix);
    static int64_t ComputeNextTrigger(const ReminderTask& task, int64_t after_unix);
    // Validates the schedule for |type| and fills the trigger/rule fields of |task|. Returns an
    // empty string on success, otherwise one of the CreateReminder() error codes.
    static std::string BuildSchedule(ReminderType type, uint64_t delay_ms, int64_t trigger_unix,
                                    int daily_hour, int daily_minute, int weekly_weekday,
                                    int weekly_hour, int weekly_minute, uint64_t created_ms,
                                    ReminderTask& task);

    ReminderTask* FindLocked(uint32_t id);
    const ReminderTask* FindLocked(uint32_t id) const;
    size_t ActiveCountLocked() const;
    // Drops the oldest finished tasks when the stored list is full, keeping the newest id as the
    // monotonic anchor so restored ids never collide.
    void PruneFinishedLocked();
    bool LoadLocked();
    void SaveLocked();

    mutable std::mutex mutex_;
    std::vector<ReminderTask> tasks_;
    uint32_t next_id_ = 1;
};

#endif  // REMINDER_MANAGER_H_
