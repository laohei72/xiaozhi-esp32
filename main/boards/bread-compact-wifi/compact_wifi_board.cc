#include "wifi_board.h"
#include "codecs/no_audio_codec.h"
#include "display/oled_display.h"
#include "system_reset.h"
#include "application.h"
#include "button.h"
#include "config.h"
#include "mcp_server.h"
#include "lamp_controller.h"
#include "led/single_led.h"
#include "assets/lang_config.h"

#include <esp_log.h>
#include <driver/i2c_master.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_panel_vendor.h>
#include <esp_timer.h>

#ifdef SH1106
#include <esp_lcd_panel_sh1106.h>
#endif

#define TAG "CompactWifiBoard"

class CompactWifiBoard : public WifiBoard {
private:
    i2c_master_bus_handle_t display_i2c_bus_;
    esp_lcd_panel_io_handle_t panel_io_ = nullptr;
    esp_lcd_panel_handle_t panel_ = nullptr;
    Display* display_ = nullptr;
    Button boot_button_;
    Button touch_button_;
    Button volume_up_button_;
    Button volume_down_button_;
    Button function_button_;
    Button mic_mute_button_;
    esp_timer_handle_t pir_poll_timer_ = nullptr;
    int pir_last_level_ = 0;
    int64_t pir_high_since_us_ = 0;
    int64_t pir_last_trigger_us_ = 0;
    bool pir_high_triggered_ = false;

    void InitializePirSensor() {
        gpio_config_t config = {};
        config.pin_bit_mask = 1ULL << PIR_SENSOR_GPIO;
        config.mode = GPIO_MODE_INPUT;
        config.pull_up_en = GPIO_PULLUP_DISABLE;
        config.pull_down_en = GPIO_PULLDOWN_ENABLE;
        config.intr_type = GPIO_INTR_DISABLE;
        ESP_ERROR_CHECK(gpio_config(&config));

        pir_last_level_ = gpio_get_level(PIR_SENSOR_GPIO);
        if (pir_last_level_ != 0) {
            // Ignore a high level during the sensor's power-on stabilization period.
            pir_high_since_us_ = esp_timer_get_time();
            pir_high_triggered_ = true;
        }

        esp_timer_create_args_t timer_args = {
            .callback = [](void* arg) {
                auto* board = static_cast<CompactWifiBoard*>(arg);
                const int level = gpio_get_level(PIR_SENSOR_GPIO);
                const int64_t now = esp_timer_get_time();
                constexpr int64_t kStableHighUs = 2500LL * 1000;
                constexpr int64_t kCooldownUs = 20LL * 1000 * 1000;

                if (level != 0) {
                    if (board->pir_last_level_ == 0) {
                        board->pir_high_since_us_ = now;
                        board->pir_high_triggered_ = false;
                    }
                    if (!board->pir_high_triggered_ && board->pir_high_since_us_ != 0 &&
                        now - board->pir_high_since_us_ >= kStableHighUs) {
                        board->pir_high_triggered_ = true;
                        if (board->pir_last_trigger_us_ == 0 ||
                            now - board->pir_last_trigger_us_ >= kCooldownUs) {
                            board->pir_last_trigger_us_ = now;
                            ESP_LOGI(TAG, "PIR motion detected");
Application::GetInstance().StartProactiveGreeting();
                        }
                    }
                } else {
                    board->pir_high_since_us_ = 0;
                    board->pir_high_triggered_ = false;
                }
                board->pir_last_level_ = level;
            },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "pir_poll",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&timer_args, &pir_poll_timer_));
        ESP_ERROR_CHECK(esp_timer_start_periodic(pir_poll_timer_, 100 * 1000));
    }

    void InitializeDisplayI2c() {
        i2c_master_bus_config_t bus_config = {
            .i2c_port = (i2c_port_t)0,
            .sda_io_num = DISPLAY_SDA_PIN,
            .scl_io_num = DISPLAY_SCL_PIN,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .intr_priority = 0,
            .trans_queue_depth = 0,
            .flags = {
                .enable_internal_pullup = 1,
            },
        };
        ESP_ERROR_CHECK(i2c_new_master_bus(&bus_config, &display_i2c_bus_));
    }

    void InitializeSsd1306Display() {
        // SSD1306 config
        esp_lcd_panel_io_i2c_config_t io_config = {
            .dev_addr = 0x3C,
            .scl_speed_hz = 400 * 1000,
            .control_phase_bytes = 1,
            .dc_bit_offset = 6,
            .lcd_cmd_bits = 8,
            .lcd_param_bits = 8,
            .on_color_trans_done = nullptr,
            .user_ctx = nullptr,
            .flags = {
                .dc_low_on_data = 0,
                .disable_control_phase = 0,
            },
        };

        ESP_ERROR_CHECK(esp_lcd_new_panel_io_i2c(display_i2c_bus_, &io_config, &panel_io_));

        ESP_LOGI(TAG, "Install SSD1306 driver");
        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = GPIO_NUM_NC;
        panel_config.bits_per_pixel = 1;

        esp_lcd_panel_ssd1306_config_t ssd1306_config = {
            .height = static_cast<uint8_t>(DISPLAY_HEIGHT),
        };
        panel_config.vendor_config = &ssd1306_config;

#ifdef SH1106
        ESP_ERROR_CHECK(esp_lcd_new_panel_sh1106(panel_io_, &panel_config, &panel_));
#else
        ESP_ERROR_CHECK(esp_lcd_new_panel_ssd1306(panel_io_, &panel_config, &panel_));
#endif
        ESP_LOGI(TAG, "SSD1306 driver installed");

        // Reset the display
        ESP_ERROR_CHECK(esp_lcd_panel_reset(panel_));
        if (esp_lcd_panel_init(panel_) != ESP_OK) {
            ESP_LOGE(TAG, "Failed to initialize display");
            display_ = new NoDisplay();
            return;
        }
        ESP_ERROR_CHECK(esp_lcd_panel_invert_color(panel_, false));

        // Set the display to on
        ESP_LOGI(TAG, "Turning display on");
        ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_, true));

        display_ = new OledDisplay(panel_io_, panel_, DISPLAY_WIDTH, DISPLAY_HEIGHT, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);
    }

    void InitializeButtons() {
        function_button_.OnClick([this]() {
            // A short press manually starts the normal wake-word interaction.
            Application::GetInstance().WakeWordInvoke("小智小智");
        });
        function_button_.OnDoubleClick([this]() {
            ReconnectWifi();
        });
        function_button_.OnLongPress([this]() {
            EnterWifiConfigMode();
        });

        mic_mute_button_.OnClick([this]() {
            auto& audio = Application::GetInstance().GetAudioService();
            const bool muted = !audio.IsMicrophoneMuted();
            audio.SetMicrophoneMuted(muted);
            GetDisplay()->ShowNotification(muted ? "麦克风已静音" : "麦克风已恢复");
        });

        boot_button_.OnClick([this]() {
            auto& app = Application::GetInstance();
            if (app.GetDeviceState() == kDeviceStateStarting) {
                EnterWifiConfigMode();
                return;
            }
            app.ToggleChatState();
        });
        boot_button_.OnDoubleClick([this]() {
            // Stage-4a PCM TTS self test: plays a local 24 kHz tone through AudioService.
            auto& app = Application::GetInstance();
            if (app.GetDeviceState() == kDeviceStateIdle) {
                app.StartPcmTtsTest();
            }
        });
        boot_button_.OnLongPress([this]() {
            Application::GetInstance().StartNotifyPlayerTest();
        });
        touch_button_.OnPressDown([this]() {
            Application::GetInstance().StartListening();
        });
        touch_button_.OnPressUp([this]() {
            Application::GetInstance().StopListening();
        });

        volume_up_button_.OnClick([this]() {
            auto codec = GetAudioCodec();
            auto volume = codec->output_volume() + 10;
            if (volume > 100) {
                volume = 100;
            }
            codec->SetOutputVolume(volume);
            GetDisplay()->ShowNotification(Lang::Strings::VOLUME + std::to_string(volume));
        });

        volume_up_button_.OnLongPress([this]() {
            GetAudioCodec()->SetOutputVolume(100);
            GetDisplay()->ShowNotification(Lang::Strings::MAX_VOLUME);
        });

        volume_down_button_.OnClick([this]() {
            auto codec = GetAudioCodec();
            auto volume = codec->output_volume() - 10;
            if (volume < 0) {
                volume = 0;
            }
            codec->SetOutputVolume(volume);
            GetDisplay()->ShowNotification(Lang::Strings::VOLUME + std::to_string(volume));
        });

        volume_down_button_.OnLongPress([this]() {
            GetAudioCodec()->SetOutputVolume(0);
            GetDisplay()->ShowNotification(Lang::Strings::MUTED);
        });
    }

    // 物联网初始化，逐步迁移到 MCP 协议
    void InitializeTools() {
        static LampController lamp(LAMP_GPIO);
    }

