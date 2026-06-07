#include "link.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "alarm.h"
#include "audio.h"
#include "battery.h"
#include "driver/usb_serial_jtag.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "storage.h"

#define LINK_QUEUE_DEPTH 8
#define LINK_HEADER_LEN 12
#define LINK_UDP_MAX 1200

typedef enum {
    LINK_TX_USB,
    LINK_TX_TCP,
    LINK_TX_UDP,
} link_tx_target_t;

typedef struct {
    uint8_t type;
    uint16_t seq;
    size_t len;
    uint8_t payload[MAMBA_LINK_MAX_PAYLOAD];
} link_rx_frame_t;

typedef struct {
    uint8_t buffer[LINK_HEADER_LEN + MAMBA_LINK_MAX_PAYLOAD];
    size_t len;
} link_parser_t;

typedef struct {
    FILE *file;
    char tmp_path[64];
    char final_path[64];
    bool alarm;
    uint32_t received;
    uint32_t expected;
    uint32_t max_file;
} audio_upload_t;

static const char *TAG = "link";
static QueueHandle_t s_rx_queue;
static SemaphoreHandle_t s_lock;
static SemaphoreHandle_t s_tx_lock;
static mamba_config_t s_config;
static mamba_link_config_updated_cb_t s_config_cb;
static int s_tcp_sock = -1;
static int s_udp_sock = -1;
static uint32_t s_udp_host;
static uint16_t s_udp_hello_port = MAMBA_LINK_UDP_HELLO_PORT;
static uint16_t s_udp_tel_port = MAMBA_LINK_UDP_TELEMETRY_PORT;
static uint16_t s_seq;
static uint32_t s_udp_seq;
static audio_upload_t s_upload;
static uint8_t s_tx_buf[LINK_HEADER_LEN + MAMBA_LINK_MAX_PAYLOAD];
static volatile uint32_t s_usb_rx_bytes;
static volatile uint32_t s_usb_rx_frames;
static volatile uint32_t s_usb_rx_loops;
static volatile uint32_t s_usb_rx_empty;

static int ensure_udp_socket(void)
{
    if (s_udp_sock >= 0) {
        return s_udp_sock;
    }
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock >= 0) {
        s_udp_sock = sock;
    }
    return s_udp_sock;
}

static uint16_t le16(const uint8_t *p)
{
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static void put16(uint8_t *p, uint16_t v)
{
    p[0] = v & 0xff;
    p[1] = v >> 8;
}

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = v & 0xff;
    p[1] = (v >> 8) & 0xff;
    p[2] = (v >> 16) & 0xff;
    p[3] = (v >> 24) & 0xff;
}

static uint16_t crc16_ccitt(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xffff;
    for (size_t i = 0; i < len; ++i) {
        crc ^= (uint16_t)data[i] << 8;
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

static bool json_get_string(const char *json, const char *key, char *out, size_t out_len)
{
    const char *p = strstr(json, key);
    if (!p || !out || out_len == 0) {
        return false;
    }
    p = strchr(p, ':');
    if (!p) {
        return false;
    }
    p++;
    while (*p == ' ' || *p == '"') {
        if (*p++ == '"') {
            break;
        }
    }
    size_t n = 0;
    while (*p && *p != '"' && *p != ',' && *p != '}' && n + 1 < out_len) {
        out[n++] = *p++;
    }
    out[n] = 0;
    return n > 0;
}

static bool bytes_contains(const uint8_t *haystack, size_t haystack_len, const char *needle)
{
    size_t needle_len = strlen(needle);
    if (needle_len == 0 || haystack_len < needle_len) {
        return false;
    }
    for (size_t i = 0; i + needle_len <= haystack_len; ++i) {
        if (memcmp(haystack + i, needle, needle_len) == 0) {
            return true;
        }
    }
    return false;
}

static bool json_get_u32(const char *json, const char *key, uint32_t *out)
{
    const char *p = strstr(json, key);
    if (!p || !out) {
        return false;
    }
    p = strchr(p, ':');
    if (!p) {
        return false;
    }
    *out = (uint32_t)strtoul(p + 1, NULL, 10);
    return true;
}

static bool json_get_float(const char *json, const char *key, float *out)
{
    const char *p = strstr(json, key);
    if (!p || !out) {
        return false;
    }
    p = strchr(p, ':');
    if (!p) {
        return false;
    }
    *out = strtof(p + 1, NULL);
    return true;
}

static size_t alarm_max_file_bytes(void)
{
    storage_info_t info = {0};
    if (storage_get_info(&info) != ESP_OK || info.total_bytes == 0) {
        return 0x1D0000 - MAMBA_POWER_ON_MAX_FILE_BYTES - MAMBA_AUDIO_STORAGE_SAFETY_BYTES;
    }
    if (info.total_bytes <= MAMBA_POWER_ON_MAX_FILE_BYTES + MAMBA_AUDIO_STORAGE_SAFETY_BYTES) {
        return 0;
    }
    return info.total_bytes - MAMBA_POWER_ON_MAX_FILE_BYTES - MAMBA_AUDIO_STORAGE_SAFETY_BYTES;
}

static void send_bytes_usb(const uint8_t *data, size_t len)
{
    if (usb_serial_jtag_is_driver_installed() && usb_serial_jtag_is_connected()) {
        size_t sent = 0;
        while (sent < len) {
            size_t chunk = len - sent;
            if (chunk > 128) {
                chunk = 128;
            }
            int written = usb_serial_jtag_write_bytes(data + sent, chunk, pdMS_TO_TICKS(100));
            if (written <= 0) {
                break;
            }
            sent += (size_t)written;
        }
    }
}

static void send_bytes_tcp(const uint8_t *data, size_t len)
{
    int sock = -1;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(20)) == pdTRUE) {
        sock = s_tcp_sock;
        xSemaphoreGive(s_lock);
    }
    if (sock >= 0) {
        (void)send(sock, data, len, 0);
    }
}

