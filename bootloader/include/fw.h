#ifndef OPENFW_H
#define OPENFW_H
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#define BOOT __attribute__((section(".boot.text"), noinline))
#define BOOTDATA __attribute__((section(".boot.rodata")))
#define ALIGNED __attribute__((aligned(32)))
#define FLASH_SIZE 0x200000u
#define STAGING ((uint8_t *)0x20200000u)
#define REG(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define USB 0xc0780000u
#define PANEL 0xc0c8d000u
#define TIMER 0xc0140100u
#define SPI 0xc0aa0000u
#define DMA 0xc0a90000u
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))

static inline __attribute__((always_inline)) void wr(uint32_t a, uint32_t v) { REG(a)=v; }
static inline __attribute__((always_inline)) uint32_t rd(uint32_t a) { return REG(a); }
static inline __attribute__((always_inline)) void modify(uint32_t a, uint32_t clear, uint32_t set)
{ wr(a, (rd(a) & ~clear) | set); }

void boot_c(void);
void firmware_main(void) __attribute__((noreturn));
void cpu_reset(void) __attribute__((noreturn));
void panel_init(void);
void leds_tick(void);
void delay_ticks(uint32_t ticks);
void usb_init(void);
void usb_poll(void);
void usb_disconnect(void);
void update_reset(void);
void update_feed(const uint8_t *data, size_t length);
void update_poll(void);
void update_status_ack(void);
uint8_t update_status(void);
bool update_receiving(void);
bool flash_identify(void);
bool flash_erase(uint32_t address);
bool flash_program(uint32_t address, const uint8_t *data);
void *memcpy(void *to, const void *from, size_t n);
void *memset(void *to, int value, size_t n);
size_t strlen(const char *text);
#endif
