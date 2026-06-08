idf_component_register(SRCS "storage.c"
                    INCLUDE_DIRS "include"
                    REQUIRES board nvs_flash spiffs vfs)
