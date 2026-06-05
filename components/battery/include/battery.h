#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "mamba_config.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool i2c_online;
    bool who_am_i_ok;
    uint8_t capacity_percent;
    int32_t i2c_voltage_mv;
    int32_t current_ma;
    int16_t temperature_decic;
    uint8_t internal_state;
    uint8_t error_state;
    uint32_t designed_capacity_mah;
    uint16_t loop_times;
    uint16_t production_date;
    uint8_t battery_life_percent;
    int adc_raw;
    int adc_pin_mv;
    uint32_t adc_battery_mv;
    uint32_t fused_voltage_mv;
    uint32_t sample_count;
} battery_snapshot_t;

esp_err_t battery_init(const mamba_config_t *config);
void battery_get_snapshot(battery_snapshot_t *out);

#ifdef __cplusplus
}
#endif
