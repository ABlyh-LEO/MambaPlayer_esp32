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
#include "can_monitor.h"
#include "driver/usb_serial_jtag.h"
#include "esp_check.h"
#include "esp_err.h"
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
#include "telemetry_mux.h"

#define LINK_HEADER_LEN 12
#define LINK_UDP_MAX 1200
#define SELECTED_BATCH_SAMPLES 4
#define SELECTED_BATCH_INTERVAL_US 1000
#define SPEAKER_UDP_MAGIC0 'M'
#define SPEAKER_UDP_MAGIC1 'S'
#define SPEAKER_UDP_HEADER_LEN 16
#define SPEAKER_UDP_MAX_PACKET 512

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

typedef enum {
    SOURCE_NONE,
    SOURCE_ADC_BATTERY_MV,
    SOURCE_ADC_PIN_MV,
    SOURCE_ADC_RAW,
    SOURCE_I2C_VOLTAGE_MV,
    SOURCE_I2C_CURRENT_MA,
    SOURCE_I2C_CAPACITY,
    SOURCE_I2C_TEMPERATURE_C,
    SOURCE_JUSTFLOAT,
} selected_source_type_t;

typedef struct {
    selected_source_type_t type;
    uint8_t index;
    char key[32];
} selected_channel_t;

typedef struct {
    size_t len;
    uint8_t payload[4 + SELECTED_BATCH_SAMPLES * MAMBA_SELECTED_MAX_CHANNELS * sizeof(float)];
} selected_packet_t;

static const char *TAG = "link";
static SemaphoreHandle_t s_lock;
static SemaphoreHandle_t s_tx_lock;
static SemaphoreHandle_t s_selected_lock;
static QueueHandle_t s_selected_tx_queue;
static mamba_config_t s_config;
static mamba_link_config_updated_cb_t s_config_cb;
static int s_tcp_sock = -1;
static int s_udp_sock = -1;
static uint32_t s_udp_host;
static uint16_t s_udp_hello_port = MAMBA_LINK_UDP_HELLO_PORT;
static uint16_t s_udp_tel_port = MAMBA_LINK_UDP_TELEMETRY_PORT;
static uint16_t s_seq;
static uint32_t s_udp_seq;
static volatile uint32_t s_udp_send_count;
static volatile uint32_t s_udp_send_errors;
static volatile int s_udp_last_errno;
static volatile uint8_t s_udp_last_stream;
static volatile uint32_t s_telemetry_ticks;
static volatile uint32_t s_catalog_count;
static volatile uint32_t s_selected_sample_count;
static volatile uint32_t s_selected_packet_count;
static volatile uint32_t s_selected_drop_count;
static volatile uint32_t s_speaker_udp_rx_packets;
static volatile uint32_t s_speaker_udp_rx_samples;
static volatile uint32_t s_speaker_udp_drop_count;
static volatile uint32_t s_speaker_udp_bad_packets;
static volatile uint32_t s_speaker_udp_last_seq;
static volatile int s_speaker_udp_last_errno;
static TaskHandle_t s_speaker_udp_task_handle;
static audio_upload_t s_upload;
static uint8_t s_tx_buf[LINK_HEADER_LEN + MAMBA_LINK_MAX_PAYLOAD];
static volatile uint32_t s_usb_rx_bytes;
static volatile uint32_t s_usb_rx_frames;
static volatile uint32_t s_usb_rx_loops;
static volatile uint32_t s_usb_rx_empty;
static volatile bool s_upload_active;
static volatile bool s_audio_stream_active;
static selected_channel_t s_selected[MAMBA_SELECTED_MAX_CHANNELS];
static uint8_t s_selected_count;
static float s_selected_batch[SELECTED_BATCH_SAMPLES][MAMBA_SELECTED_MAX_CHANNELS];
static uint8_t s_selected_batch_count;

static void speaker_udp_task(void *arg);

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

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
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

static void put64(uint8_t *p, uint64_t v)
{
    for (int i = 0; i < 8; ++i) {
        p[i] = (uint8_t)(v >> (i * 8));
    }
}

