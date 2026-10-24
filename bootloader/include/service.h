#ifndef SERVICE_H
#define SERVICE_H
#include "fw.h"
#include "protocol.h"

#define BOOT_WINDOW_MS 5000u
#define CLOCK_TICKS_PER_MS 96000u
#define OWNER_USB 1u
#define OWNER_I2C 2u

void clock_init(void);
uint32_t time_ms(void);
void service_init(bool bootloader);
void service_poll(void);
bool service_stay(void);
bool service_reboot(unsigned mode);
void service_reboot_ack(void);
unsigned service_info(uint8_t *data);
bool service_is_bootloader(void);
bool service_busy(void);
void service_hold(void);
bool update_begin(uint32_t length,unsigned owner);
bool update_begin_dangerous(uint32_t length,unsigned owner);
bool update_can_reboot(void);
bool update_app_verified(void);
unsigned update_owner(void);
void update_abort(uint8_t status);
bool rescue_read(uint8_t *data);
bool rescue_write(const uint8_t *data,size_t length);
void rescue_release_bus(void);
void mailbox_poll(void);
#endif
