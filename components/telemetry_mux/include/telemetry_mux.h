#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define MAMBA_STREAM_BATTERY 1
#define MAMBA_STREAM_CAN_RAW 2
#define MAMBA_STREAM_RM_MOTOR 3
#define MAMBA_STREAM_JUSTFLOAT 4
#define MAMBA_STREAM_ALARM 5
#define MAMBA_STREAM_ADC_BATCH 6

#define MAMBA_TELEM_CAN_MAX_DATA 8
#define MAMBA_TELEM_JUSTFLOAT_MAX 16

typedef struct {
    uint32_t sample_index;
    uint32_t battery_mv;
    uint16_t pin_mv;
    uint16_t raw;
    uint64_t timestamp_us;
} telemetry_adc_sample_t;

typedef struct {
    uint32_t id;
    uint8_t dlc;
    uint8_t data[MAMBA_TELEM_CAN_MAX_DATA];
    uint64_t timestamp_us;
} telemetry_can_frame_t;

typedef struct {
    uint8_t motor_id;
    uint16_t angle;
    int16_t rpm;
    int16_t torque_current;
    int16_t commanded_current;
    uint8_t temperature;
    uint8_t error;
    uint64_t timestamp_us;
} telemetry_rm_motor_t;

typedef struct {
    uint8_t count;
    uint32_t dropped_count;
    float values[MAMBA_TELEM_JUSTFLOAT_MAX];
    uint64_t timestamp_us;
} telemetry_justfloat_frame_t;

esp_err_t telemetry_mux_init(void);

bool telemetry_mux_publish_adc(const telemetry_adc_sample_t *sample);
bool telemetry_mux_receive_adc(telemetry_adc_sample_t *sample, uint32_t timeout_ms);

bool telemetry_mux_publish_can(const telemetry_can_frame_t *frame);
bool telemetry_mux_receive_can(telemetry_can_frame_t *frame, uint32_t timeout_ms);

bool telemetry_mux_publish_rm_motor(const telemetry_rm_motor_t *motor);
bool telemetry_mux_receive_rm_motor(telemetry_rm_motor_t *motor, uint32_t timeout_ms);

bool telemetry_mux_publish_justfloat(const telemetry_justfloat_frame_t *frame);
bool telemetry_mux_receive_justfloat(telemetry_justfloat_frame_t *frame, uint32_t timeout_ms);

uint32_t telemetry_mux_dropped_adc(void);
uint32_t telemetry_mux_dropped_can(void);
uint32_t telemetry_mux_dropped_rm_motor(void);
uint32_t telemetry_mux_dropped_justfloat(void);

bool telemetry_mux_self_test(void);

#ifdef __cplusplus
}
#endif
