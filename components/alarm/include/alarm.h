#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "mamba_config.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    bool active;
    uint32_t enter_candidate_ms;
    uint32_t exit_candidate_ms;
    uint32_t transitions;
} alarm_status_t;

esp_err_t alarm_init(const mamba_config_t *config);
void alarm_get_status(alarm_status_t *out);
bool alarm_self_test(void);

#ifdef __cplusplus
}
#endif
