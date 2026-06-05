#include "audio.h"

#include <stdio.h>
#include <string.h>

#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mamba_config.h"

typedef enum {
    AUDIO_CMD_PLAY,
    AUDIO_CMD_STOP,
    AUDIO_CMD_RESET_OFFSET,
} audio_cmd_type_t;

typedef struct {
    audio_cmd_type_t type;
    char path[64];
    uint32_t offset;
    bool loop;
} audio_cmd_t;

static const char *TAG = "audio";
static QueueHandle_t s_queue;
static SemaphoreHandle_t s_lock;
static audio_status_t s_status;
static uint32_t s_alarm_offset;
static volatile bool s_stop_requested;

static const int8_t IMA_INDEX_TABLE[16] = {
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
};

static const int16_t IMA_STEP_TABLE[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
};

static uint16_t le16(const uint8_t *p)
{
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void set_status(bool playing, const char *file, uint32_t offset, uint32_t rate)
{
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(20)) == pdTRUE) {
        s_status.playing = playing;
        if (file) {
            strlcpy(s_status.file, file, sizeof(s_status.file));
        }
        s_status.offset = offset;
        s_status.sample_rate_hz = rate;
        xSemaphoreGive(s_lock);
    }
}

void audio_get_status(audio_status_t *out)
{
    memset(out, 0, sizeof(*out));
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(20)) == pdTRUE) {
        *out = s_status;
        xSemaphoreGive(s_lock);
    }
}

uint32_t audio_get_alarm_offset(void)
{
    return s_alarm_offset;
}

void audio_reset_alarm_offset(void)
{
    s_alarm_offset = 0;
    if (s_queue) {
        audio_cmd_t cmd = {.type = AUDIO_CMD_RESET_OFFSET};
        xQueueSend(s_queue, &cmd, 0);
    }
}

esp_err_t audio_validate_wav_file(const char *path, wav_info_t *info)
{
    FILE *f = fopen(path, "rb");
    ESP_RETURN_ON_FALSE(f, ESP_ERR_NOT_FOUND, TAG, "open wav");
    uint8_t hdr[12];
    if (fread(hdr, 1, sizeof(hdr), f) != sizeof(hdr) ||
        memcmp(hdr, "RIFF", 4) != 0 || memcmp(hdr + 8, "WAVE", 4) != 0) {
        fclose(f);
        return ESP_ERR_INVALID_ARG;
    }

    wav_info_t parsed = {0};
    bool has_fmt = false;
    bool has_data = false;
    uint32_t fact_samples = 0;
    while (!has_data) {
        uint8_t chunk[8];
        if (fread(chunk, 1, sizeof(chunk), f) != sizeof(chunk)) {
            fclose(f);
            return ESP_ERR_INVALID_ARG;
        }
        uint32_t size = le32(chunk + 4);
        long payload = ftell(f);
        if (memcmp(chunk, "fmt ", 4) == 0) {
            uint8_t fmt[24] = {0};
            size_t read_len = size < sizeof(fmt) ? size : sizeof(fmt);
            if (size < 20 || fread(fmt, 1, read_len, f) != read_len) {
                fclose(f);
                return ESP_ERR_INVALID_ARG;
            }
            parsed.audio_format = le16(fmt);
            parsed.channels = le16(fmt + 2);
            parsed.sample_rate_hz = le32(fmt + 4);
            parsed.block_align = le16(fmt + 12);
            parsed.bits_per_sample = le16(fmt + 14);
            parsed.samples_per_block = le16(fmt + 18);
            has_fmt = parsed.audio_format == 0x0011;
        } else if (memcmp(chunk, "fact", 4) == 0) {
            uint8_t fact[4] = {0};
            size_t read_len = size < sizeof(fact) ? size : sizeof(fact);
            if (read_len == sizeof(fact) && fread(fact, 1, read_len, f) == read_len) {
                fact_samples = le32(fact);
            }
        } else if (memcmp(chunk, "data", 4) == 0) {
            parsed.data_offset = (uint32_t)payload;
            parsed.data_size = size;
            has_data = true;
            break;
        }
        fseek(f, payload + size + (size & 1), SEEK_SET);
    }
    fclose(f);

    bool rate_ok = parsed.sample_rate_hz == MAMBA_AUDIO_SAMPLE_RATE_HZ;
    if (!has_fmt || !has_data || parsed.channels != MAMBA_AUDIO_CHANNELS ||
        parsed.bits_per_sample != MAMBA_AUDIO_ADPCM_BITS_PER_SAMPLE || !rate_ok ||
        parsed.block_align != MAMBA_AUDIO_ADPCM_BLOCK_ALIGN ||
        parsed.samples_per_block != MAMBA_AUDIO_ADPCM_SAMPLES_PER_BLOCK ||
        parsed.data_size < parsed.block_align || (parsed.data_size % parsed.block_align) != 0) {
        return ESP_ERR_INVALID_ARG;
    }
    uint32_t block_samples = (parsed.data_size / parsed.block_align) * parsed.samples_per_block;
    parsed.decoded_samples = (fact_samples > 0 && fact_samples <= block_samples) ? fact_samples : block_samples;
    if (info) {
        *info = parsed;
    }
    return ESP_OK;
}

