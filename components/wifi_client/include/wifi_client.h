#pragma once

#include "esp_err.h"
#include "mamba_config.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t wifi_client_init(const mamba_config_t *config);
void wifi_client_apply_config(const mamba_config_t *config);

#ifdef __cplusplus
}
#endif
