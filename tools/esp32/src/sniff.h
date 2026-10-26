#ifndef BRIDGE_SNIFF_H
#define BRIDGE_SNIFF_H
#include <stdbool.h>
#include "driver/gpio.h"
bool sniff_start(gpio_num_t sda, gpio_num_t scl, void (*say)(const char *));
void sniff_poll(void);
void sniff_stop(void);
bool sniff_active(void);
#endif
