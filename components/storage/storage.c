#include "storage.h"

#include <dirent.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

#include "esp_log.h"
#include "esp_check.h"
#include "esp_spiffs.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "storage";
static const char *NVS_NS = "mamba";
static storage_info_t s_cached_info;

static void load_string(nvs_handle_t nvs, const char *key, char *value, size_t value_len)
{
    size_t len = value_len;
    esp_err_t err = nvs_get_str(nvs, key, value, &len);
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        ESP_LOGW(TAG, "nvs_get_str(%s): %s", key, esp_err_to_name(err));
    }
}

esp_err_t storage_load_config(mamba_config_t *config)
{
    mamba_config_defaults(config);
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NVS_NS, NVS_READONLY, &nvs);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;
    }
    ESP_RETURN_ON_ERROR(err, TAG, "open nvs");

    load_string(nvs, "device", config->device_name, sizeof(config->device_name));
    load_string(nvs, "alarm_file", config->alarm_file, sizeof(config->alarm_file));
    load_string(nvs, "power_file", config->power_on_file, sizeof(config->power_on_file));
    load_string(nvs, "can_filter", config->can_filter, sizeof(config->can_filter));
    load_string(nvs, "wifi_ssid", config->wifi_ssid, sizeof(config->wifi_ssid));
    load_string(nvs, "wifi_pass", config->wifi_password, sizeof(config->wifi_password));
    if (strcmp(config->device_name, "Mamba C3") == 0) {
        strlcpy(config->device_name, MAMBA_DEFAULT_DEVICE_NAME, sizeof(config->device_name));
    }
    if (strcmp(config->can_filter, "0x201-0x208,0x200,0x1FF") == 0) {
        config->can_filter[0] = '\0';
    }
    nvs_get_u32(nvs, "can_bitrate", &config->can_bitrate);
    nvs_get_u32(nvs, "tel_ms", &config->telemetry_interval_ms);
    nvs_get_u32(nvs, "v_enter", &config->low_voltage_enter_mv);
    nvs_get_u32(nvs, "v_exit", &config->low_voltage_exit_mv);
    uint16_t port = 0;
    if (nvs_get_u16(nvs, "tcp_port", &port) == ESP_OK && port != 0) {
        config->tcp_port = port;
    }
    if (nvs_get_u16(nvs, "udp_hello", &port) == ESP_OK && port != 0) {
        config->udp_hello_port = port;
    }
    if (nvs_get_u16(nvs, "udp_tel", &port) == ESP_OK && port != 0) {
        config->udp_telemetry_port = port;
    }
    uint8_t value = 0;
    if (nvs_get_u8(nvs, "tel_en", &value) == ESP_OK) {
        config->telemetry_enabled = value != 0;
    }
    if (nvs_get_u8(nvs, "alarm_en", &value) == ESP_OK) {
        config->alarm_enabled = value != 0;
    }
    if (nvs_get_u8(nvs, "cap_enter", &value) == ESP_OK) {
        config->low_capacity_enter_pct = value;
    }
    if (nvs_get_u8(nvs, "cap_exit", &value) == ESP_OK) {
        config->low_capacity_exit_pct = value;
    }
    size_t len = sizeof(config->adc_calibration_factor);
    nvs_get_blob(nvs, "adc_factor", &config->adc_calibration_factor, &len);
    nvs_close(nvs);
    return ESP_OK;
}

