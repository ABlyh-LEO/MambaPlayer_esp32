#include "alarm.h"
#include "audio.h"
#include "battery.h"
#include "can_monitor.h"
#include "esp_err.h"
#include "esp_log.h"
#include "link.h"
#include "mamba_config.h"
#include "storage.h"
#include "uart_justfloat.h"
#include "wifi_client.h"

void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_NONE);

    mamba_config_t config;
    ESP_ERROR_CHECK(storage_init(&config));

    audio_init();
    audio_play_once(config.power_on_file);
    battery_init(&config);
    can_monitor_init(config.can_bitrate);
    alarm_init(&config);
    link_init(&config);
    link_set_config_updated_callback(wifi_client_apply_config);
    wifi_client_init(&config);
    uart_justfloat_init();
}