static void send_frame(link_tx_target_t target, uint8_t type, uint16_t seq, const void *payload, size_t len)
{
    if (len > MAMBA_LINK_MAX_PAYLOAD || !s_tx_lock) {
        return;
    }
    if (xSemaphoreTake(s_tx_lock, pdMS_TO_TICKS(100)) != pdTRUE) {
        return;
    }
    s_tx_buf[0] = MAMBA_LINK_SYNC0;
    s_tx_buf[1] = MAMBA_LINK_SYNC1;
    s_tx_buf[2] = MAMBA_PROTOCOL_VERSION;
    s_tx_buf[3] = type;
    s_tx_buf[4] = 0;
    s_tx_buf[5] = 0;
    put16(s_tx_buf + 6, seq);
    put16(s_tx_buf + 8, (uint16_t)len);
    put16(s_tx_buf + 10, payload ? crc16_ccitt(payload, len) : 0);
    if (payload && len > 0) {
        memcpy(s_tx_buf + LINK_HEADER_LEN, payload, len);
    }
    if (target == LINK_TX_USB) {
        send_bytes_usb(s_tx_buf, LINK_HEADER_LEN + len);
    } else if (target == LINK_TX_TCP) {
        send_bytes_tcp(s_tx_buf, LINK_HEADER_LEN + len);
    }
    xSemaphoreGive(s_tx_lock);
}

static void send_ack(uint16_t seq, const char *text)
{
    send_frame(LINK_TX_USB, MAMBA_LINK_TYPE_ACK, seq, text, strlen(text));
    send_frame(LINK_TX_TCP, MAMBA_LINK_TYPE_ACK, seq, text, strlen(text));
}

static void send_error(uint16_t seq, const char *text)
{
    send_frame(LINK_TX_USB, MAMBA_LINK_TYPE_ERROR, seq, text, strlen(text));
    send_frame(LINK_TX_TCP, MAMBA_LINK_TYPE_ERROR, seq, text, strlen(text));
}

static void send_udp_payload(uint8_t stream_id, const void *payload, size_t len)
{
    if (len + 20 > LINK_UDP_MAX || s_udp_host == 0) {
        return;
    }
    int udp_sock = ensure_udp_socket();
    if (udp_sock < 0) {
        return;
    }
    uint8_t buf[LINK_UDP_MAX];
    buf[0] = 'M';
    buf[1] = 'T';
    buf[2] = MAMBA_PROTOCOL_VERSION;
    buf[3] = stream_id;
    put32(buf + 4, s_udp_seq++);
    uint64_t now = (uint64_t)esp_timer_get_time();
    memcpy(buf + 8, &now, sizeof(now));
    put16(buf + 16, (uint16_t)len);
    buf[18] = 0;
    buf[19] = 0;
    memcpy(buf + 20, payload, len);

    struct sockaddr_in dst = {
        .sin_family = AF_INET,
        .sin_port = htons(s_udp_tel_port),
        .sin_addr.s_addr = s_udp_host,
    };
    (void)sendto(udp_sock, buf, len + 20, 0, (struct sockaddr *)&dst, sizeof(dst));
}

