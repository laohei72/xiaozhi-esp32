#include "settings.h"

#include <esp_log.h>
#include <nvs_flash.h>
#include <nvs.h>

#define TAG "Settings"

Settings::Settings(const std::string& ns, bool read_write) : ns_(ns), read_write_(read_write) {
    esp_err_t err = nvs_open(ns.c_str(), read_write_ ? NVS_READWRITE : NVS_READONLY, &nvs_handle_);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Failed to open namespace %s: %s", ns_.c_str(), esp_err_to_name(err));
        nvs_handle_ = 0;
    }
}

Settings::~Settings() {
    if (nvs_handle_ != 0) {
        if (read_write_ && dirty_) {
            Commit();
        }
        nvs_close(nvs_handle_);
    }
}

bool Settings::Commit() {
    if (!read_write_ || nvs_handle_ == 0) {
        return false;
    }
    if (!dirty_) {
        return true;
    }

    const esp_err_t err = nvs_commit(nvs_handle_);
    // Do not silently retry a failed commit from the destructor. The caller must see the
    // failure and decide whether to retry with a smaller value or report a storage error.
    dirty_ = false;
    if (err != ESP_OK) {
        nvs_stats_t stats{};
        const esp_err_t stats_err = nvs_get_stats(nullptr, &stats);
        if (stats_err == ESP_OK) {
            ESP_LOGE(TAG,
                     "NVS commit failed: namespace=%s err=%s(%d) used=%u free=%u available=%u total=%u",
                     ns_.c_str(), esp_err_to_name(err), static_cast<int>(err),
                     static_cast<unsigned>(stats.used_entries),
                     static_cast<unsigned>(stats.free_entries),
                     static_cast<unsigned>(stats.available_entries),
                     static_cast<unsigned>(stats.total_entries));
        } else {
            ESP_LOGE(TAG, "NVS commit failed: namespace=%s err=%s(%d), stats_err=%s",
                     ns_.c_str(), esp_err_to_name(err), static_cast<int>(err),
                     esp_err_to_name(stats_err));
        }
        return false;
    }
    return true;
}

std::string Settings::GetString(const std::string& key, const std::string& default_value) {
    if (nvs_handle_ == 0) {
        return default_value;
    }

    size_t length = 0;
    if (nvs_get_str(nvs_handle_, key.c_str(), nullptr, &length) != ESP_OK) {
        return default_value;
    }

    std::string value;
    value.resize(length);
    esp_err_t err = nvs_get_str(nvs_handle_, key.c_str(), value.data(), &length);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "Failed to read %s/%s: %s", ns_.c_str(), key.c_str(), esp_err_to_name(err));
        return default_value;
    }
    while (!value.empty() && value.back() == '\0') {
        value.pop_back();
    }
    return value;
}

bool Settings::SetString(const std::string& key, const std::string& value) {
    if (!read_write_) {
        ESP_LOGW(TAG, "Namespace %s is not open for writing", ns_.c_str());
        return false;
    }
    if (nvs_handle_ == 0) {
        return false;
    }
    esp_err_t err = nvs_set_str(nvs_handle_, key.c_str(), value.c_str());
    if (err != ESP_OK) {
        nvs_stats_t stats{};
        const esp_err_t stats_err = nvs_get_stats(nullptr, &stats);
        if (stats_err == ESP_OK) {
            size_t namespace_used = 0;
            const esp_err_t namespace_err = nvs_get_used_entry_count(nvs_handle_, &namespace_used);
            ESP_LOGE(TAG,
                     "NVS set_str failed: %s/%s bytes=%u err=%s(%d) used=%u free=%u available=%u total=%u namespace_used=%u namespace_stats=%s",
                     ns_.c_str(), key.c_str(), static_cast<unsigned>(value.size() + 1),
                     esp_err_to_name(err), static_cast<int>(err),
                     static_cast<unsigned>(stats.used_entries),
                     static_cast<unsigned>(stats.free_entries),
                     static_cast<unsigned>(stats.available_entries),
                     static_cast<unsigned>(stats.total_entries),
                     static_cast<unsigned>(namespace_used),
                     namespace_err == ESP_OK ? "ok" : esp_err_to_name(namespace_err));
        } else {
            ESP_LOGE(TAG, "NVS set_str failed: %s/%s bytes=%u err=%s(%d), stats_err=%s",
                     ns_.c_str(), key.c_str(), static_cast<unsigned>(value.size() + 1),
                     esp_err_to_name(err), static_cast<int>(err), esp_err_to_name(stats_err));
        }
        return false;
    }
    dirty_ = true;
    return true;
}

int32_t Settings::GetInt(const std::string& key, int32_t default_value) {
    if (nvs_handle_ == 0) {
        return default_value;
    }

    int32_t value;
    if (nvs_get_i32(nvs_handle_, key.c_str(), &value) != ESP_OK) {
        return default_value;
    }
    return value;
}

bool Settings::SetInt(const std::string& key, int32_t value) {
    if (!read_write_) {
        ESP_LOGW(TAG, "Namespace %s is not open for writing", ns_.c_str());
        return false;
    }
    if (nvs_handle_ == 0) {
        return false;
    }
    esp_err_t err = nvs_set_i32(nvs_handle_, key.c_str(), value);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to write %s/%s: %s", ns_.c_str(), key.c_str(), esp_err_to_name(err));
        return false;
    }
    dirty_ = true;
    return true;
}

bool Settings::GetBool(const std::string& key, bool default_value) {
    if (nvs_handle_ == 0) {
        return default_value;
    }

    uint8_t value;
    if (nvs_get_u8(nvs_handle_, key.c_str(), &value) != ESP_OK) {
        return default_value;
    }
    return value != 0;
}

bool Settings::SetBool(const std::string& key, bool value) {
    if (!read_write_) {
        ESP_LOGW(TAG, "Namespace %s is not open for writing", ns_.c_str());
        return false;
    }
    if (nvs_handle_ == 0) {
        return false;
    }
    esp_err_t err = nvs_set_u8(nvs_handle_, key.c_str(), value ? 1 : 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to write %s/%s: %s", ns_.c_str(), key.c_str(), esp_err_to_name(err));
        return false;
    }
    dirty_ = true;
    return true;
}

void Settings::EraseKey(const std::string& key) {
    if (!read_write_) {
        ESP_LOGW(TAG, "Namespace %s is not open for writing", ns_.c_str());
        return;
    }
    if (nvs_handle_ == 0) {
        return;
    }
    esp_err_t err = nvs_erase_key(nvs_handle_, key.c_str());
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGE(TAG, "Failed to erase %s/%s: %s", ns_.c_str(), key.c_str(), esp_err_to_name(err));
    }
}

bool Settings::EraseAll() {
    if (!read_write_) {
        ESP_LOGW(TAG, "Namespace %s is not open for writing", ns_.c_str());
        return false;
    }
    if (nvs_handle_ == 0) {
        return false;
    }
    esp_err_t err = nvs_erase_all(nvs_handle_);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to erase namespace %s: %s", ns_.c_str(), esp_err_to_name(err));
        return false;
    }
    dirty_ = true;
    return true;
}