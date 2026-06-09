#include "alarm.h"
#include "audio.h"
#include "battery.h"
#include "can_monitor.h"
#include "esp_err.h"
#include "esp_log.h"
#include "link.h"
#include "mamba_config.h"
#include "storage.h"
#include "telemetry_mux.h"
#include "uart_justfloat.h"
#include "wifi_client.h"

void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_NONE);

    mamba_config_t config;
    mamba_config_defaults(&config);
    (void)storage_init(&config);
    (void)telemetry_mux_init();

    esp_err_t audio_err = audio_init();
    if (audio_err == ESP_OK) {
        audio_play_once(config.power_on_file);
    }
    battery_init(&config);
    if (can_monitor_init(config.can_bitrate) == ESP_OK) {
        can_monitor_apply_config(&config);
    }
    alarm_init(&config);
    (void)link_init(&config);
    link_set_config_updated_callback(wifi_client_apply_config);
    wifi_client_init(&config);
    uart_justfloat_init();
}