static void send_status(uint16_t seq)
{
    battery_snapshot_t bat;
    can_monitor_snapshot_t can;
    audio_status_t audio;
    alarm_status_t alarm;
    storage_info_t storage = {0};
    battery_get_snapshot(&bat);
    can_monitor_get_snapshot(&can);
    audio_get_status(&audio);
    alarm_get_status(&alarm);
    storage_get_info(&storage);

    char *json = malloc(MAMBA_LINK_MAX_PAYLOAD);
    if (!json) {
        send_error(seq, "no memory");
        return;
    }
    int n = snprintf(json, MAMBA_LINK_MAX_PAYLOAD,
        "{\"fw\":\"%s\",\"proto\":%u,\"device\":\"%s\","
        "\"battery\":{\"i2c\":%s,\"capacity\":%u,\"fused_mv\":%lu,\"adc_mv\":%lu,\"current_ma\":%ld,\"temp_decic\":%d},"
        "\"alarm\":{\"active\":%s,\"offset\":%lu,\"transitions\":%lu},"
        "\"audio\":{\"playing\":%s,\"alarm_file\":\"%s\",\"power_on_file\":\"%s\",\"current\":\"%s\"},"
        "\"can\":{\"started\":%s,\"bitrate\":%lu,\"rx\":%lu,\"dropped\":%lu},"
        "\"storage\":{\"total\":%u,\"used\":%u},"
        "\"wifi\":{\"ssid\":\"%s\",\"tcp_port\":%u,\"udp_hello\":%u,\"udp_telemetry\":%u}}",
        MAMBA_FIRMWARE_VERSION, MAMBA_PROTOCOL_VERSION, s_config.device_name,
        bat.i2c_online ? "true" : "false", bat.capacity_percent,
        (unsigned long)bat.fused_voltage_mv, (unsigned long)bat.adc_battery_mv,
        (long)bat.current_ma, bat.temperature_decic,
        alarm.active ? "true" : "false", (unsigned long)audio_get_alarm_offset(),
        (unsigned long)alarm.transitions,
        audio.playing ? "true" : "false", s_config.alarm_file, s_config.power_on_file, audio.file,
        can.started ? "true" : "false", (unsigned long)can.bitrate,
        (unsigned long)can.rx_count, (unsigned long)can.dropped_count,
        (unsigned)storage.total_bytes, (unsigned)storage.used_bytes,
        s_config.wifi_ssid, s_config.tcp_port, s_config.udp_hello_port, s_config.udp_telemetry_port);
    if (n > 0 && (size_t)n < MAMBA_LINK_MAX_PAYLOAD) {
        send_frame(LINK_TX_USB, MAMBA_LINK_TYPE_STATUS, seq, json, (size_t)n);
        send_frame(LINK_TX_TCP, MAMBA_LINK_TYPE_STATUS, seq, json, (size_t)n);
    } else {
        send_error(seq, "status too large");
    }
    free(json);
}

static void close_upload(void)
{
    if (s_upload.file) {
        fclose(s_upload.file);
    }
    if (s_upload.tmp_path[0]) {
        unlink(s_upload.tmp_path);
    }
    memset(&s_upload, 0, sizeof(s_upload));
}

