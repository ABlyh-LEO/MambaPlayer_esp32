#include "battery.h"

#include <string.h>

#include "driver/i2c_master.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

enum {
    REG_DESIGNED_CAPACITY = 0x20,
    REG_LOOP_TIMES = 0x24,
    REG_PRODUCTION_DATE = 0x26,
    REG_BATTERY_LIFE = 0x28,
    REG_CURRENT_VOLTAGE = 0x29,
    REG_CURRENT_CURRENT = 0x2D,
    REG_TEMPERATURE = 0x31,
    REG_CAPACITY_PERCENT = 0x33,
    REG_INTERNAL_STATE = 0x34,
    REG_ERROR_STATE = 0x35,
};

static const char *TAG = "battery";
static battery_snapshot_t s_snapshot;
static SemaphoreHandle_t s_lock;
static i2c_master_bus_handle_t s_i2c_bus;
static i2c_master_dev_handle_t s_i2c_dev;
static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;
static bool s_do_cali;
static float s_adc_factor = 1.0f;

static uint16_t rd_le16(const uint8_t *p)
{
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static uint32_t rd_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static esp_err_t i2c_read_reg(uint8_t reg, uint8_t *data, size_t len)
{
    if (!s_i2c_dev) {
        return ESP_ERR_INVALID_STATE;
    }
    return i2c_master_transmit_receive(s_i2c_dev, &reg, 1, data, len, 50);
}

static void publish_snapshot(const battery_snapshot_t *snap)
{
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(20)) == pdTRUE) {
        s_snapshot = *snap;
        xSemaphoreGive(s_lock);
    }
}

void battery_get_snapshot(battery_snapshot_t *out)
{
    memset(out, 0, sizeof(*out));
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(20)) == pdTRUE) {
        *out = s_snapshot;
        xSemaphoreGive(s_lock);
    }
}

static void update_adc(battery_snapshot_t *snap)
{
    int raw = 0;
    int mv = 0;
    if (s_adc && adc_oneshot_read(s_adc, MAMBA_ADC_CHANNEL, &raw) == ESP_OK) {
        snap->adc_raw = raw;
        if (s_do_cali && adc_cali_raw_to_voltage(s_cali, raw, &mv) == ESP_OK) {
            snap->adc_pin_mv = mv;
        } else {
            snap->adc_pin_mv = raw * 3300 / 4095;
        }
        snap->adc_battery_mv = (uint32_t)((float)snap->adc_pin_mv * MAMBA_ADC_DIVIDER_RATIO * s_adc_factor);
    }
}

static void update_i2c_static(battery_snapshot_t *snap)
{
    uint8_t buf[4] = {0};
    if (i2c_read_reg(REG_DESIGNED_CAPACITY, buf, 4) == ESP_OK) {
        snap->designed_capacity_mah = rd_le32(buf);
    }
    if (i2c_read_reg(REG_LOOP_TIMES, buf, 2) == ESP_OK) {
        snap->loop_times = rd_le16(buf);
    }
    if (i2c_read_reg(REG_PRODUCTION_DATE, buf, 2) == ESP_OK) {
        snap->production_date = rd_le16(buf);
    }
    if (i2c_read_reg(REG_BATTERY_LIFE, buf, 1) == ESP_OK) {
        snap->battery_life_percent = buf[0];
    }
}

static void update_i2c_dynamic(battery_snapshot_t *snap)
{
    uint8_t buf[4] = {0};
    uint8_t who = 0;
    snap->i2c_online = (i2c_read_reg(MAMBA_BATTERY_REG_WHO_AM_I, &who, 1) == ESP_OK);
    snap->who_am_i_ok = snap->i2c_online && who == MAMBA_BATTERY_I2C_ADDR;
    if (!snap->who_am_i_ok) {
        return;
    }
    if (i2c_read_reg(REG_CURRENT_VOLTAGE, buf, 4) == ESP_OK) {
        snap->i2c_voltage_mv = (int32_t)rd_le32(buf);
    }
    if (i2c_read_reg(REG_CURRENT_CURRENT, buf, 4) == ESP_OK) {
        snap->current_ma = (int32_t)rd_le32(buf);
    }
    if (i2c_read_reg(REG_TEMPERATURE, buf, 2) == ESP_OK) {
        snap->temperature_decic = (int16_t)rd_le16(buf);
    }
    if (i2c_read_reg(REG_CAPACITY_PERCENT, buf, 1) == ESP_OK) {
        snap->capacity_percent = buf[0];
    }
    if (i2c_read_reg(REG_INTERNAL_STATE, buf, 1) == ESP_OK) {
        snap->internal_state = buf[0];
    }
    if (i2c_read_reg(REG_ERROR_STATE, buf, 1) == ESP_OK) {
        snap->error_state = buf[0];
    }
}

static void battery_task(void *arg)
{
    battery_snapshot_t snap = {0};
    int64_t last_static_us = 0;
    while (true) {
        update_adc(&snap);
        update_i2c_dynamic(&snap);
        int64_t now = esp_timer_get_time();
        if (snap.who_am_i_ok && now - last_static_us > 3000000) {
            update_i2c_static(&snap);
            last_static_us = now;
        }
        snap.fused_voltage_mv = snap.i2c_voltage_mv > 0 ? (uint32_t)snap.i2c_voltage_mv : snap.adc_battery_mv;
        snap.sample_count++;
        publish_snapshot(&snap);
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

static void init_adc(void)
{
    adc_oneshot_unit_init_cfg_t unit_cfg = {
        .unit_id = MAMBA_ADC_UNIT,
    };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&unit_cfg, &s_adc));
    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = MAMBA_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc, MAMBA_ADC_CHANNEL, &chan_cfg));

#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = MAMBA_ADC_UNIT,
        .chan = MAMBA_ADC_CHANNEL,
        .atten = MAMBA_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    s_do_cali = adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_cali) == ESP_OK;
#endif
}

static void init_i2c(void)
{
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = MAMBA_I2C_SDA_GPIO,
        .scl_io_num = MAMBA_I2C_SCL_GPIO,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &s_i2c_bus));
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = MAMBA_BATTERY_I2C_ADDR,
        .scl_speed_hz = MAMBA_I2C_FREQ_HZ,
    };
    ESP_ERROR_CHECK(i2c_master_bus_add_device(s_i2c_bus, &dev_cfg, &s_i2c_dev));
}

esp_err_t battery_init(const mamba_config_t *config)
{
    s_adc_factor = config ? config->adc_calibration_factor : 1.0f;
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) {
        return ESP_ERR_NO_MEM;
    }
    init_i2c();
    init_adc();
    BaseType_t ok = xTaskCreate(battery_task, "battery_task", 4096, NULL, 5, NULL);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "create battery task");
    ESP_LOGI(TAG, "battery monitor started");
    return ESP_OK;
}
