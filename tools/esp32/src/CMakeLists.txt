idf_component_register(SRCS "main.c" "wiring.c" "sniff.c" "../../../bootloader/src/integrity.c"
                       INCLUDE_DIRS "../../../bootloader/include"
                       REQUIRES esp_driver_i2c esp_driver_gpio esp_driver_usb_serial_jtag esp_partition esp_timer)
