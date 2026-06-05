#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CAN_MONITOR_MAX_DATA 8
#define CAN_MONITOR_MOTOR_COUNT 8

typedef struct {
    uint32_t id;
    uint8_t dlc;
    uint8_t data[CAN_MONITOR_MAX_DATA];
    uint64_t timestamp_us;
} can_raw_frame_t;

typedef struct {
    bool valid;
    uint16_t angle;
    int16_t rpm;
    int16_t torque_current;
    uint8_t temperature;
    uint8_t error;
    int16_t commanded_current;
    uint32_t update_count;
} rm_motor_state_t;

typedef struct {
    bool started;
    uint32_t bitrate;
    uint32_t rx_count;
    uint32_t dropped_count;
    rm_motor_state_t motors[CAN_MONITOR_MOTOR_COUNT];
} can_monitor_snapshot_t;

esp_err_t can_monitor_init(uint32_t bitrate);
void can_monitor_get_snapshot(can_monitor_snapshot_t *out);
bool can_monitor_receive_frame(can_raw_frame_t *out, uint32_t timeout_ms);
bool can_monitor_parse_rm_feedback(uint32_t id, const uint8_t data[8], rm_motor_state_t *out);
bool can_monitor_self_test(void);

#ifdef __cplusplus
}
#endif