esp_err_t storage_save_config(const mamba_config_t *config)
{
    nvs_handle_t nvs;
    ESP_RETURN_ON_ERROR(nvs_open(NVS_NS, NVS_READWRITE, &nvs), TAG, "open nvs");
    nvs_set_str(nvs, "device", config->device_name);
    nvs_set_str(nvs, "alarm_file", config->alarm_file);
    nvs_set_str(nvs, "power_file", config->power_on_file);
    nvs_set_str(nvs, "can_filter", config->can_filter);
    nvs_set_str(nvs, "wifi_ssid", config->wifi_ssid);
    nvs_set_str(nvs, "wifi_pass", config->wifi_password);
    nvs_set_u32(nvs, "can_bitrate", config->can_bitrate);
    nvs_set_u32(nvs, "tel_ms", config->telemetry_interval_ms);
    nvs_set_u32(nvs, "v_enter", config->low_voltage_enter_mv);
    nvs_set_u32(nvs, "v_exit", config->low_voltage_exit_mv);
    nvs_set_u16(nvs, "tcp_port", config->tcp_port);
    nvs_set_u16(nvs, "udp_hello", config->udp_hello_port);
    nvs_set_u16(nvs, "udp_tel", config->udp_telemetry_port);
    nvs_set_u8(nvs, "tel_en", config->telemetry_enabled ? 1 : 0);
    nvs_set_u8(nvs, "alarm_en", config->alarm_enabled ? 1 : 0);
    nvs_set_u8(nvs, "cap_enter", config->low_capacity_enter_pct);
    nvs_set_u8(nvs, "cap_exit", config->low_capacity_exit_pct);
    nvs_set_blob(nvs, "adc_factor", &config->adc_calibration_factor, sizeof(config->adc_calibration_factor));
    esp_err_t err = nvs_commit(nvs);
    nvs_close(nvs);
    return err;
}

esp_err_t storage_get_info(storage_info_t *info)
{
    if (!info) {
        return ESP_ERR_INVALID_ARG;
    }
    *info = s_cached_info;
    return s_cached_info.total_bytes > 0 ? ESP_OK : ESP_ERR_INVALID_STATE;
}

esp_err_t storage_list_audio_json(char *out, size_t out_len)
{
    size_t used = 0;
    int written = snprintf(out, out_len, "{\"files\":[");
    if (written < 0 || (size_t)written >= out_len) {
        return ESP_ERR_NO_MEM;
    }
    used = (size_t)written;

    DIR *dir = opendir(MAMBA_SPIFFS_BASE_PATH);
    if (dir) {
        struct dirent *entry;
        bool first = true;
        while ((entry = readdir(dir)) != NULL) {
            const char *name = entry->d_name;
            size_t name_len = strlen(name);
            if (name_len < 5 || strcasecmp(name + name_len - 4, ".wav") != 0) {
                continue;
            }
            char path[320];
            snprintf(path, sizeof(path), "%s/%s", MAMBA_SPIFFS_BASE_PATH, name);
            struct stat st;
            if (stat(path, &st) != 0) {
                continue;
            }
            written = snprintf(out + used, out_len - used, "%s{\"name\":\"%s\",\"size\":%ld}",
                               first ? "" : ",", name, (long)st.st_size);
            if (written < 0 || (size_t)written >= out_len - used) {
                closedir(dir);
                return ESP_ERR_NO_MEM;
            }
            used += (size_t)written;
            first = false;
        }
        closedir(dir);
    }
    written = snprintf(out + used, out_len - used, "]}");
    return (written < 0 || (size_t)written >= out_len - used) ? ESP_ERR_NO_MEM : ESP_OK;
}

esp_err_t storage_init(mamba_config_t *config)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_RETURN_ON_ERROR(err, TAG, "nvs init");

    esp_vfs_spiffs_conf_t spiffs = {
        .base_path = MAMBA_SPIFFS_BASE_PATH,
        .partition_label = "storage",
        .max_files = 8,
        .format_if_mount_failed = true,
    };
    ESP_RETURN_ON_ERROR(esp_vfs_spiffs_register(&spiffs), TAG, "spiffs mount");
    if (esp_spiffs_info("storage", &s_cached_info.total_bytes, &s_cached_info.used_bytes) == ESP_OK) {
        ESP_LOGI(TAG, "SPIFFS total=%u used=%u", (unsigned)s_cached_info.total_bytes,
                 (unsigned)s_cached_info.used_bytes);
    }
    return storage_load_config(config);
}
