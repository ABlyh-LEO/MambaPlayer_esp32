#include "telemetry_mux.h"

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#define ADC_QUEUE_DEPTH 64
#define CAN_QUEUE_DEPTH 128
#define RM_QUEUE_DEPTH 64
#define JUSTFLOAT_QUEUE_DEPTH 64

static QueueHandle_t s_adc_queue;
static QueueHandle_t s_can_queue;
static QueueHandle_t s_rm_queue;
static QueueHandle_t s_justfloat_queue;
static SemaphoreHandle_t s_latest_lock;
static telemetry_adc_sample_t s_latest_adc;
static telemetry_justfloat_frame_t s_latest_justfloat;
static bool s_has_adc;
static bool s_has_justfloat;
static volatile uint32_t s_adc_dropped;
static volatile uint32_t s_can_dropped;
static volatile uint32_t s_rm_dropped;
static volatile uint32_t s_justfloat_dropped;

static bool send_drop_oldest(QueueHandle_t queue, const void *item, uint32_t *dropped)
{
    if (!queue || !item) {
        return false;
    }
    if (xQueueSend(queue, item, 0) == pdTRUE) {
        return true;
    }
    uint8_t scratch[sizeof(telemetry_justfloat_frame_t)];
    (void)xQueueReceive(queue, scratch, 0);
    (*dropped)++;
    return xQueueSend(queue, item, 0) == pdTRUE;
}

static bool receive_item(QueueHandle_t queue, void *item, uint32_t timeout_ms)
{
    return queue && item &&
           xQueueReceive(queue, item, pdMS_TO_TICKS(timeout_ms)) == pdTRUE;
}

esp_err_t telemetry_mux_init(void)
{
    s_adc_queue = xQueueCreate(ADC_QUEUE_DEPTH, sizeof(telemetry_adc_sample_t));
    s_can_queue = xQueueCreate(CAN_QUEUE_DEPTH, sizeof(telemetry_can_frame_t));
    s_rm_queue = xQueueCreate(RM_QUEUE_DEPTH, sizeof(telemetry_rm_motor_t));
    s_justfloat_queue = xQueueCreate(JUSTFLOAT_QUEUE_DEPTH, sizeof(telemetry_justfloat_frame_t));
    s_latest_lock = xSemaphoreCreateMutex();
    return (s_adc_queue && s_can_queue && s_rm_queue && s_justfloat_queue && s_latest_lock) ? ESP_OK : ESP_ERR_NO_MEM;
}

bool telemetry_mux_publish_adc(const telemetry_adc_sample_t *sample)
{
    if (sample && s_latest_lock && xSemaphoreTake(s_latest_lock, pdMS_TO_TICKS(2)) == pdTRUE) {
        s_latest_adc = *sample;
        s_has_adc = true;
        xSemaphoreGive(s_latest_lock);
    }
    return send_drop_oldest(s_adc_queue, sample, (uint32_t *)&s_adc_dropped);
}

bool telemetry_mux_receive_adc(telemetry_adc_sample_t *sample, uint32_t timeout_ms)
{
    return receive_item(s_adc_queue, sample, timeout_ms);
}

bool telemetry_mux_get_latest_adc(telemetry_adc_sample_t *sample)
{
    if (!sample || !s_latest_lock || xSemaphoreTake(s_latest_lock, pdMS_TO_TICKS(2)) != pdTRUE) {
        return false;
    }
    bool ok = s_has_adc;
    if (ok) {
        *sample = s_latest_adc;
    }
    xSemaphoreGive(s_latest_lock);
    return ok;
}

bool telemetry_mux_publish_can(const telemetry_can_frame_t *frame)
{
    return send_drop_oldest(s_can_queue, frame, (uint32_t *)&s_can_dropped);
}

bool telemetry_mux_receive_can(telemetry_can_frame_t *frame, uint32_t timeout_ms)
{
    return receive_item(s_can_queue, frame, timeout_ms);
}

bool telemetry_mux_publish_rm_motor(const telemetry_rm_motor_t *motor)
{
    return send_drop_oldest(s_rm_queue, motor, (uint32_t *)&s_rm_dropped);
}

bool telemetry_mux_receive_rm_motor(telemetry_rm_motor_t *motor, uint32_t timeout_ms)
{
    return receive_item(s_rm_queue, motor, timeout_ms);
}

bool telemetry_mux_publish_justfloat(const telemetry_justfloat_frame_t *frame)
{
    if (frame && s_latest_lock && xSemaphoreTake(s_latest_lock, pdMS_TO_TICKS(2)) == pdTRUE) {
        s_latest_justfloat = *frame;
        s_has_justfloat = true;
        xSemaphoreGive(s_latest_lock);
    }
    return send_drop_oldest(s_justfloat_queue, frame, (uint32_t *)&s_justfloat_dropped);
}

bool telemetry_mux_receive_justfloat(telemetry_justfloat_frame_t *frame, uint32_t timeout_ms)
{
    return receive_item(s_justfloat_queue, frame, timeout_ms);
}

bool telemetry_mux_get_latest_justfloat(telemetry_justfloat_frame_t *frame)
{
    if (!frame || !s_latest_lock || xSemaphoreTake(s_latest_lock, pdMS_TO_TICKS(2)) != pdTRUE) {
        return false;
    }
    bool ok = s_has_justfloat;
    if (ok) {
        *frame = s_latest_justfloat;
    }
    xSemaphoreGive(s_latest_lock);
    return ok;
}

uint32_t telemetry_mux_dropped_adc(void)
{
    return s_adc_dropped;
}

uint32_t telemetry_mux_dropped_can(void)
{
    return s_can_dropped;
}

uint32_t telemetry_mux_dropped_rm_motor(void)
{
    return s_rm_dropped;
}

uint32_t telemetry_mux_dropped_justfloat(void)
{
    return s_justfloat_dropped;
}

bool telemetry_mux_self_test(void)
{
    telemetry_adc_sample_t in = {.sample_index = 7, .battery_mv = 22000};
    telemetry_adc_sample_t out = {0};
    return telemetry_mux_publish_adc(&in) &&
           telemetry_mux_receive_adc(&out, 0) &&
           out.sample_index == 7 && out.battery_mv == 22000;
}
