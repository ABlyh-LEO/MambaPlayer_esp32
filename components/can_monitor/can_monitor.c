#include "can_monitor.h"

#include <string.h>

#include "esp_check.h"
#include "esp_log.h"
#include "esp_twai.h"
#include "esp_twai_onchip.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mamba_config.h"

#define CAN_QUEUE_DEPTH 64

typedef struct {
    twai_frame_t frame;
    uint8_t data[TWAI_FRAME_MAX_LEN];
} twai_rx_item_t;

static const char *TAG = "can";
static QueueHandle_t s_rx_queue;
static SemaphoreHandle_t s_lock;
static twai_node_handle_t s_node;
static can_monitor_snapshot_t s_snapshot;

static int16_t be_i16(const uint8_t *p)
{
    return (int16_t)((uint16_t)p[0] << 8 | p[1]);
}

bool can_monitor_parse_rm_feedback(uint32_t id, const uint8_t data[8], rm_motor_state_t *out)
{
    if (id < 0x201 || id > 0x208 || !data || !out) {
        return false;
    }
    out->valid = true;
    out->angle = (uint16_t)data[0] << 8 | data[1];
    out->rpm = be_i16(data + 2);
    out->torque_current = be_i16(data + 4);
    out->temperature = data[6];
    out->error = data[7];
    out->update_count++;
    return true;
}

bool can_monitor_self_test(void)
{
    rm_motor_state_t state = {0};
    const uint8_t data[8] = {0x12, 0x34, 0xFF, 0x9C, 0x00, 0x2A, 55, 1};
    return can_monitor_parse_rm_feedback(0x201, data, &state) &&
           state.angle == 0x1234 && state.rpm == -100 && state.torque_current == 42 &&
           state.temperature == 55 && state.error == 1;
}

static void parse_control_frame_locked(uint32_t id, const uint8_t data[8])
{
    if (id != 0x200 && id != 0x1FF) {
        return;
    }
    int base = id == 0x200 ? 0 : 4;
    for (int i = 0; i < 4; ++i) {
        s_snapshot.motors[base + i].commanded_current = be_i16(data + i * 2);
        s_snapshot.motors[base + i].valid = true;
    }
}

static bool IRAM_ATTR rx_done_cb(twai_node_handle_t handle, const twai_rx_done_event_data_t *edata, void *user_ctx)
{
    BaseType_t woken = pdFALSE;
    twai_rx_item_t item = {0};
    item.frame.buffer = item.data;
    item.frame.buffer_len = sizeof(item.data);
    if (twai_node_receive_from_isr(handle, &item.frame) != ESP_OK) {
        return false;
    }
    if (xQueueSendFromISR(s_rx_queue, &item, &woken) != pdTRUE) {
        twai_rx_item_t old;
        xQueueReceiveFromISR(s_rx_queue, &old, &woken);
        if (xQueueSendFromISR(s_rx_queue, &item, &woken) != pdTRUE) {
            s_snapshot.dropped_count++;
        } else {
            s_snapshot.dropped_count++;
        }
    }
    return woken == pdTRUE;
}

static bool IRAM_ATTR error_cb(twai_node_handle_t handle, const twai_error_event_data_t *edata, void *user_ctx)
{
    return false;
}

static void process_frame(const twai_rx_item_t *item)
{
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(20)) != pdTRUE) {
        return;
    }
    s_snapshot.rx_count++;
    uint32_t id = item->frame.header.id;
    if (!item->frame.header.ide && item->frame.header.dlc >= 8) {
        if (id >= 0x201 && id <= 0x208) {
            can_monitor_parse_rm_feedback(id, item->data, &s_snapshot.motors[id - 0x201]);
        } else {
            parse_control_frame_locked(id, item->data);
        }
    }
    xSemaphoreGive(s_lock);
}

static void can_task(void *arg)
{
    twai_rx_item_t item;
    while (true) {
        if (xQueueReceive(s_rx_queue, &item, portMAX_DELAY) == pdTRUE) {
            process_frame(&item);
            xQueueSend((QueueHandle_t)arg, &item, 0);
        }
    }
}

static QueueHandle_t s_telemetry_queue;

bool can_monitor_receive_frame(can_raw_frame_t *out, uint32_t timeout_ms)
{
    if (!s_telemetry_queue || !out) {
        return false;
    }
    twai_rx_item_t item;
    if (xQueueReceive(s_telemetry_queue, &item, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) {
        return false;
    }
    out->id = item.frame.header.id;
    out->dlc = item.frame.header.dlc > CAN_MONITOR_MAX_DATA ? CAN_MONITOR_MAX_DATA : item.frame.header.dlc;
    memcpy(out->data, item.data, out->dlc);
    out->timestamp_us = item.frame.header.timestamp;
    return true;
}

void can_monitor_get_snapshot(can_monitor_snapshot_t *out)
{
    memset(out, 0, sizeof(*out));
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(20)) == pdTRUE) {
        *out = s_snapshot;
        xSemaphoreGive(s_lock);
    }
}

esp_err_t can_monitor_init(uint32_t bitrate)
{
    if (bitrate != 250000 && bitrate != 500000 && bitrate != 1000000) {
        bitrate = 1000000;
    }
    s_rx_queue = xQueueCreate(CAN_QUEUE_DEPTH, sizeof(twai_rx_item_t));
    s_telemetry_queue = xQueueCreate(CAN_QUEUE_DEPTH, sizeof(twai_rx_item_t));
    s_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_rx_queue && s_telemetry_queue && s_lock, ESP_ERR_NO_MEM, TAG, "alloc");

    twai_onchip_node_config_t cfg = {
        .io_cfg = {
            .tx = GPIO_NUM_NC,
            .rx = MAMBA_TWAI_RX_GPIO,
            .quanta_clk_out = GPIO_NUM_NC,
            .bus_off_indicator = GPIO_NUM_NC,
        },
        .bit_timing = {
            .bitrate = bitrate,
        },
        .timestamp_resolution_hz = 1000000,
        .tx_queue_depth = 0,
        .flags = {
            .enable_listen_only = true,
            .no_receive_rtr = true,
        },
    };
    esp_err_t err = twai_new_node_onchip(&cfg, &s_node);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "TWAI node not started: %s", esp_err_to_name(err));
        return ESP_OK;
    }
    twai_mask_filter_config_t filter = {
        .id = 0,
        .mask = 0,
        .is_ext = false,
        .no_fd = true,
    };
    twai_node_config_mask_filter(s_node, 0, &filter);
    twai_event_callbacks_t cbs = {
        .on_rx_done = rx_done_cb,
        .on_error = error_cb,
    };
    ESP_RETURN_ON_ERROR(twai_node_register_event_callbacks(s_node, &cbs, NULL), TAG, "twai callbacks");
    ESP_RETURN_ON_ERROR(twai_node_enable(s_node), TAG, "twai enable");
    s_snapshot.started = true;
    s_snapshot.bitrate = bitrate;
    BaseType_t ok = xTaskCreate(can_task, "can_rx_task", 4096, s_telemetry_queue, 6, NULL);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "can task");
    ESP_LOGI(TAG, "TWAI listen-only started at %lu bps, parser self-test=%s",
             (unsigned long)bitrate, can_monitor_self_test() ? "ok" : "fail");
    return ESP_OK;
}