static void handle_audio_begin(const link_rx_frame_t *frame)
{
    char body[MAMBA_LINK_MAX_PAYLOAD + 1];
    memcpy(body, frame->payload, frame->len);
    body[frame->len] = 0;
    char kind[16] = {0};
    uint32_t expected = 0;
    json_get_string(body, "kind", kind, sizeof(kind));
    json_get_u32(body, "size", &expected);
    bool alarm = strcmp(kind, "alarm") == 0;
    bool power = strcmp(kind, "poweron") == 0;
    if (!alarm && !power) {
        send_error(frame->seq, "bad audio kind");
        return;
    }
    size_t max_file = alarm ? alarm_max_file_bytes() : MAMBA_POWER_ON_MAX_FILE_BYTES;
    if (expected == 0 || expected > max_file) {
        send_error(frame->seq, "audio too large");
        return;
    }
    close_upload();
    strlcpy(s_upload.tmp_path, alarm ? "/spiffs/alarm.tmp" : "/spiffs/poweron.tmp", sizeof(s_upload.tmp_path));
    strlcpy(s_upload.final_path, alarm ? MAMBA_DEFAULT_ALARM_FILE : MAMBA_DEFAULT_POWER_ON_FILE, sizeof(s_upload.final_path));
    s_upload.alarm = alarm;
    s_upload.expected = expected;
    s_upload.max_file = max_file;
    s_upload.file = fopen(s_upload.tmp_path, "wb");
    if (!s_upload.file) {
        close_upload();
        send_error(frame->seq, "open failed");
        return;
    }
    send_ack(frame->seq, "audio begin");
}

static void handle_audio_chunk(const link_rx_frame_t *frame)
{
    if (!s_upload.file) {
        send_error(frame->seq, "no upload");
        return;
    }
    if (s_upload.received + frame->len > s_upload.max_file) {
        close_upload();
        send_error(frame->seq, "audio too large");
        return;
    }
    if (fwrite(frame->payload, 1, frame->len, s_upload.file) != frame->len) {
        close_upload();
        send_error(frame->seq, "write failed");
        return;
    }
    s_upload.received += frame->len;
    send_ack(frame->seq, "chunk");
}

static void handle_audio_end(const link_rx_frame_t *frame)
{
    if (!s_upload.file) {
        send_error(frame->seq, "no upload");
        return;
    }
    fclose(s_upload.file);
    s_upload.file = NULL;
    if (s_upload.expected != 0 && s_upload.received != s_upload.expected) {
        close_upload();
        send_error(frame->seq, "size mismatch");
        return;
    }
    wav_info_t info;
    if (audio_validate_wav_file(s_upload.tmp_path, &info) != ESP_OK) {
        close_upload();
        send_error(frame->seq, "invalid audio");
        return;
    }
    uint32_t max_samples = s_upload.alarm
        ? (uint32_t)((s_upload.max_file / MAMBA_AUDIO_ADPCM_BLOCK_ALIGN) * MAMBA_AUDIO_ADPCM_SAMPLES_PER_BLOCK)
        : MAMBA_POWER_ON_MAX_SAMPLES;
    if (info.decoded_samples > max_samples + info.samples_per_block) {
        close_upload();
        send_error(frame->seq, "audio too long");
        return;
    }
    unlink(s_upload.final_path);
    if (rename(s_upload.tmp_path, s_upload.final_path) != 0) {
        close_upload();
        send_error(frame->seq, "rename failed");
        return;
    }
    if (s_upload.alarm) {
        strlcpy(s_config.alarm_file, s_upload.final_path, sizeof(s_config.alarm_file));
        audio_reset_alarm_offset();
    } else {
        strlcpy(s_config.power_on_file, s_upload.final_path, sizeof(s_config.power_on_file));
    }
    storage_save_config(&s_config);
    memset(&s_upload, 0, sizeof(s_upload));
    send_ack(frame->seq, "audio uploaded");
}

static void handle_set_config(const link_rx_frame_t *frame)
{
    char body[MAMBA_LINK_MAX_PAYLOAD + 1];
    memcpy(body, frame->payload, frame->len);
    body[frame->len] = 0;
    uint32_t u32;
    float f;
    if (json_get_u32(body, "can_bitrate", &u32) && (u32 == 250000 || u32 == 500000 || u32 == 1000000)) {
        s_config.can_bitrate = u32;
    }
    if (json_get_u32(body, "telemetry_interval_ms", &u32) && u32 >= 20 && u32 <= 2000) {
        s_config.telemetry_interval_ms = u32;
    }
    if (json_get_u32(body, "low_voltage_enter_mv", &u32)) {
        s_config.low_voltage_enter_mv = u32;
    }
    if (json_get_u32(body, "low_voltage_exit_mv", &u32)) {
        s_config.low_voltage_exit_mv = u32;
    }
    if (json_get_float(body, "adc_factor", &f) && f > 0.5f && f < 1.5f) {
        s_config.adc_calibration_factor = f;
    }
    storage_save_config(&s_config);
    if (s_config_cb) {
        s_config_cb(&s_config);
    }
    send_ack(frame->seq, "config saved");
}