static void put_float(uint8_t *p, float v)
{
    memcpy(p, &v, sizeof(v));
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

static bool parse_source_key(const char *key, selected_channel_t *out)
{
    if (!key || !out) {
        return false;
    }
    memset(out, 0, sizeof(*out));
    strlcpy(out->key, key, sizeof(out->key));
    if (strcmp(key, "adc.battery_mv") == 0) {
        out->type = SOURCE_ADC_BATTERY_MV;
        return true;
    }
    if (strcmp(key, "adc.pin_mv") == 0) {
        out->type = SOURCE_ADC_PIN_MV;
        return true;
    }
    if (strcmp(key, "adc.raw") == 0) {
        out->type = SOURCE_ADC_RAW;
        return true;
    }
    if (strcmp(key, "i2c.voltage_mv") == 0) {
        out->type = SOURCE_I2C_VOLTAGE_MV;
        return true;
    }
    if (strcmp(key, "i2c.current_ma") == 0) {
        out->type = SOURCE_I2C_CURRENT_MA;
        return true;
    }
    if (strcmp(key, "i2c.capacity") == 0) {
        out->type = SOURCE_I2C_CAPACITY;
        return true;
    }
    if (strcmp(key, "i2c.temperature_c") == 0) {
        out->type = SOURCE_I2C_TEMPERATURE_C;
        return true;
    }
    if (strncmp(key, "justfloat.", 10) == 0) {
        uint32_t index = (uint32_t)strtoul(key + 10, NULL, 10);
        if (index < MAMBA_TELEM_JUSTFLOAT_MAX) {
            out->type = SOURCE_JUSTFLOAT;
            out->index = (uint8_t)index;
            return true;
        }
    }
    return false;
}

static uint8_t parse_string_array(const char *json, const char *key, char out[][32], uint8_t max_count)
{
    const char *p = strstr(json, key);
    if (!p || !out || max_count == 0) {
        return 0;
    }
    p = strchr(p, '[');
    if (!p) {
        return 0;
    }
    uint8_t count = 0;
    while (*p && *p != ']' && count < max_count) {
        p = strchr(p, '"');
        if (!p) {
            break;
        }
        p++;
        size_t n = 0;
        while (*p && *p != '"' && n + 1 < 32) {
            out[count][n++] = *p++;
        }
        out[count][n] = 0;
        if (*p == '"') {
            count++;
            p++;
        }
    }
    return count;
}

static uint8_t parse_u32_array(const char *json, const char *key, uint32_t *out, uint8_t max_count)
{
    const char *p = strstr(json, key);
    if (!p || !out || max_count == 0) {
        return 0;
    }
    p = strchr(p, '[');
    if (!p) {
        return 0;
    }
    p++;
    uint8_t count = 0;
    while (*p && *p != ']' && count < max_count) {
        while (*p == ' ' || *p == ',') {
            p++;
        }
        if (*p == ']') {
            break;
        }
        char *end = NULL;
        out[count++] = (uint32_t)strtoul(p, &end, 0) & 0x7ff;
        if (end == p) {
            break;
        }
        p = end;
    }
    return count;
}

static size_t alarm_max_file_bytes(void)
{
    storage_info_t info = {0};
    if (storage_get_info(&info) != ESP_OK || info.total_bytes == 0) {
        return MAMBA_STORAGE_PARTITION_BYTES - MAMBA_POWER_ON_MAX_FILE_BYTES - MAMBA_AUDIO_STORAGE_SAFETY_BYTES;
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

static void stop_speaker_stream(bool resume_alarm)
{
    if (!s_audio_stream_active) {
        return;
    }
    s_audio_stream_active = false;
    esp_err_t err = audio_stream_stop();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE && err != ESP_ERR_TIMEOUT) {
        ESP_LOGW(TAG, "speaker stream stop failed: %s", esp_err_to_name(err));
    }
    if (resume_alarm) {
        alarm_status_t alarm = {0};
        alarm_get_status(&alarm);
        if (alarm.active) {
            audio_play_alarm(s_config.alarm_file, audio_get_alarm_offset());
        }
    }
}

static void drain_realtime_telemetry(void)
{
    telemetry_adc_sample_t adc;
    telemetry_can_frame_t can;
    telemetry_rm_motor_t motor;
    telemetry_justfloat_frame_t justfloat;
    while (telemetry_mux_receive_adc(&adc, 0)) {
    }
    while (telemetry_mux_receive_can(&can, 0)) {
    }
    while (telemetry_mux_receive_rm_motor(&motor, 0)) {
    }
    while (telemetry_mux_receive_justfloat(&justfloat, 0)) {
    }
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
    int sent = sendto(udp_sock, buf, len + 20, 0, (struct sockaddr *)&dst, sizeof(dst));
    s_udp_last_stream = stream_id;
    if (sent > 0) {
        s_udp_send_count++;
    } else {
        s_udp_send_errors++;
        s_udp_last_errno = errno;
    }
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
        "\"audio\":{\"playing\":%s,\"alarm_file\":\"%s\",\"power_on_file\":\"%s\",\"current\":\"%s\","
        "\"diag\":{\"i2s_starts\":%lu,\"write_calls\":%lu,\"write_bytes\":%lu,\"write_errors\":%lu,\"last_write_bytes\":%lu,"
        "\"speaker_udp_packets\":%lu,\"speaker_udp_samples\":%lu,\"speaker_udp_drops\":%lu,\"speaker_udp_bad\":%lu,"
        "\"speaker_udp_seq\":%lu,\"speaker_udp_errno\":%d,\"speaker_udp_port\":%u}},"
        "\"can\":{\"started\":%s,\"bitrate\":%lu,\"rx\":%lu,\"dropped\":%lu,"
        "\"forward_filter\":\"%s\",\"parser\":\"host\"},"
        "\"link\":{\"udp_tel\":%u,\"udp_sent\":%lu,\"udp_errors\":%lu,\"udp_errno\":%d,\"udp_stream\":%u,"
        "\"tel_ticks\":%lu,\"catalogs\":%lu,\"sel_samples\":%lu,\"sel_packets\":%lu,\"sel_drops\":%lu,"
        "\"stream_active\":%s},"
        "\"storage\":{\"total\":%u,\"used\":%u},"
        "\"wifi\":{\"ssid\":\"%s\",\"tcp_port\":%u,\"udp_hello\":%u,\"udp_telemetry\":%u}}",
        MAMBA_FIRMWARE_VERSION, MAMBA_PROTOCOL_VERSION, s_config.device_name,
        bat.i2c_online ? "true" : "false", bat.capacity_percent,
        (unsigned long)bat.fused_voltage_mv, (unsigned long)bat.adc_battery_mv,
        (long)bat.current_ma, bat.temperature_decic,
        alarm.active ? "true" : "false", (unsigned long)audio_get_alarm_offset(),
        (unsigned long)alarm.transitions,
        audio.playing ? "true" : "false", s_config.alarm_file, s_config.power_on_file, audio.file,
        (unsigned long)audio.i2s_starts, (unsigned long)audio.write_calls,
        (unsigned long)audio.write_bytes, (unsigned long)audio.write_errors,
        (unsigned long)audio.last_write_bytes,
        (unsigned long)s_speaker_udp_rx_packets, (unsigned long)s_speaker_udp_rx_samples,
        (unsigned long)s_speaker_udp_drop_count, (unsigned long)s_speaker_udp_bad_packets,
        (unsigned long)s_speaker_udp_last_seq, s_speaker_udp_last_errno,
        MAMBA_LINK_UDP_SPEAKER_PORT,
        can.started ? "true" : "false", (unsigned long)can.bitrate,
        (unsigned long)can.rx_count, (unsigned long)can.dropped_count, s_config.can_filter,
        s_udp_tel_port, (unsigned long)s_udp_send_count, (unsigned long)s_udp_send_errors,
        s_udp_last_errno, s_udp_last_stream,
        (unsigned long)s_telemetry_ticks, (unsigned long)s_catalog_count,
        (unsigned long)s_selected_sample_count, (unsigned long)s_selected_packet_count,
        (unsigned long)s_selected_drop_count,
        s_audio_stream_active ? "true" : "false",
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
    s_upload_active = false;
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
    audio_stop_and_save_offset();
    close_upload();
    strlcpy(s_upload.tmp_path, alarm ? "/spiffs/alarm.tmp" : "/spiffs/poweron.tmp", sizeof(s_upload.tmp_path));
    strlcpy(s_upload.final_path, alarm ? MAMBA_DEFAULT_ALARM_FILE : MAMBA_DEFAULT_POWER_ON_FILE, sizeof(s_upload.final_path));
    storage_info_t info = {0};
    if (storage_get_info(&info) == ESP_OK && info.total_bytes > info.used_bytes) {
        uint32_t free_bytes = info.total_bytes - info.used_bytes;
        if (free_bytes < expected + MAMBA_WAV_HEADER_ALLOWANCE_BYTES) {
            unlink(s_upload.final_path);
        }
    }
    s_upload.alarm = alarm;
    s_upload.expected = expected;
    s_upload.max_file = max_file;
    s_upload.file = fopen(s_upload.tmp_path, "wb");
    if (!s_upload.file) {
        close_upload();
        send_error(frame->seq, "open failed");
        return;
    }
    s_upload_active = true;
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
    s_upload_active = false;
    send_ack(frame->seq, "audio uploaded");
}

static void handle_set_config(const link_rx_frame_t *frame)
{
    char body[MAMBA_LINK_MAX_PAYLOAD + 1];
    memcpy(body, frame->payload, frame->len);
    body[frame->len] = 0;
    uint32_t u32;
    float f;
    char text[sizeof(s_config.device_name)] = {0};
    if (json_get_string(body, "device_name", text, sizeof(text)) && text[0] != '\0') {
        strlcpy(s_config.device_name, text, sizeof(s_config.device_name));
    }
    if (json_get_u32(body, "can_bitrate", &u32) && (u32 == 250000 || u32 == 500000 || u32 == 1000000)) {
        s_config.can_bitrate = u32;
    }
    if (json_get_u32(body, "telemetry_interval_ms", &u32) && u32 >= 1 && u32 <= 2000) {
        s_config.telemetry_interval_ms = u32;
    }
    char filter[sizeof(s_config.can_filter)] = {0};
    if (json_get_string(body, "can_filter", filter, sizeof(filter))) {
        strlcpy(s_config.can_filter, filter, sizeof(s_config.can_filter));
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
    can_monitor_apply_config(&s_config);
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
        stop_speaker_stream(false);
    } else if (bytes_contains(frame->payload, frame->len, "tone30")) {
        audio_play_tone(30000, 1000);
    } else if (bytes_contains(frame->payload, frame->len, "tone")) {
        audio_play_tone(2000, 1000);
    } else if (bytes_contains(frame->payload, frame->len, "power")) {
        audio_play_once(s_config.power_on_file);
    } else {
        audio_play_alarm(s_config.alarm_file, audio_get_alarm_offset());
    }
    send_ack(frame->seq, "audio command");
}

static void handle_audio_stream_start(const link_rx_frame_t *frame)
{
    char body[MAMBA_LINK_MAX_PAYLOAD + 1];
    memcpy(body, frame->payload, frame->len);
    body[frame->len] = 0;
    uint32_t rate = MAMBA_AUDIO_SAMPLE_RATE_HZ;
    json_get_u32(body, "sample_rate", &rate);
    esp_err_t err = audio_stream_start(rate);
    if (err == ESP_OK) {
        s_audio_stream_active = true;
        s_speaker_udp_rx_packets = 0;
        s_speaker_udp_rx_samples = 0;
        s_speaker_udp_drop_count = 0;
        s_speaker_udp_bad_packets = 0;
        s_speaker_udp_last_seq = 0;
        s_speaker_udp_last_errno = 0;
        if (!s_speaker_udp_task_handle &&
            xTaskCreate(speaker_udp_task, "link_spk_udp", 3072, NULL, 5, &s_speaker_udp_task_handle) != pdPASS) {
            s_audio_stream_active = false;
            audio_stream_stop();
            send_error(frame->seq, "speaker UDP task start failed");
            return;
        }
        char text[64];
        snprintf(text, sizeof(text), "audio stream start udp:%u", MAMBA_LINK_UDP_SPEAKER_PORT);
        send_ack(frame->seq, text);
    } else {
        s_audio_stream_active = false;
        char text[64];
        snprintf(text, sizeof(text), "audio stream start failed: %s", esp_err_to_name(err));
        send_error(frame->seq, text);
    }
}

static void handle_audio_stream_pcm(const link_rx_frame_t *frame)
{
    if ((frame->len % sizeof(int16_t)) != 0) {
        send_error(frame->seq, "bad pcm chunk");
        return;
    }
    esp_err_t err = audio_stream_write_pcm((const int16_t *)frame->payload, frame->len / sizeof(int16_t));
    if (err != ESP_OK) {
        send_error(frame->seq, "pcm write failed");
    }
}

static void handle_audio_stream_stop(const link_rx_frame_t *frame)
{
    stop_speaker_stream(true);
    send_ack(frame->seq, "audio stream stop");
}

static void handle_set_stream_channels(const link_rx_frame_t *frame)
{
    char body[MAMBA_LINK_MAX_PAYLOAD + 1];
    memcpy(body, frame->payload, frame->len);
    body[frame->len] = 0;
    char keys[MAMBA_SELECTED_MAX_CHANNELS][32] = {0};
    uint8_t parsed = parse_string_array(body, "channels", keys, MAMBA_SELECTED_MAX_CHANNELS);
    selected_channel_t next[MAMBA_SELECTED_MAX_CHANNELS] = {0};
    uint8_t count = 0;
    for (uint8_t i = 0; i < parsed; ++i) {
        if (parse_source_key(keys[i], &next[count])) {
            count++;
        }
    }
    if (xSemaphoreTake(s_selected_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        memcpy(s_selected, next, sizeof(s_selected));
        s_selected_count = count;
        s_selected_batch_count = 0;
        xSemaphoreGive(s_selected_lock);
    }
    send_ack(frame->seq, "stream channels updated");
}

static void handle_set_can_forward_ids(const link_rx_frame_t *frame)
{
    char body[MAMBA_LINK_MAX_PAYLOAD + 1];
    memcpy(body, frame->payload, frame->len);
    body[frame->len] = 0;
    uint32_t ids[MAMBA_CAN_FORWARD_MAX_IDS] = {0};
    uint8_t count = parse_u32_array(body, "ids", ids, MAMBA_CAN_FORWARD_MAX_IDS);
    can_monitor_set_forward_ids(ids, count);
    size_t used = 0;
    s_config.can_filter[0] = '\0';
    for (uint8_t i = 0; i < count; ++i) {
        int n = snprintf(s_config.can_filter + used, sizeof(s_config.can_filter) - used,
                         "%s0x%03lX", i == 0 ? "" : ",", (unsigned long)ids[i]);
        if (n < 0 || (size_t)n >= sizeof(s_config.can_filter) - used) {
            break;
        }
        used += (size_t)n;
    }
    storage_save_config(&s_config);
    send_ack(frame->seq, "can forward ids updated");
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
    case MAMBA_LINK_TYPE_AUDIO_STREAM_START:
        handle_audio_stream_start(frame);
        break;
    case MAMBA_LINK_TYPE_AUDIO_STREAM_PCM:
        handle_audio_stream_pcm(frame);
        break;
    case MAMBA_LINK_TYPE_AUDIO_STREAM_STOP:
        handle_audio_stream_stop(frame);
        break;
    case MAMBA_LINK_TYPE_SET_STREAM_CHANNELS:
        handle_set_stream_channels(frame);
        break;
    case MAMBA_LINK_TYPE_SET_CAN_FORWARD_IDS:
        handle_set_can_forward_ids(frame);
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
                    if (frame.type == MAMBA_LINK_TYPE_AUDIO_STREAM_PCM) {
                        handle_audio_stream_pcm(&frame);
                        if (from_usb) {
                            s_usb_rx_frames++;
                        }
                    } else {
                        handle_frame(&frame);
                        if (from_usb) {
                            s_usb_rx_frames++;
                        }
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
        } else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            continue;
        } else {
            link_detach_tcp_socket(sock);
            vTaskDelay(pdMS_TO_TICKS(250));
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

static void speaker_udp_task(void *arg)
{
    int sock = -1;
    while (s_audio_stream_active) {
        if (sock < 0) {
            sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
            if (sock < 0) {
                s_speaker_udp_last_errno = errno;
                vTaskDelay(pdMS_TO_TICKS(500));
                continue;
            }
            struct sockaddr_in addr = {
                .sin_family = AF_INET,
                .sin_port = htons(MAMBA_LINK_UDP_SPEAKER_PORT),
                .sin_addr.s_addr = htonl(INADDR_ANY),
            };
            if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
                s_speaker_udp_last_errno = errno;
                close(sock);
                sock = -1;
                vTaskDelay(pdMS_TO_TICKS(500));
                continue;
            }
            struct timeval timeout = {
                .tv_sec = 0,
                .tv_usec = 100000,
            };
            setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        }

        uint8_t packet[SPEAKER_UDP_MAX_PACKET];
        int n = recvfrom(sock, packet, sizeof(packet), 0, NULL, NULL);
        if (n < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK) {
                s_speaker_udp_last_errno = errno;
                close(sock);
                sock = -1;
            }
            continue;
        }
        if (!s_audio_stream_active) {
            s_speaker_udp_drop_count++;
            continue;
        }
        if (n < SPEAKER_UDP_HEADER_LEN || packet[0] != SPEAKER_UDP_MAGIC0 ||
            packet[1] != SPEAKER_UDP_MAGIC1 || packet[2] != MAMBA_PROTOCOL_VERSION) {
            s_speaker_udp_bad_packets++;
            continue;
        }
        uint32_t seq = le32(packet + 4);
        uint16_t sample_count = le16(packet + 12);
        uint16_t byte_count = le16(packet + 14);
        if (byte_count != sample_count * sizeof(int16_t) ||
            SPEAKER_UDP_HEADER_LEN + byte_count != (uint16_t)n ||
            (byte_count % sizeof(int16_t)) != 0) {
            s_speaker_udp_bad_packets++;
            continue;
        }
        esp_err_t err = audio_stream_write_pcm((const int16_t *)(packet + SPEAKER_UDP_HEADER_LEN), sample_count);
        s_speaker_udp_last_seq = seq;
        if (err == ESP_OK) {
            s_speaker_udp_rx_packets++;
            s_speaker_udp_rx_samples += sample_count;
        } else {
            s_speaker_udp_drop_count++;
            s_speaker_udp_last_errno = err;
        }
    }
    if (sock >= 0) {
        close(sock);
    }
    s_speaker_udp_task_handle = NULL;
    vTaskDelete(NULL);
}

static float selected_value(const selected_channel_t *channel, const battery_snapshot_t *bat,
                            const telemetry_adc_sample_t *adc, bool has_adc,
                            const telemetry_justfloat_frame_t *jf, bool has_jf)
{
    switch (channel->type) {
    case SOURCE_ADC_BATTERY_MV:
        return has_adc ? (float)adc->battery_mv : 0.0f;
    case SOURCE_ADC_PIN_MV:
        return has_adc ? (float)adc->pin_mv : 0.0f;
    case SOURCE_ADC_RAW:
        return has_adc ? (float)adc->raw : 0.0f;
    case SOURCE_I2C_VOLTAGE_MV:
        return (float)bat->i2c_voltage_mv;
    case SOURCE_I2C_CURRENT_MA:
        return (float)bat->current_ma;
    case SOURCE_I2C_CAPACITY:
        return (float)bat->capacity_percent;
    case SOURCE_I2C_TEMPERATURE_C:
        return (float)bat->temperature_decic / 10.0f;
    case SOURCE_JUSTFLOAT:
        return (has_jf && channel->index < jf->count) ? jf->values[channel->index] : 0.0f;
    default:
        return 0.0f;
    }
}

static void publish_selected_values(void)
{
    selected_channel_t selected[MAMBA_SELECTED_MAX_CHANNELS];
    uint8_t count = 0;
    if (xSemaphoreTake(s_selected_lock, pdMS_TO_TICKS(1)) == pdTRUE) {
        count = s_selected_count;
        memcpy(selected, s_selected, sizeof(selected));
        xSemaphoreGive(s_selected_lock);
    }
    if (count == 0) {
        s_selected_batch_count = 0;
        return;
    }
    battery_snapshot_t bat;
    telemetry_adc_sample_t adc = {0};
    telemetry_justfloat_frame_t jf = {0};
    battery_get_snapshot(&bat);
    bool has_adc = telemetry_mux_get_latest_adc(&adc);
    bool has_jf = telemetry_mux_get_latest_justfloat(&jf);
    if (s_selected_batch_count >= SELECTED_BATCH_SAMPLES) {
        s_selected_batch_count = 0;
    }
    for (uint8_t i = 0; i < count; ++i) {
        s_selected_batch[s_selected_batch_count][i] =
            selected_value(&selected[i], &bat, &adc, has_adc, &jf, has_jf);
    }
    s_selected_sample_count++;
    s_selected_batch_count++;
    if (s_selected_batch_count < SELECTED_BATCH_SAMPLES) {
        return;
    }

    selected_packet_t packet = {0};
    packet.payload[0] = count;
    packet.payload[1] = SELECTED_BATCH_SAMPLES;
    put16(packet.payload + 2, SELECTED_BATCH_INTERVAL_US);
    size_t offset = 4;
    for (uint8_t sample = 0; sample < SELECTED_BATCH_SAMPLES; ++sample) {
        for (uint8_t i = 0; i < count; ++i) {
            put_float(packet.payload + offset, s_selected_batch[sample][i]);
            offset += sizeof(float);
        }
    }
    packet.len = offset;
    if (s_selected_tx_queue && xQueueSend(s_selected_tx_queue, &packet, 0) != pdTRUE) {
        selected_packet_t old_packet;
        if (xQueueReceive(s_selected_tx_queue, &old_packet, 0) == pdTRUE) {
            s_selected_drop_count++;
        }
        if (xQueueSend(s_selected_tx_queue, &packet, 0) != pdTRUE) {
            s_selected_drop_count++;
        }
    }
    s_selected_packet_count++;
    s_selected_batch_count = 0;
}

static void selected_sample_task(void *arg)
{
    TickType_t last = xTaskGetTickCount();
    while (true) {
        if (!s_audio_stream_active) {
            publish_selected_values();
        } else {
            s_selected_batch_count = 0;
        }
        vTaskDelayUntil(&last, pdMS_TO_TICKS(1));
    }
}

static void selected_tx_task(void *arg)
{
    selected_packet_t packet;
    while (true) {
        if (xQueueReceive(s_selected_tx_queue, &packet, portMAX_DELAY) == pdTRUE) {
            send_udp_payload(MAMBA_STREAM_SELECTED_VALUES, packet.payload, packet.len);
        }
    }
}

static void publish_can_last(void)
{
    can_raw_frame_t frames[MAMBA_CAN_FORWARD_MAX_IDS];
    uint8_t count = can_monitor_collect_forward_frames(frames, MAMBA_CAN_FORWARD_MAX_IDS);
    if (count == 0) {
        return;
    }
    uint8_t payload[4 + MAMBA_CAN_FORWARD_MAX_IDS * 24];
    put16(payload, count);
    put16(payload + 2, 0);
    for (uint8_t i = 0; i < count; ++i) {
        uint8_t *p = payload + 4 + i * 24;
        put32(p, frames[i].id);
        p[4] = frames[i].dlc;
        p[5] = p[6] = p[7] = 0;
        put64(p + 8, frames[i].timestamp_us);
        memset(p + 16, 0, 8);
        memcpy(p + 16, frames[i].data, frames[i].dlc > 8 ? 8 : frames[i].dlc);
    }
    send_udp_payload(MAMBA_STREAM_CAN_LAST, payload, 4 + count * 24);
}

static void publish_catalog(uint32_t tick)
{
    if ((tick % 50) != 0) {
        return;
    }
    battery_snapshot_t bat;
    alarm_status_t alarm;
    can_monitor_snapshot_t can;
    battery_get_snapshot(&bat);
    alarm_get_status(&alarm);
    can_monitor_get_snapshot(&can);
    char json[900];
    int n = snprintf(json, sizeof(json),
        "{\"type\":\"catalog\",\"fw\":\"%s\",\"proto\":%u,\"selected\":%u,"
        "\"sources\":["
        "{\"key\":\"adc.battery_mv\",\"name\":\"ADC Battery\",\"unit\":\"mV\",\"value\":%lu,\"rate_hz\":500},"
        "{\"key\":\"adc.pin_mv\",\"name\":\"ADC Pin\",\"unit\":\"mV\",\"value\":%d,\"rate_hz\":500},"
        "{\"key\":\"adc.raw\",\"name\":\"ADC Raw\",\"unit\":\"\",\"value\":%d,\"rate_hz\":500},"
        "{\"key\":\"i2c.voltage_mv\",\"name\":\"I2C Voltage\",\"unit\":\"mV\",\"value\":%ld,\"rate_hz\":2},"
        "{\"key\":\"i2c.current_ma\",\"name\":\"I2C Current\",\"unit\":\"mA\",\"value\":%ld,\"rate_hz\":2},"
        "{\"key\":\"i2c.capacity\",\"name\":\"I2C Capacity\",\"unit\":\"%%\",\"value\":%u,\"rate_hz\":2},"
        "{\"key\":\"i2c.temperature_c\",\"name\":\"I2C Temp\",\"unit\":\"C\",\"value\":%.1f,\"rate_hz\":2}",
        MAMBA_FIRMWARE_VERSION, MAMBA_PROTOCOL_VERSION, s_selected_count,
        (unsigned long)bat.adc_battery_mv, bat.adc_pin_mv, bat.adc_raw,
        (long)bat.i2c_voltage_mv, (long)bat.current_ma, bat.capacity_percent,
        (double)bat.temperature_decic / 10.0);
    if (n < 0 || n >= (int)sizeof(json)) {
        return;
    }
    size_t used = (size_t)n;
    telemetry_justfloat_frame_t jf = {0};
    if (telemetry_mux_get_latest_justfloat(&jf)) {
        for (uint8_t i = 0; i < jf.count && i < MAMBA_TELEM_JUSTFLOAT_MAX; ++i) {
            n = snprintf(json + used, sizeof(json) - used,
                         ",{\"key\":\"justfloat.%u\",\"name\":\"JustFloat %u\",\"unit\":\"\",\"value\":%.5g,\"rate_hz\":1000}",
                         i, i, (double)jf.values[i]);
            if (n < 0 || n >= (int)(sizeof(json) - used)) {
                return;
            }
            used += (size_t)n;
        }
    }
    n = snprintf(json + used, sizeof(json) - used,
                 "],\"can\":{\"started\":%s,\"bitrate\":%lu,\"rx\":%lu,\"dropped\":%lu},"
                 "\"alarm\":%s,\"heap\":%lu}",
                 can.started ? "true" : "false", (unsigned long)can.bitrate,
                 (unsigned long)can.rx_count, (unsigned long)can.dropped_count,
                 alarm.active ? "true" : "false", (unsigned long)esp_get_free_heap_size());
    if (n > 0 && n < (int)(sizeof(json) - used)) {
        used += (size_t)n;
        send_udp_payload(MAMBA_STREAM_CATALOG, json, used);
        s_catalog_count++;
        if (!s_upload_active) {
            send_frame(LINK_TX_USB, MAMBA_LINK_TYPE_TELEMETRY, s_seq++, json, used);
        }
    }
}

static void telemetry_task(void *arg)
{
    uint32_t tick = 0;
    while (true) {
        s_telemetry_ticks++;
        uint32_t interval_ms = s_config.telemetry_interval_ms;
        if (interval_ms < 1 || interval_ms > 2000) {
            interval_ms = MAMBA_TELEMETRY_BATCH_INTERVAL_MS;
        }
        if (s_audio_stream_active) {
            drain_realtime_telemetry();
            publish_catalog(tick++);
            vTaskDelay(pdMS_TO_TICKS(interval_ms));
            continue;
        }
        publish_can_last();
        publish_catalog(tick++);
        vTaskDelay(pdMS_TO_TICKS(interval_ms));
    }
}

void link_set_config_updated_callback(mamba_link_config_updated_cb_t cb)
{
    s_config_cb = cb;
}

void link_attach_tcp_socket(int sock, uint32_t host_ip_addr)
{
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (s_tcp_sock >= 0 && s_tcp_sock != sock) {
            shutdown(s_tcp_sock, SHUT_RDWR);
            close(s_tcp_sock);
        }
        s_tcp_sock = sock;
        xSemaphoreGive(s_lock);
    }
    link_set_udp_target(host_ip_addr, s_config.udp_hello_port, s_config.udp_telemetry_port);
    send_status(0);
    publish_catalog(0);
}

void link_detach_tcp_socket(int sock)
{
    bool should_stop_stream = false;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (s_tcp_sock == sock) {
            should_stop_stream = true;
            shutdown(s_tcp_sock, SHUT_RDWR);
            close(s_tcp_sock);
            s_tcp_sock = -1;
        }
        xSemaphoreGive(s_lock);
    }
    if (should_stop_stream) {
        stop_speaker_stream(true);
    }
}

