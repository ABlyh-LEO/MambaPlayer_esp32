idf_component_register(SRCS "alarm.c"
                    INCLUDE_DIRS "include"
                    REQUIRES board battery audio freertos log)