static void handle_wifi_config(const link_rx_frame_t *frame)
{
    char body[MAMBA_LINK_MAX_PAYLOAD + 1];
    memcpy(body, frame->payload, frame->len);
    body[frame->len] = 0;
    char ssid[sizeof(s_config.wifi_ssid)] = {0};
    char password[sizeof(s_config.wifi_password)] = {0};
    if (!json_get_string(body, "ssid", ssid, sizeof(ssid))) {
        send_error(frame->seq, "missing ssid");
        return;
    }
    json_get_string(body, "password", password, sizeof(password));
    strlcpy(s_config.wifi_ssid, ssid, sizeof(s_config.wifi_ssid));
    strlcpy(s_config.wifi_password, password, sizeof(s_config.wifi_password));
    storage_save_config(&s_config);
    if (s_config_cb) {
        s_config_cb(&s_config);
    }
    send_ack(frame->seq, "wifi saved");
}

static void handle_audio_test(const link_rx_frame_t *frame)
{
    if (bytes_contains(frame->payload, frame->len, "stop")) {
        audio_stop_and_save_offset();
    } else if (bytes_contains(frame->payload, frame->len, "power")) {
        audio_play_once(s_config.power_on_file);
    } else {
        audio_play_alarm(s_config.alarm_file, audio_get_alarm_offset());
    }
    send_ack(frame->seq, "audio command");
}

static void handle_frame(const link_rx_frame_t *frame)
{
    switch (frame->type) {
    case MAMBA_LINK_TYPE_HELLO:
    case MAMBA_LINK_TYPE_GET_STATUS:
        send_status(frame->seq);
        break;
    case MAMBA_LINK_TYPE_SET_CONFIG:
        handle_set_config(frame);
        break;
    case MAMBA_LINK_TYPE_WIFI_CONFIG:
        handle_wifi_config(frame);
        break;
    case MAMBA_LINK_TYPE_AUDIO_BEGIN:
        handle_audio_begin(frame);
        break;
    case MAMBA_LINK_TYPE_AUDIO_CHUNK:
        handle_audio_chunk(frame);
        break;
    case MAMBA_LINK_TYPE_AUDIO_END:
        handle_audio_end(frame);
        break;
    case MAMBA_LINK_TYPE_AUDIO_TEST:
        handle_audio_test(frame);
        break;
    default:
        send_error(frame->seq, "unknown type");
        break;
    }
}

static void parser_feed(link_parser_t *parser, const uint8_t *data, size_t len, bool from_usb)
{
    for (size_t i = 0; i < len; ++i) {
        if (parser->len == 0 && data[i] != MAMBA_LINK_SYNC0) {
            continue;
        }
        if (parser->len == 1 && data[i] != MAMBA_LINK_SYNC1) {
            parser->len = 0;
            continue;
        }
        if (parser->len < sizeof(parser->buffer)) {
            parser->buffer[parser->len++] = data[i];
        } else {
            parser->len = 0;
        }
        if (parser->len >= LINK_HEADER_LEN) {
            uint16_t payload_len = le16(parser->buffer + 8);
            if (payload_len > MAMBA_LINK_MAX_PAYLOAD) {
                parser->len = 0;
                continue;
            }
            size_t total = LINK_HEADER_LEN + payload_len;
            if (parser->len == total) {
                uint16_t crc = le16(parser->buffer + 10);
                if (parser->buffer[2] == MAMBA_PROTOCOL_VERSION &&
                    crc == crc16_ccitt(parser->buffer + LINK_HEADER_LEN, payload_len)) {
                    link_rx_frame_t frame = {
                        .type = parser->buffer[3],
                        .seq = le16(parser->buffer + 6),
                        .len = payload_len,
                    };
                    memcpy(frame.payload, parser->buffer + LINK_HEADER_LEN, payload_len);
                    if (xQueueSend(s_rx_queue, &frame, 0) == pdTRUE && from_usb) {
                        s_usb_rx_frames++;
                    }
                }
                parser->len = 0;
            }
        }
    }
}

