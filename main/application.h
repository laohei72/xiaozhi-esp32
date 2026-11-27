#ifndef _APPLICATION_H_
#define _APPLICATION_H_

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>
#include <esp_timer.h>

#include <string>
#include <mutex>
#include <deque>
#include <memory>
#include <functional>
#include <cstdint>
#include <atomic>
#include <vector>

#include "protocol.h"
#include "ota.h"
#include "audio_service.h"
#include "device_state.h"
#include "device_state_machine.h"
#include "notify/notify_player.h"
#include "reminder/baidu_tts_source.h"
#include "reminder/pcm_tts_player.h"
#include "reminder/reminder_manager.h"

// Main event bits
#define MAIN_EVENT_SCHEDULE             (1 << 0)
#define MAIN_EVENT_SEND_AUDIO           (1 << 1)
#define MAIN_EVENT_WAKE_WORD_DETECTED   (1 << 2)
#define MAIN_EVENT_VAD_CHANGE           (1 << 3)
#define MAIN_EVENT_ERROR                (1 << 4)
#define MAIN_EVENT_ACTIVATION_DONE      (1 << 5)
#define MAIN_EVENT_CLOCK_TICK           (1 << 6)
#define MAIN_EVENT_NETWORK_CONNECTED    (1 << 7)
#define MAIN_EVENT_NETWORK_DISCONNECTED (1 << 8)
#define MAIN_EVENT_TOGGLE_CHAT          (1 << 9)
#define MAIN_EVENT_START_LISTENING      (1 << 10)
#define MAIN_EVENT_STOP_LISTENING       (1 << 11)
#define MAIN_EVENT_STATE_CHANGED        (1 << 12)
#define MAIN_EVENT_PLAYBACK_DRAINED     (1 << 13)
#define MAIN_EVENT_STARTUP_HINT_TIMEOUT (1 << 14)
#define MAIN_EVENT_PROTOCOL_PENDING (1 << 15)

enum AecMode {
    kAecOff,
    kAecOnDeviceSide,
    kAecOnServerSide,
};

class Application {
public:
    static Application& GetInstance() {
        static Application instance;
        return instance;
    }
    // Delete copy constructor and assignment operator
    Application(const Application&) = delete;
    Application& operator=(const Application&) = delete;

    /**
     * Initialize the application
     * This sets up display, audio, network callbacks, etc.
     * Network connection starts asynchronously.
     */
    void Initialize();

    /**
     * Run the main event loop
     * This function runs in the main task and never returns.
     * It handles all events including network, state changes, and user interactions.
     */
    void Run();

    DeviceState GetDeviceState() const { return state_machine_.GetState(); }
    bool IsVoiceDetected() const { return audio_service_.IsVoiceDetected(); }
    
    /**
     * Request state transition
     * Returns true if transition was successful
     */
    bool SetDeviceState(DeviceState state);

    /**
     * Schedule a callback to be executed in the main task
     */
    void Schedule(std::function<void()>&& callback);
    void NotifyProtocolPending() { xEventGroupSetBits(event_group_, MAIN_EVENT_PROTOCOL_PENDING); }

    /**
     * Alert with status, message, emotion and optional sound
     */
    void Alert(const char* status, const char* message, const char* emotion = "", const std::string_view& sound = "");
    void DismissAlert();

    void AbortSpeaking(AbortReason reason);

    /**
     * Toggle chat state (event-based, thread-safe)
     * Sends MAIN_EVENT_TOGGLE_CHAT to be handled in Run()
     */
    void ToggleChatState();

    /**
     * Start listening (event-based, thread-safe)
     * Sends MAIN_EVENT_START_LISTENING to be handled in Run()
     */
    void StartListening();

    /**
     * Stop listening (event-based, thread-safe)
     * Sends MAIN_EVENT_STOP_LISTENING to be handled in Run()
     */
    void StopListening();

    void Reboot();
    void WakeWordInvoke(const std::string& wake_word);
    bool UpgradeFirmware(const std::string& url, const std::string& version = "");
    bool CanEnterSleepMode();
    void SendMcpMessage(const std::string& payload);
    void RegisterMcpBroadcastCallback(std::function<void(const std::string&)> callback);
    void SetAecMode(AecMode mode);
    AecMode GetAecMode() const { return aec_mode_; }
    void PlaySound(const std::string_view& sound);
    void StartNotifyPlayerTest();
    void StartPcmTtsTest();
    AudioService& GetAudioService() { return audio_service_; }
    // Reminder tasks (main/reminder/reminder_manager.*). Thread-safe, so the MCP tools call it
    // from wherever they run; the utterances are started by the main task only.
    ReminderManager& GetReminderManager() { return reminder_manager_; }
    
