idf_component_register(SRCS "uart_justfloat.c"
                    INCLUDE_DIRS "include"
                    REQUIRES board telemetry_mux esp_driver_uart esp_timer freertos log)