static void usb_rx_task(void *arg)
{
    link_parser_t parser = {0};
    uint8_t buf[128];
    while (true) {
        int n = usb_serial_jtag_read_bytes(buf, sizeof(buf), pdMS_TO_TICKS(100));
        s_usb_rx_loops++;
        if (n > 0) {
            s_usb_rx_bytes += (uint32_t)n;
            parser_feed(&parser, buf, (size_t)n, true);
        } else {
            s_usb_rx_empty++;
        }
    }
}

static void tcp_rx_task(void *arg)
{
    link_parser_t parser = {0};
    uint8_t buf[256];
    while (true) {
        int sock = -1;
        if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
            sock = s_tcp_sock;
            xSemaphoreGive(s_lock);
        }
        if (sock < 0) {
            vTaskDelay(pdMS_TO_TICKS(250));
            continue;
        }
        int n = recv(sock, buf, sizeof(buf), 0);
        if (n > 0) {
            parser_feed(&parser, buf, (size_t)n, false);
        } else {
            link_detach_tcp_socket(sock);
            vTaskDelay(pdMS_TO_TICKS(250));
        }
    }
}

static void command_task(void *arg)
{
    link_rx_frame_t frame;
    while (true) {
        if (xQueueReceive(s_rx_queue, &frame, portMAX_DELAY) == pdTRUE) {
            handle_frame(&frame);
        }
    }
}

