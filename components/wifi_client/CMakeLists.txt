idf_component_register(SRCS "wifi_client.c"
                    INCLUDE_DIRS "include"
                    REQUIRES board link esp_wifi esp_netif esp_event freertos log lwip)
