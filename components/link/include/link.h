#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "can_monitor.h"
#include "esp_err.h"
#include "mamba_config.h"

#ifdef __cplusplus
extern "C" {
#endif

#define MAMBA_LINK_SYNC0 'M'
#define MAMBA_LINK_SYNC1 'L'
#define MAMBA_LINK_MAX_PAYLOAD 1024

typedef enum {
    MAMBA_LINK_TYPE_HELLO = 1,
    MAMBA_LINK_TYPE_GET_STATUS = 2,
    MAMBA_LINK_TYPE_STATUS = 3,
    MAMBA_LINK_TYPE_SET_CONFIG = 4,
    MAMBA_LINK_TYPE_AUDIO_BEGIN = 5,
    MAMBA_LINK_TYPE_AUDIO_CHUNK = 6,
    MAMBA_LINK_TYPE_AUDIO_END = 7,
    MAMBA_LINK_TYPE_ACK = 8,
    MAMBA_LINK_TYPE_AUDIO_TEST = 9,
    MAMBA_LINK_TYPE_ERROR = 10,
    MAMBA_LINK_TYPE_WIFI_CONFIG = 11,
    MAMBA_LINK_TYPE_TELEMETRY = 64,
} mamba_link_type_t;

typedef enum {
    MAMBA_STREAM_BATTERY = 1,
    MAMBA_STREAM_CAN_RAW = 2,
    MAMBA_STREAM_RM_MOTOR = 3,
    MAMBA_STREAM_JUSTFLOAT = 4,
    MAMBA_STREAM_ALARM = 5,
} mamba_stream_id_t;

typedef void (*mamba_link_config_updated_cb_t)(const mamba_config_t *config);

esp_err_t link_init(const mamba_config_t *config);
void link_set_config_updated_callback(mamba_link_config_updated_cb_t cb);
void link_attach_tcp_socket(int sock, uint32_t host_ip_addr);
void link_detach_tcp_socket(int sock);
void link_set_udp_target(uint32_t host_ip_addr, uint16_t hello_port, uint16_t telemetry_port);
void link_publish_can_frame(const can_raw_frame_t *frame);
void link_publish_justfloat(const float *values, uint8_t count, uint32_t dropped_count);
bool link_self_test(void);

#ifdef __cplusplus
}
#endif
