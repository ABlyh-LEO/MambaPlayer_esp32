idf_component_register(SRCS "audio.c"
                    INCLUDE_DIRS "include"
                    REQUIRES board esp_driver_i2s freertos log)