static void hello_task(void *arg)
{
    while (true) {
        if (s_udp_sock >= 0 && s_udp_host != 0) {
            int udp_sock = ensure_udp_socket();
            if (udp_sock < 0) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }
            char hello[160];
            int n = snprintf(hello, sizeof(hello), "{\"fw\":\"%s\",\"proto\":%u,\"device\":\"%s\"}",
                             MAMBA_FIRMWARE_VERSION, MAMBA_PROTOCOL_VERSION, s_config.device_name);
            struct sockaddr_in dst = {
                .sin_family = AF_INET,
                .sin_port = htons(s_udp_hello_port),
                .sin_addr.s_addr = s_udp_host,
            };
            (void)sendto(udp_sock, hello, n, 0, (struct sockaddr *)&dst, sizeof(dst));
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static void telemetry_task(void *arg)
{
    uint32_t tick = 0;
    while (true) {
        can_raw_frame_t frame;
        while (can_monitor_receive_frame(&frame, 0)) {
            link_publish_can_frame(&frame);
        }
        if ((tick++ % 10) == 0) {
            battery_snapshot_t bat;
            alarm_status_t alarm;
            battery_get_snapshot(&bat);
            alarm_get_status(&alarm);
            char json[256];
            int n = snprintf(json, sizeof(json),
                "{\"fused_mv\":%lu,\"adc_mv\":%lu,\"capacity\":%u,\"alarm\":%s,\"sample\":%lu,"
                "\"usb_rx\":%lu,\"usb_frames\":%lu,\"usb_loops\":%lu,\"usb_empty\":%lu,\"heap\":%lu}",
                (unsigned long)bat.fused_voltage_mv, (unsigned long)bat.adc_battery_mv,
                bat.capacity_percent, alarm.active ? "true" : "false", (unsigned long)bat.sample_count,
                (unsigned long)s_usb_rx_bytes, (unsigned long)s_usb_rx_frames,
                (unsigned long)s_usb_rx_loops, (unsigned long)s_usb_rx_empty,
                (unsigned long)esp_get_free_heap_size());
            if (n > 0) {
                send_udp_payload(MAMBA_STREAM_BATTERY, json, (size_t)n);
                send_frame(LINK_TX_USB, MAMBA_LINK_TYPE_TELEMETRY, s_seq++, json, (size_t)n);
                send_frame(LINK_TX_TCP, MAMBA_LINK_TYPE_TELEMETRY, s_seq++, json, (size_t)n);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(s_config.telemetry_interval_ms > 0 ? s_config.telemetry_interval_ms : 100));
    }
}

void link_publish_can_frame(const can_raw_frame_t *frame)
{
    if (!frame) {
        return;
    }
    uint8_t payload[32];
    put32(payload, frame->id);
    payload[4] = frame->dlc;
    payload[5] = 0;
    payload[6] = 0;
    payload[7] = 0;
    memcpy(payload + 8, &frame->timestamp_us, sizeof(frame->timestamp_us));
    memset(payload + 16, 0, 8);
    memcpy(payload + 16, frame->data, frame->dlc > 8 ? 8 : frame->dlc);
    send_udp_payload(MAMBA_STREAM_CAN_RAW, payload, 24);
    send_frame(LINK_TX_USB, MAMBA_LINK_TYPE_TELEMETRY, s_seq++, payload, 24);
    send_frame(LINK_TX_TCP, MAMBA_LINK_TYPE_TELEMETRY, s_seq++, payload, 24);
}

void link_publish_justfloat(const float *values, uint8_t count, uint32_t dropped_count)
{
    if (!values || count == 0 || count > 16) {
        return;
    }
    uint8_t payload[8 + sizeof(float) * 16];
    payload[0] = count;
    payload[1] = 0;
    put16(payload + 2, 0);
    put32(payload + 4, dropped_count);
    memcpy(payload + 8, values, sizeof(float) * count);
    send_udp_payload(MAMBA_STREAM_JUSTFLOAT, payload, 8 + sizeof(float) * count);
    send_frame(LINK_TX_USB, MAMBA_LINK_TYPE_TELEMETRY, s_seq++, payload, 8 + sizeof(float) * count);
    send_frame(LINK_TX_TCP, MAMBA_LINK_TYPE_TELEMETRY, s_seq++, payload, 8 + sizeof(float) * count);
}

void link_set_config_updated_callback(mamba_link_config_updated_cb_t cb)
{
    s_config_cb = cb;
}

void link_attach_tcp_socket(int sock, uint32_t host_ip_addr)
{
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (s_tcp_sock >= 0 && s_tcp_sock != sock) {
            close(s_tcp_sock);
        }
        s_tcp_sock = sock;
        xSemaphoreGive(s_lock);
    }
    link_set_udp_target(host_ip_addr, s_config.udp_hello_port, s_config.udp_telemetry_port);
    send_status(0);
}

void link_detach_tcp_socket(int sock)
{
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (s_tcp_sock == sock) {
            close(s_tcp_sock);
            s_tcp_sock = -1;
        }
        xSemaphoreGive(s_lock);
    }
}

void link_set_udp_target(uint32_t host_ip_addr, uint16_t hello_port, uint16_t telemetry_port)
{
    s_udp_host = host_ip_addr;
    s_udp_hello_port = hello_port;
    s_udp_tel_port = telemetry_port;
    if (host_ip_addr != 0) {
        (void)ensure_udp_socket();
    }
}

bool link_self_test(void)
{
    const uint8_t data[] = {'1','2','3','4','5','6','7','8','9'};
    return crc16_ccitt(data, sizeof(data)) == 0x29b1;
}

esp_err_t link_init(const mamba_config_t *config)
{
    if (config) {
        s_config = *config;
    } else {
        mamba_config_defaults(&s_config);
    }
    s_lock = xSemaphoreCreateMutex();
    s_tx_lock = xSemaphoreCreateMutex();
    s_rx_queue = xQueueCreate(LINK_QUEUE_DEPTH, sizeof(link_rx_frame_t));
    ESP_RETURN_ON_FALSE(s_lock && s_tx_lock && s_rx_queue, ESP_ERR_NO_MEM, TAG, "alloc");
    usb_serial_jtag_driver_config_t usb_cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb_cfg.rx_buffer_size = 512;
    usb_cfg.tx_buffer_size = 512;
    esp_err_t err = usb_serial_jtag_driver_install(&usb_cfg);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "usb serial install failed: %s", esp_err_to_name(err));
    }
    BaseType_t ok = xTaskCreate(usb_rx_task, "link_usb_rx", 6144, NULL, 5, NULL);
    ok &= xTaskCreate(tcp_rx_task, "link_tcp_rx", 6144, NULL, 5, NULL);
    ok &= xTaskCreate(command_task, "link_cmd", 4096, NULL, 5, NULL);
    ok &= xTaskCreate(hello_task, "link_hello", 3072, NULL, 3, NULL);
    ok &= xTaskCreate(telemetry_task, "link_tel", 4096, NULL, 3, NULL);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "tasks");
    ESP_LOGI(TAG, "MambaLink v%u ready, self-test=%s", MAMBA_PROTOCOL_VERSION, link_self_test() ? "ok" : "fail");
    return ESP_OK;
}
