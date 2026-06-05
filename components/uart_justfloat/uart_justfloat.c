#include "uart_justfloat.h"

#include <string.h>

#include "driver/uart.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "link.h"
#include "mamba_config.h"

#define JUSTFLOAT_MAX_FRAME 128
#define JUSTFLOAT_MAX_VALUES 16

static const char *TAG = "uart_justfloat";
static uint32_t s_dropped;

static bool is_tail(const uint8_t *buf, size_t len)
{
    return len >= 4 && buf[len - 4] == 0x00 && buf[len - 3] == 0x00 &&
           buf[len - 2] == 0x80 && buf[len - 1] == 0x7f;
}

static void process_frame(const uint8_t *buf, size_t len)
{
    if (len <= 4) {
        s_dropped++;
        return;
    }
    size_t data_len = len - 4;
    if ((data_len % sizeof(float)) != 0) {
        s_dropped++;
        return;
    }
    uint8_t count = data_len / sizeof(float);
    if (count == 0 || count > JUSTFLOAT_MAX_VALUES) {
        s_dropped++;
        return;
    }
    float values[JUSTFLOAT_MAX_VALUES];
    memcpy(values, buf, data_len);
    link_publish_justfloat(values, count, s_dropped);
}

static void uart_task(void *arg)
{
    uint8_t rx[64];
    uint8_t frame[JUSTFLOAT_MAX_FRAME];
    size_t frame_len = 0;
    while (true) {
        int n = uart_read_bytes(MAMBA_UART_JUSTFLOAT_PORT, rx, sizeof(rx), pdMS_TO_TICKS(100));
        for (int i = 0; i < n; ++i) {
            if (frame_len >= sizeof(frame)) {
                frame_len = 0;
                s_dropped++;
            }
            frame[frame_len++] = rx[i];
            if (is_tail(frame, frame_len)) {
                process_frame(frame, frame_len);
                frame_len = 0;
            }
        }
    }
}

esp_err_t uart_justfloat_init(void)
{
    uart_config_t cfg = {
        .baud_rate = MAMBA_UART_JUSTFLOAT_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_RETURN_ON_ERROR(uart_driver_install(MAMBA_UART_JUSTFLOAT_PORT, 2048, 0, 0, NULL, 0), TAG, "driver");
    ESP_RETURN_ON_ERROR(uart_param_config(MAMBA_UART_JUSTFLOAT_PORT, &cfg), TAG, "config");
    ESP_RETURN_ON_ERROR(uart_set_pin(MAMBA_UART_JUSTFLOAT_PORT,
                                     MAMBA_UART_JUSTFLOAT_TX_GPIO,
                                     MAMBA_UART_JUSTFLOAT_RX_GPIO,
                                     UART_PIN_NO_CHANGE,
                                     UART_PIN_NO_CHANGE), TAG, "pins");
    BaseType_t ok = xTaskCreate(uart_task, "uart_justfloat", 3072, NULL, 5, NULL);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "task");
    ESP_LOGI(TAG, "UART%d JustFloat RX ready at %d baud", MAMBA_UART_JUSTFLOAT_PORT, MAMBA_UART_JUSTFLOAT_BAUD);
    return ESP_OK;
}
