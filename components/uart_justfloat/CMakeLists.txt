idf_component_register(SRCS "uart_justfloat.c"
                    INCLUDE_DIRS "include"
                    REQUIRES board link esp_driver_uart freertos log)