bool audio_self_test(void)
{
    uint8_t wav[] = {
        'R','I','F','F', 40,0,0,0, 'W','A','V','E',
        'f','m','t',' ', 20,0,0,0, 0x11,0, 1,0, 0x80,0x3e,0,0,
        0xae,0x1f,0,0, 0,1, 4,0, 2,0, 0xf9,1,
        'd','a','t','a', 0,1,0,0,
    };
    return memcmp(wav, "RIFF", 4) == 0 && le16(wav + 20) == 0x11 && le16(wav + 22) == 1 && le16(wav + 34) == 4;
}

static int16_t ima_decode_nibble(uint8_t nibble, int *predictor, int *index)
{
    int step = IMA_STEP_TABLE[*index];
    int diff = step >> 3;
    if (nibble & 1) {
        diff += step >> 2;
    }
    if (nibble & 2) {
        diff += step >> 1;
    }
    if (nibble & 4) {
        diff += step;
    }
    if (nibble & 8) {
        *predictor -= diff;
    } else {
        *predictor += diff;
    }
    if (*predictor > 32767) {
        *predictor = 32767;
    } else if (*predictor < -32768) {
        *predictor = -32768;
    }
    *index += IMA_INDEX_TABLE[nibble & 0x0f];
    if (*index < 0) {
        *index = 0;
    } else if (*index > 88) {
        *index = 88;
    }
    return (int16_t)*predictor;
}

static size_t decode_ima_block(const uint8_t *block, size_t block_len, int16_t *pcm, size_t pcm_capacity)
{
    if (block_len < 4 || pcm_capacity == 0) {
        return 0;
    }
    int predictor = (int16_t)le16(block);
    int index = block[2];
    if (index > 88) {
        index = 88;
    }
    size_t out = 0;
    pcm[out++] = (int16_t)predictor;
    for (size_t i = 4; i < block_len && out < pcm_capacity; ++i) {
        uint8_t byte = block[i];
        pcm[out++] = ima_decode_nibble(byte & 0x0f, &predictor, &index);
        if (out < pcm_capacity) {
            pcm[out++] = ima_decode_nibble(byte >> 4, &predictor, &index);
        }
    }
    return out;
}

static esp_err_t create_i2s(uint32_t sample_rate, i2s_chan_handle_t *tx)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, tx, NULL), TAG, "new i2s");
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(sample_rate),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = GPIO_NUM_NC,
            .bclk = MAMBA_I2S_BCLK_GPIO,
            .ws = MAMBA_I2S_WS_GPIO,
            .dout = MAMBA_I2S_DOUT_GPIO,
            .din = GPIO_NUM_NC,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(*tx, &std_cfg), TAG, "init i2s std");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(*tx), TAG, "enable i2s");
    return ESP_OK;
}

