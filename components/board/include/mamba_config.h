#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "hal/adc_types.h"
#include "hal/gpio_types.h"

#ifdef __cplusplus
extern "C" {
#endif

#define MAMBA_I2C_SDA_GPIO GPIO_NUM_7
#define MAMBA_I2C_SCL_GPIO GPIO_NUM_10
#define MAMBA_I2C_FREQ_HZ 100000
#define MAMBA_BATTERY_I2C_ADDR 0x41
#define MAMBA_BATTERY_REG_WHO_AM_I 0x1F

#define MAMBA_ADC_UNIT ADC_UNIT_1
#define MAMBA_ADC_CHANNEL ADC_CHANNEL_3
#define MAMBA_ADC_ATTEN ADC_ATTEN_DB_12
#define MAMBA_ADC_DIVIDER_RATIO 11.0f

#define MAMBA_I2S_BCLK_GPIO GPIO_NUM_4
#define MAMBA_I2S_WS_GPIO GPIO_NUM_5
#define MAMBA_I2S_DOUT_GPIO GPIO_NUM_6

#define MAMBA_TWAI_TX_GPIO GPIO_NUM_0
#define MAMBA_TWAI_RX_GPIO GPIO_NUM_1

#define MAMBA_UART_JUSTFLOAT_PORT 0
#define MAMBA_UART_JUSTFLOAT_TX_GPIO GPIO_NUM_21
#define MAMBA_UART_JUSTFLOAT_RX_GPIO GPIO_NUM_20
#define MAMBA_UART_JUSTFLOAT_BAUD 1000000

#define MAMBA_FIRMWARE_VERSION "0.3.0"
#define MAMBA_PROTOCOL_VERSION 2
#define MAMBA_LINK_TCP_PORT 37210
#define MAMBA_LINK_UDP_HELLO_PORT 37211
#define MAMBA_LINK_UDP_TELEMETRY_PORT 37212
#define MAMBA_LINK_UDP_SPEAKER_PORT 37213

#define MAMBA_DEFAULT_DEVICE_NAME "MambaPlayer-C3"
#define MAMBA_SPIFFS_BASE_PATH "/spiffs"
#define MAMBA_STORAGE_PARTITION_BYTES 0x250000
#define MAMBA_ALARM_DIR "/spiffs"
#define MAMBA_DEFAULT_ALARM_FILE "/spiffs/alarm.wav"
#define MAMBA_DEFAULT_POWER_ON_FILE "/spiffs/poweron.wav"
#define MAMBA_TELEMETRY_BATCH_INTERVAL_MS 1
#define MAMBA_AUDIO_SAMPLE_RATE_HZ 16000
#define MAMBA_AUDIO_CHANNELS 1
#define MAMBA_AUDIO_PCM_BITS_PER_SAMPLE 16
#define MAMBA_AUDIO_ADPCM_BITS_PER_SAMPLE 4
#define MAMBA_AUDIO_ADPCM_BLOCK_ALIGN 256
#define MAMBA_AUDIO_ADPCM_SAMPLES_PER_BLOCK ((MAMBA_AUDIO_ADPCM_BLOCK_ALIGN - 4) * 2 + 1)
#define MAMBA_AUDIO_ADPCM_BYTES_PER_SECOND ((MAMBA_AUDIO_SAMPLE_RATE_HZ * MAMBA_AUDIO_ADPCM_BLOCK_ALIGN + MAMBA_AUDIO_ADPCM_SAMPLES_PER_BLOCK - 1) / MAMBA_AUDIO_ADPCM_SAMPLES_PER_BLOCK)
#define MAMBA_POWER_ON_MAX_SECONDS 10
#define MAMBA_POWER_ON_MAX_SAMPLES (MAMBA_AUDIO_SAMPLE_RATE_HZ * MAMBA_POWER_ON_MAX_SECONDS)
#define MAMBA_POWER_ON_MAX_DATA_BYTES (MAMBA_AUDIO_ADPCM_BYTES_PER_SECOND * MAMBA_POWER_ON_MAX_SECONDS)
#define MAMBA_WAV_HEADER_ALLOWANCE_BYTES 4096
#define MAMBA_POWER_ON_MAX_FILE_BYTES (MAMBA_POWER_ON_MAX_DATA_BYTES + MAMBA_WAV_HEADER_ALLOWANCE_BYTES)
#define MAMBA_AUDIO_STORAGE_SAFETY_BYTES (32 * 1024)
#define MAMBA_CATALOG_INTERVAL_MS 500

typedef struct {
    char device_name[32];
    uint32_t can_bitrate;
    bool telemetry_enabled;
    uint32_t telemetry_interval_ms;
    float adc_calibration_factor;
    uint32_t low_voltage_enter_mv;
    uint32_t low_voltage_exit_mv;
    uint8_t low_capacity_enter_pct;
    uint8_t low_capacity_exit_pct;
    bool alarm_enabled;
    char alarm_file[64];
    char power_on_file[64];
    char can_filter[96];
    char wifi_ssid[33];
    char wifi_password[65];
    uint16_t tcp_port;
    uint16_t udp_hello_port;
    uint16_t udp_telemetry_port;
} mamba_config_t;

void mamba_config_defaults(mamba_config_t *config);

#ifdef __cplusplus
}
#endif