public:
    CompactWifiBoard() :
        boot_button_(BOOT_BUTTON_GPIO, false, 2000),
        touch_button_(TOUCH_BUTTON_GPIO),
        volume_up_button_(VOLUME_UP_BUTTON_GPIO),
        volume_down_button_(VOLUME_DOWN_BUTTON_GPIO),
        function_button_(FUNCTION_BUTTON_GPIO, false, 2000),
        mic_mute_button_(MIC_MUTE_BUTTON_GPIO) {
        InitializeDisplayI2c();
        InitializeSsd1306Display();
        InitializeButtons();
        InitializePirSensor();
        InitializeTools();
    }

    virtual Led* GetLed() override {
        static SingleLed led(BUILTIN_LED_GPIO);
        return &led;
    }

    virtual AudioCodec* GetAudioCodec() override {
#ifdef AUDIO_I2S_METHOD_SIMPLEX
        static NoAudioCodecSimplex audio_codec(AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE,
            AUDIO_I2S_SPK_GPIO_BCLK, AUDIO_I2S_SPK_GPIO_LRCK, AUDIO_I2S_SPK_GPIO_DOUT, AUDIO_I2S_MIC_GPIO_SCK, AUDIO_I2S_MIC_GPIO_WS, AUDIO_I2S_MIC_GPIO_DIN);
#else
        static NoAudioCodecDuplex audio_codec(AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE,
            AUDIO_I2S_GPIO_BCLK, AUDIO_I2S_GPIO_WS, AUDIO_I2S_GPIO_DOUT, AUDIO_I2S_GPIO_DIN);
#endif
        return &audio_codec;
    }

    virtual Display* GetDisplay() override {
        return display_;
    }
};

DECLARE_BOARD(CompactWifiBoard);