static void play_file(const char *path, uint32_t offset, bool loop)
{
    wav_info_t info;
    if (audio_validate_wav_file(path, &info) != ESP_OK) {
        ESP_LOGW(TAG, "invalid wav: %s", path);
        return;
    }
    FILE *f = fopen(path, "rb");
    if (!f) {
        return;
    }
    i2s_chan_handle_t tx = NULL;
    if (create_i2s(info.sample_rate_hz, &tx) != ESP_OK) {
        fclose(f);
        return;
    }

    uint8_t block[MAMBA_AUDIO_ADPCM_BLOCK_ALIGN];
    int16_t pcm[MAMBA_AUDIO_ADPCM_SAMPLES_PER_BLOCK];
    uint32_t pos = offset < info.data_size ? offset - (offset % info.block_align) : 0;
    set_status(true, path, pos, info.sample_rate_hz);
    s_stop_requested = false;
    while (!s_stop_requested) {
        if (pos >= info.data_size) {
            if (!loop) {
                break;
            }
            pos = 0;
        }
        uint32_t to_read = info.block_align;
        fseek(f, info.data_offset + pos, SEEK_SET);
        size_t n = fread(block, 1, to_read, f);
        if (n == 0) {
            if (!loop) {
                break;
            }
            pos = 0;
            continue;
        }
        if (n != info.block_align) {
            break;
        }
        size_t samples = decode_ima_block(block, n, pcm, sizeof(pcm) / sizeof(pcm[0]));
        size_t written = 0;
        i2s_channel_write(tx, pcm, samples * sizeof(pcm[0]), &written, 1000);
        pos += info.block_align;
        if (loop) {
            s_alarm_offset = pos;
        }
        set_status(true, path, pos, info.sample_rate_hz);
    }
    i2s_channel_disable(tx);
    i2s_del_channel(tx);
    fclose(f);
    set_status(false, path, loop ? s_alarm_offset : pos, info.sample_rate_hz);
}

static void audio_task(void *arg)
{
    audio_cmd_t cmd;
    while (true) {
        if (xQueueReceive(s_queue, &cmd, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (cmd.type == AUDIO_CMD_PLAY) {
            play_file(cmd.path, cmd.offset, cmd.loop);
        } else if (cmd.type == AUDIO_CMD_STOP) {
            s_stop_requested = true;
        } else if (cmd.type == AUDIO_CMD_RESET_OFFSET) {
            s_alarm_offset = 0;
        }
    }
}

esp_err_t audio_play_alarm(const char *path, uint32_t offset)
{
    ESP_RETURN_ON_FALSE(s_queue, ESP_ERR_INVALID_STATE, TAG, "audio not initialized");
    s_stop_requested = true;
    audio_cmd_t cmd = {.type = AUDIO_CMD_PLAY, .offset = offset};
    cmd.loop = true;
    strlcpy(cmd.path, path, sizeof(cmd.path));
    return xQueueSend(s_queue, &cmd, pdMS_TO_TICKS(20)) == pdTRUE ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t audio_play_once(const char *path)
{
    ESP_RETURN_ON_FALSE(s_queue, ESP_ERR_INVALID_STATE, TAG, "audio not initialized");
    s_stop_requested = true;
    audio_cmd_t cmd = {.type = AUDIO_CMD_PLAY, .offset = 0, .loop = false};
    strlcpy(cmd.path, path, sizeof(cmd.path));
    return xQueueSend(s_queue, &cmd, pdMS_TO_TICKS(20)) == pdTRUE ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t audio_stop_and_save_offset(void)
{
    ESP_RETURN_ON_FALSE(s_queue, ESP_ERR_INVALID_STATE, TAG, "audio not initialized");
    s_stop_requested = true;
    audio_cmd_t cmd = {.type = AUDIO_CMD_STOP};
    return xQueueSend(s_queue, &cmd, 0) == pdTRUE ? ESP_OK : ESP_OK;
}

esp_err_t audio_init(void)
{
    s_queue = xQueueCreate(4, sizeof(audio_cmd_t));
    s_lock = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_queue && s_lock, ESP_ERR_NO_MEM, TAG, "audio alloc");
    BaseType_t ok = xTaskCreate(audio_task, "audio_task", 4096, NULL, 4, NULL);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "audio task");
    ESP_LOGI(TAG, "audio ready, wav self-test=%s", audio_self_test() ? "ok" : "fail");
    return ESP_OK;
}
