#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "mamba_config.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    size_t total_bytes;
    size_t used_bytes;
} storage_info_t;

esp_err_t storage_init(mamba_config_t *config);
esp_err_t storage_load_config(mamba_config_t *config);
esp_err_t storage_save_config(const mamba_config_t *config);
esp_err_t storage_get_info(storage_info_t *info);
esp_err_t storage_list_audio_json(char *out, size_t out_len);

#ifdef __cplusplus
}
#endif
