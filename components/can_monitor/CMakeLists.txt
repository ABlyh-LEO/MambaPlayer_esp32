idf_component_register(SRCS "can_monitor.c"
                    INCLUDE_DIRS "include"
                    REQUIRES board esp_driver_twai freertos log)
