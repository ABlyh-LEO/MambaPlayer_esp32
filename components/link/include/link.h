#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "mamba_config.h"
#include "telemetry_mux.h"

#ifdef __cplusplus
extern "C" {
#endif

#define MAMBA_LINK_SYNC0 'M'
#define MAMBA_LINK_SYNC1 'L'
#define MAMBA_LINK_MAX_PAYLOAD 1536

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
    MAMBA_LINK_TYPE_AUDIO_STREAM_START = 12,
    MAMBA_LINK_TYPE_AUDIO_STREAM_PCM = 13,
    MAMBA_LINK_TYPE_AUDIO_STREAM_STOP = 14,
    MAMBA_LINK_TYPE_SET_STREAM_CHANNELS = 15,
    MAMBA_LINK_TYPE_SET_CAN_FORWARD_IDS = 16,
    MAMBA_LINK_TYPE_TELEMETRY = 64,
} mamba_link_type_t;

typedef void (*mamba_link_config_updated_cb_t)(const mamba_config_t *config);

esp_err_t link_init(const mamba_config_t *config);
void link_set_config_updated_callback(mamba_link_config_updated_cb_t cb);
void link_attach_tcp_socket(int sock, uint32_t host_ip_addr);
void link_detach_tcp_socket(int sock);
bool link_is_tcp_socket_attached(int sock);
void link_handle_tcp_rx_data(const uint8_t *data, size_t len);
void link_set_udp_target(uint32_t host_ip_addr, uint16_t hello_port, uint16_t telemetry_port);
bool link_self_test(void);

#ifdef __cplusplus
}
#endif
