#include "alarm.h"

#include <string.h>

#include "audio.h"
#include "battery.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "alarm";
static SemaphoreHandle_t s_lock;
static alarm_status_t s_status;
static mamba_config_t s_config;

static bool should_enter(const battery_snapshot_t *bat, const mamba_config_t *cfg)
{
    if (!cfg->alarm_enabled) {
        return false;
    }
    if (bat->who_am_i_ok && bat->capacity_percent > 0) {
        return bat->capacity_percent < cfg->low_capacity_enter_pct;
    }
    return bat->fused_voltage_mv > 0 && bat->fused_voltage_mv < cfg->low_voltage_enter_mv;
}

static bool should_exit(const battery_snapshot_t *bat, const mamba_config_t *cfg)
{
    bool voltage_ok = bat->fused_voltage_mv == 0 || bat->fused_voltage_mv > cfg->low_voltage_exit_mv;
    bool cap_ok = !bat->who_am_i_ok || bat->capacity_percent > cfg->low_capacity_exit_pct;
    return !cfg->alarm_enabled || (voltage_ok && cap_ok);
}

bool alarm_self_test(void)
{
    mamba_config_t cfg;
    mamba_config_defaults(&cfg);
    battery_snapshot_t bat = {
        .who_am_i_ok = false,
        .fused_voltage_mv = 20900,
    };
    if (!should_enter(&bat, &cfg)) {
        return false;
    }
    bat.fused_voltage_mv = 22100;
    return should_exit(&bat, &cfg);
}

static void publish(const alarm_status_t *status)
{
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(20)) == pdTRUE) {
        s_status = *status;
        xSemaphoreGive(s_lock);
    }
}

void alarm_get_status(alarm_status_t *out)
{
    memset(out, 0, sizeof(*out));
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(20)) == pdTRUE) {
        *out = s_status;
        xSemaphoreGive(s_lock);
    }
}

static void alarm_task(void *arg)
{
    alarm_status_t st = {0};
    while (true) {
        battery_snapshot_t bat;
        battery_get_snapshot(&bat);
        uint32_t now = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);

        if (!st.active) {
            if (should_enter(&bat, &s_config)) {
                if (st.enter_candidate_ms == 0) {
                    st.enter_candidate_ms = now;
                } else if (now - st.enter_candidate_ms >= 2500) {
                    st.active = true;
                    st.exit_candidate_ms = 0;
                    st.transitions++;
                    audio_play_alarm(s_config.alarm_file, audio_get_alarm_offset());
                    ESP_LOGW(TAG, "low battery alarm entered");
                }
            } else {
                st.enter_candidate_ms = 0;
            }
        } else {
            if (should_exit(&bat, &s_config)) {
                if (st.exit_candidate_ms == 0) {
                    st.exit_candidate_ms = now;
                } else if (now - st.exit_candidate_ms >= 2500) {
                    st.active = false;
                    st.enter_candidate_ms = 0;
                    st.transitions++;
                    audio_stop_and_save_offset();
                    ESP_LOGI(TAG, "low battery alarm exited");
                }
            } else {
                st.exit_candidate_ms = 0;
            }
        }
        publish(&st);
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

esp_err_t alarm_init(const mamba_config_t *config)
{
    s_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_lock, ESP_ERR_NO_MEM, TAG, "alloc");
    if (config) {
        s_config = *config;
    } else {
        mamba_config_defaults(&s_config);
    }
    BaseType_t ok = xTaskCreate(alarm_task, "alarm_task", 3072, NULL, 4, NULL);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "alarm task");
    ESP_LOGI(TAG, "alarm ready, self-test=%s", alarm_self_test() ? "ok" : "fail");
    return ESP_OK;
}
