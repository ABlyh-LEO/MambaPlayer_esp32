idf_component_register(SRCS "mamba_player.c"
                    INCLUDE_DIRS "."
                    REQUIRES board storage battery audio can_monitor alarm link wifi_client uart_justfloat telemetry_mux)