bool link_is_tcp_socket_attached(int sock)
{
    bool attached = false;
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        attached = s_tcp_sock == sock;
        xSemaphoreGive(s_lock);
    }
    return attached;
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
    s_selected_lock = xSemaphoreCreateMutex();
    s_selected_tx_queue = xQueueCreate(16, sizeof(selected_packet_t));
    ESP_RETURN_ON_FALSE(s_lock && s_tx_lock && s_selected_lock && s_selected_tx_queue, ESP_ERR_NO_MEM, TAG, "alloc");
    usb_serial_jtag_driver_config_t usb_cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb_cfg.rx_buffer_size = 512;
    usb_cfg.tx_buffer_size = 512;
    esp_err_t err = usb_serial_jtag_driver_install(&usb_cfg);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "usb serial install failed: %s", esp_err_to_name(err));
    }
    BaseType_t ok = xTaskCreate(usb_rx_task, "link_usb_rx", 4096, NULL, 5, NULL);
    ok &= xTaskCreate(tcp_rx_task, "link_tcp_rx", 4096, NULL, 5, NULL);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "core link tasks");
    if (xTaskCreate(hello_task, "link_hello", 2048, NULL, 3, NULL) != pdPASS) {
        ESP_LOGW(TAG, "hello task disabled");
    }
    if (xTaskCreate(telemetry_task, "link_tel", 3072, NULL, 6, NULL) != pdPASS) {
        ESP_LOGW(TAG, "telemetry task disabled");
    }
    if (xTaskCreate(selected_sample_task, "link_sel_s", 3072, NULL, 10, NULL) != pdPASS) {
        ESP_LOGW(TAG, "selected sample task disabled");
    }
    if (xTaskCreate(selected_tx_task, "link_sel_tx", 3072, NULL, 7, NULL) != pdPASS) {
        ESP_LOGW(TAG, "selected TX task disabled");
    }
    ESP_LOGI(TAG, "MambaLink v%u ready, self-test=%s", MAMBA_PROTOCOL_VERSION, link_self_test() ? "ok" : "fail");
    return ESP_OK;
}
