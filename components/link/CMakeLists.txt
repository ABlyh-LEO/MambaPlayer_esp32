idf_component_register(SRCS "link.c"
                    INCLUDE_DIRS "include"
                    REQUIRES board storage battery audio can_monitor alarm esp_driver_usb_serial_jtag freertos log esp_timer lwip)
