#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint16_t audio_format;
    uint32_t sample_rate_hz;
    uint16_t channels;
    uint16_t bits_per_sample;
    uint16_t block_align;
    uint16_t samples_per_block;
    uint32_t data_offset;
    uint32_t data_size;
    uint32_t decoded_samples;
} wav_info_t;

typedef struct {
    bool playing;
    char file[64];
    uint32_t offset;
    uint32_t sample_rate_hz;
} audio_status_t;

esp_err_t audio_init(void);
esp_err_t audio_play_alarm(const char *path, uint32_t offset);
esp_err_t audio_play_once(const char *path);
esp_err_t audio_play_tone(uint32_t duration_ms, uint32_t frequency_hz);
esp_err_t audio_stop_and_save_offset(void);
void audio_reset_alarm_offset(void);
uint32_t audio_get_alarm_offset(void);
void audio_get_status(audio_status_t *out);
esp_err_t audio_validate_wav_file(const char *path, wav_info_t *info);
bool audio_self_test(void);

#ifdef __cplusplus
}
#endif
