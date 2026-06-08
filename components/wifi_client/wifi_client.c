#include "wifi_client.h"

#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "link.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_NEEDS_RECONNECT_BIT BIT1
#define TCP_RETRY_DELAY_MS 1000
#define WIFI_HOSTNAME_MAX_LEN 32

static const char *TAG = "wifi_client";
static EventGroupHandle_t s_events;
static SemaphoreHandle_t s_lock;
static esp_netif_t *s_sta_netif;
static mamba_config_t s_config;
static bool s_started;

static bool hostname_char_ok(char c)
{
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
           (c >= '0' && c <= '9') || c == '-';
}

static void make_hostname(char *out, size_t out_len)
{
    const char *name = s_config.device_name[0] ? s_config.device_name : MAMBA_DEFAULT_DEVICE_NAME;
    uint8_t mac[6] = {0};
    (void)esp_read_mac(mac, ESP_MAC_WIFI_STA);
    char suffix[8];
    snprintf(suffix, sizeof(suffix), "-%02X%02X", mac[4], mac[5]);

    size_t suffix_len = strlen(suffix);
    size_t max_base = out_len > suffix_len + 1 ? out_len - suffix_len - 1 : 0;
    size_t used = 0;
    for (size_t i = 0; name[i] && used < max_base; ++i) {
        char c = name[i];
        if (c == ' ' || c == '_' || c == '.') {
            c = '-';
        }
        if (!hostname_char_ok(c)) {
            continue;
        }
        if (c == '-' && (used == 0 || out[used - 1] == '-')) {
            continue;
        }
        out[used++] = c;
    }
    while (used > 0 && out[used - 1] == '-') {
        used--;
    }
    if (used == 0) {
        strlcpy(out, MAMBA_DEFAULT_DEVICE_NAME, out_len);
        used = strlen(out);
    } else {
        out[used] = '\0';
    }
    strlcat(out, suffix, out_len);
}

static void apply_hostname_locked(void)
{
    if (!s_sta_netif) {
        return;
    }
    char hostname[WIFI_HOSTNAME_MAX_LEN] = {0};
    make_hostname(hostname, sizeof(hostname));
    esp_err_t err = esp_netif_set_hostname(s_sta_netif, hostname);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "set hostname failed: %s", esp_err_to_name(err));
    } else {
        ESP_LOGI(TAG, "wifi hostname: %s", hostname);
    }
}

static void event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_events, WIFI_CONNECTED_BIT);
        xEventGroupSetBits(s_events, WIFI_NEEDS_RECONNECT_BIT);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupClearBits(s_events, WIFI_NEEDS_RECONNECT_BIT);
        xEventGroupSetBits(s_events, WIFI_CONNECTED_BIT);
    }
}

static void configure_sta_locked(void)
{
    wifi_config_t wifi = {0};
    apply_hostname_locked();
    strlcpy((char *)wifi.sta.ssid, s_config.wifi_ssid, sizeof(wifi.sta.ssid));
    strlcpy((char *)wifi.sta.password, s_config.wifi_password, sizeof(wifi.sta.password));
    wifi.sta.threshold.authmode = strlen(s_config.wifi_password) >= 8 ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wifi.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_disconnect());
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_set_config(WIFI_IF_STA, &wifi));
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_connect());
}

static void tcp_client_task(void *arg)
{
    while (true) {
        EventBits_t bits = xEventGroupWaitBits(s_events, WIFI_CONNECTED_BIT | WIFI_NEEDS_RECONNECT_BIT,
                                               pdFALSE, pdFALSE, pdMS_TO_TICKS(TCP_RETRY_DELAY_MS));
        if ((bits & WIFI_NEEDS_RECONNECT_BIT) && !(bits & WIFI_CONNECTED_BIT)) {
            xEventGroupClearBits(s_events, WIFI_NEEDS_RECONNECT_BIT);
            if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
                if (s_config.wifi_ssid[0]) {
                    esp_wifi_connect();
                }
                xSemaphoreGive(s_lock);
            }
            vTaskDelay(pdMS_TO_TICKS(TCP_RETRY_DELAY_MS));
            continue;
        }
        if ((xEventGroupGetBits(s_events) & WIFI_CONNECTED_BIT) == 0) {
            continue;
        }

        esp_netif_ip_info_t ip;
        if (!s_sta_netif || esp_netif_get_ip_info(s_sta_netif, &ip) != ESP_OK || ip.gw.addr == 0) {
            vTaskDelay(pdMS_TO_TICKS(TCP_RETRY_DELAY_MS));
            continue;
        }
        uint16_t tcp_port = s_config.tcp_port ? s_config.tcp_port : MAMBA_LINK_TCP_PORT;
        link_set_udp_target(ip.gw.addr,
                            s_config.udp_hello_port ? s_config.udp_hello_port : MAMBA_LINK_UDP_HELLO_PORT,
                            s_config.udp_telemetry_port ? s_config.udp_telemetry_port : MAMBA_LINK_UDP_TELEMETRY_PORT);

        int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
        if (sock < 0) {
            vTaskDelay(pdMS_TO_TICKS(TCP_RETRY_DELAY_MS));
            continue;
        }
        struct sockaddr_in dst = {
            .sin_family = AF_INET,
            .sin_port = htons(tcp_port),
            .sin_addr.s_addr = ip.gw.addr,
        };
        if (connect(sock, (struct sockaddr *)&dst, sizeof(dst)) == 0) {
            ESP_LOGI(TAG, "connected to host " IPSTR ":%u", IP2STR(&ip.gw), tcp_port);
            link_attach_tcp_socket(sock, ip.gw.addr);
            while ((xEventGroupGetBits(s_events) & WIFI_CONNECTED_BIT) != 0 &&
                   link_is_tcp_socket_attached(sock)) {
                vTaskDelay(pdMS_TO_TICKS(TCP_RETRY_DELAY_MS));
            }
            link_detach_tcp_socket(sock);
        } else {
            close(sock);
            ESP_LOGD(TAG, "TCP connect to host failed, retrying");
            vTaskDelay(pdMS_TO_TICKS(TCP_RETRY_DELAY_MS));
        }
    }
}

void wifi_client_apply_config(const mamba_config_t *config)
{
    if (!config || !s_lock) {
        return;
    }
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(200)) == pdTRUE) {
        s_config = *config;
        if (s_started && s_config.wifi_ssid[0]) {
            configure_sta_locked();
        }
        xSemaphoreGive(s_lock);
    }
}

esp_err_t wifi_client_init(const mamba_config_t *config)
{
    if (config) {
        s_config = *config;
    } else {
        mamba_config_defaults(&s_config);
    }
    s_lock = xSemaphoreCreateMutex();
    s_events = xEventGroupCreate();
    ESP_RETURN_ON_FALSE(s_lock && s_events, ESP_ERR_NO_MEM, TAG, "alloc");

    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif init");
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    s_sta_netif = esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&init), TAG, "wifi init");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, event_handler, NULL), TAG, "wifi event");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, event_handler, NULL), TAG, "ip event");
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "wifi mode");
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_set_max_tx_power(84));
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW20));
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N));
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "wifi start");
    s_started = true;
    if (s_config.wifi_ssid[0]) {
        configure_sta_locked();
    } else {
        ESP_LOGI(TAG, "wifi credentials empty; configure them over USB");
    }
    BaseType_t ok = xTaskCreate(tcp_client_task, "wifi_tcp", 4096, NULL, 4, NULL);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "tcp task");
    return ESP_OK;
}
