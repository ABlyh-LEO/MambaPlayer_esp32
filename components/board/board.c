#include "mamba_config.h"

#include <stdio.h>
#include <string.h>

#include "esp_mac.h"

void mamba_make_default_ssid(char *ssid, size_t ssid_len)
{
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
    snprintf(ssid, ssid_len, "Mamba-C3-%02X%02X", mac[4], mac[5]);
}

void mamba_config_defaults(mamba_config_t *config)
{
    memset(config, 0, sizeof(*config));
    strlcpy(config->device_name, "Mamba C3", sizeof(config->device_name));
    mamba_make_default_ssid(config->ap_ssid, sizeof(config->ap_ssid));
    config->ap_password[0] = '\0';
    config->can_bitrate = 1000000;
    config->telemetry_enabled = true;
    config->telemetry_interval_ms = 100;
    config->adc_calibration_factor = 1.0f;
    config->low_voltage_enter_mv = 21000;
    config->low_voltage_exit_mv = 22000;
    config->low_capacity_enter_pct = 30;
    config->low_capacity_exit_pct = 35;
    config->alarm_enabled = true;
    strlcpy(config->alarm_file, MAMBA_DEFAULT_ALARM_FILE, sizeof(config->alarm_file));
    strlcpy(config->power_on_file, MAMBA_DEFAULT_POWER_ON_FILE, sizeof(config->power_on_file));
    config->wifi_ssid[0] = '\0';
    config->wifi_password[0] = '\0';
    config->tcp_port = MAMBA_LINK_TCP_PORT;
    config->udp_hello_port = MAMBA_LINK_UDP_HELLO_PORT;
    config->udp_telemetry_port = MAMBA_LINK_UDP_TELEMETRY_PORT;
}