    /**
     * Reset protocol resources (thread-safe)
     * Can be called from any task to release resources allocated after network connected
     * This includes closing audio channel, resetting protocol and ota objects
     */
    void ResetProtocol();

private:
    Application();
    ~Application();

    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    std::unique_ptr<Protocol> protocol_;
    EventGroupHandle_t event_group_ = nullptr;
    esp_timer_handle_t clock_timer_handle_ = nullptr;
    esp_timer_handle_t startup_wake_word_hint_timer_ = nullptr;
    DeviceStateMachine state_machine_;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    AecMode aec_mode_ = kAecOff;
    std::string last_error_message_;
    std::string startup_wake_word_hint_message_;
    AudioService audio_service_;
    NotifyPlayer notify_player_;
    // Reminder tasks. The clock tick settles them and starts their utterance through the PCM
    // player below.
    ReminderManager reminder_manager_;
    // Baidu TTS transport of the PCM utterances. Declared before the player so it outlives the
    // player's worker task, which owns every Http call of a session.
    BaiduTtsSource baidu_tts_source_;
    PcmTtsPlayer pcm_tts_player_;
    uint32_t notification_playback_id_ = 0;
    // Session id of the PCM TTS utterance the UI is currently showing; 0 when none is active.
    uint32_t pcm_tts_session_id_ = 0;
    // Reminder the active PCM utterance speaks, 0 when the session is the self test. The player's
    // result is fed back to the manager with this id when the session ends.
    uint32_t pcm_tts_reminder_id_ = 0;
    // Monotonic deadline (ms) before which a reminder whose utterance failed or was interrupted is
    // not retried, so a barge-in does not replay the task on the very next tick.
    uint64_t reminder_retry_not_before_ms_ = 0;
    // The local reminder has finished speaking; listening remains open through unlimited
    // assistant/user turns. This deadline is used only while waiting for server processing.
    uint64_t reminder_response_deadline_ms_ = 0;
    bool reminder_response_listening_ = false;
    bool reminder_response_resolution_pending_ = false;
    // Temporary diagnostics for reminder-response microphone uplink (10-second windows).
    uint32_t reminder_audio_popped_ = 0;
    uint32_t reminder_audio_attempted_ = 0;
    uint32_t reminder_audio_sent_ = 0;
    uint32_t reminder_audio_failed_ = 0;
    uint32_t reminder_audio_discarded_ = 0;
    std::atomic<bool> reminder_response_vad_speaking_{false};
    // Server Opus frames dropped while a PCM session owned the playback path (§9.5).
    std::atomic<uint32_t> pcm_tts_dropped_packets_{0};
    std::unique_ptr<Ota> ota_;

    std::function<void(const std::string&)> mcp_broadcast_callback_;

    bool has_server_time_ = false;
    bool startup_completed_ = false;
    bool pending_startup_wake_word_hint_ = false;
    bool aborted_ = false;
    bool assets_version_checked_ = false;
    bool play_popup_on_listening_ = false;  // Flag to play popup sound after state changes to listening
    bool pending_listening_start_ = false;  // Waiting for playback to drain before starting listening (auto mode)
    int clock_ticks_ = 0;
    TaskHandle_t activation_task_handle_ = nullptr;


    // Event handlers
    void HandleStateChangedEvent();
    void HandleToggleChatEvent();
    void HandleStartListeningEvent();
    void HandleStopListeningEvent();
    void HandleNetworkConnectedEvent();
    void HandleNetworkDisconnectedEvent();
    void HandleActivationDoneEvent();
    void HandleWakeWordDetectedEvent();
    void ContinueOpenAudioChannel(ListeningMode mode);
    void BeginWakeWordInvoke(const std::string& wake_word);
    void ContinueWakeWordInvoke(const std::string& wake_word);
    void StartListeningAudio();
    void ConfigureWakeWordForListening();
    void ShowStartupWakeWordHint();
    void HideStartupWakeWordHint();
    void StartNotification(std::string audio_url, std::vector<NotifySubtitle> subtitles);
    void StopNotification();
    void HandleNotificationFinished(uint32_t playback_id, bool success);
    // PCM TTS (main/reminder/pcm_tts_player.*). StartPcmTtsSession() must run in the main task.
    bool StartPcmTtsSession(const char* source, int sample_rate, int channels,
                            PcmTtsPlayer::ChunkReader reader,
                            PcmTtsPlayer::PrepareCallback prepare = nullptr,
                            PcmTtsPlayer::CloseCallback close = nullptr);
    // Cancels the active PCM utterance; returns true when one was running.
    bool StopPcmTts(const char* reason);
    // Runs on the player's worker task: only schedules work for the main task.
    void HandlePcmTtsFinished(uint32_t session_id, PcmTtsPlayer::Error error);
    // Clock tick: settles due reminders and starts the next utterance when the device is idle.
    void HandleReminderTick();
    // Starts the PCM utterance that speaks |task|; returns false when it could not be started.
    bool StartReminderPlayback(const ReminderTask& task);
    void StartReminderResponseListening();

    // Activation task (runs in background)
    void ActivationTask();

    // Helper methods
    void CheckAssetsVersion();
    void CheckNewVersion();
    void InitializeProtocol();
    void ShowActivationCode(const std::string& code, const std::string& message);
    void SetListeningMode(ListeningMode mode);
    ListeningMode GetDefaultListeningMode() const;
    
    // State change handler called by state machine
    void OnStateChanged(DeviceState old_state, DeviceState new_state);
};


class TaskPriorityReset {
public:
    TaskPriorityReset(BaseType_t priority) {
        original_priority_ = uxTaskPriorityGet(NULL);
        vTaskPrioritySet(NULL, priority);
    }
    ~TaskPriorityReset() {
        vTaskPrioritySet(NULL, original_priority_);
    }

private:
    BaseType_t original_priority_;
};

#endif // _APPLICATION_H_
