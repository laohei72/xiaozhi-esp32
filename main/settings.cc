#include "settings.h"

#include <esp_log.h>
#include <nvs_flash.h>

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
            esp_err_t err = nvs_commit(nvs_handle_);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Failed to commit namespace %s: %s", ns_.c_str(),
                         esp_err_to_name(err));
            }
        }
        nvs_close(nvs_handle_);
    }
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
        ESP_LOGE(TAG, "Failed to write %s/%s: %s", ns_.c_str(), key.c_str(), esp_err_to_name(err));
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