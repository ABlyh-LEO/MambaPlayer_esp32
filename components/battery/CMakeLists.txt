idf_component_register(SRCS "battery.c"
                    INCLUDE_DIRS "include"
                    REQUIRES board telemetry_mux esp_adc esp_driver_i2c esp_timer freertos log)
