#include "service.h"

/* Reserved internal SRAM, below the stage-0 stack. A checked one-shot request
 * survives software reset; random cold SRAM is not trusted as a request. */
#define MAILBOX 0xe0000800u
#define COOKIE 0x4d474252u
static bool boot_mode, held, app_ok, reboot_pending, reboot_acknowledged;
static unsigned reboot_mode;
static uint32_t window_start;
static uint8_t observed_status;

static unsigned take_boot_request(void)
{
    uint32_t magic=rd(MAILBOX), mode=rd(MAILBOX+4);
    bool valid=magic==COOKIE && rd(MAILBOX+8)==~COOKIE && rd(MAILBOX+12)==~mode;
    wr(MAILBOX,0); wr(MAILBOX+8,0);
    return valid && mode<=REBOOT_APPLICATION?mode:REBOOT_NORMAL;
}

static __attribute__((noreturn)) void launch_application(void)
{
    usb_disconnect(); rescue_release_bus();
    delay_ticks(CLOCK_TICKS_PER_MS*100u); /* Host must observe distinct USB identities. */
    const uint8_t *image=(const uint8_t *)APP_FLASH;
    memcpy((void *)APP_RAM,image+APP_HEADER,get32(image+8));
    ((void (*)(void))APP_RAM)();
    for(;;) leds_tick();
}

void service_init(bool bootloader)
{
    boot_mode=bootloader;
    app_ok=app_valid((const uint8_t *)APP_FLASH,APP_CAPACITY);
    unsigned request=bootloader?take_boot_request():REBOOT_NORMAL;
    held=bootloader && (!app_ok || request==REBOOT_BOOTLOADER);
    reboot_pending=reboot_acknowledged=false;
    update_reset();
    observed_status=0x80;
    if(bootloader && app_ok && request==REBOOT_APPLICATION) launch_application();
}

bool service_is_bootloader(void) { return boot_mode; }
bool service_busy(void) { return update_owner()!=0; }
void service_hold(void) { if(boot_mode) held=true; }
bool service_stay(void)
{ if(!boot_mode) return false; held=true; return true; }

bool service_reboot(unsigned mode)
{
    if(mode>REBOOT_APPLICATION || service_busy() || !update_can_reboot()) return false;
    if(mode==REBOOT_APPLICATION && !app_valid((const uint8_t *)APP_FLASH,APP_CAPACITY)) return false;
    reboot_mode=mode; reboot_pending=true; reboot_acknowledged=false;
    return true;
}

void service_reboot_ack(void)
{ if(reboot_pending) reboot_acknowledged=true; }

unsigned service_info(uint8_t *data)
{
    uint8_t status=update_status();
    if(status!=observed_status && !service_busy()) {
        app_ok=app_valid((const uint8_t *)APP_FLASH,APP_CAPACITY);
        observed_status=status;
    }
    uint32_t elapsed=time_ms()-window_start;
    uint32_t remaining=boot_mode && !held && elapsed<BOOT_WINDOW_MS?BOOT_WINDOW_MS-elapsed:0;
    put32(data,0x5746474du); /* MGFW */
    put32(data+4,APP_VERSION); put32(data+8,boot_mode?1:2);
    unsigned capabilities=update_can_reboot()?0:16u;
#ifndef APPLICATION_BUILD
    capabilities|=8u; /* Explicit whole-flash USB writer is available. */
#endif
    put32(data+12,(held?1u:0)|(service_busy()?2u:0)|(app_ok && !service_busy()?4u:0)|capabilities);
    put32(data+16,remaining); put32(data+20,status);
    put32(data+24,APP_FLASH); put32(data+28,APP_CAPACITY);
    return 32;
}

void service_poll(void)
{
    if(reboot_pending && reboot_acknowledged && !service_busy()) {
        wr(MAILBOX,0); wr(MAILBOX+4,reboot_mode);
        wr(MAILBOX+8,~COOKIE); wr(MAILBOX+12,~reboot_mode); wr(MAILBOX,COOKIE);
        usb_disconnect(); rescue_release_bus(); delay_ticks(CLOCK_TICKS_PER_MS*100u); cpu_reset();
    }
    if(boot_mode && !held && (uint32_t)(time_ms()-window_start)>=BOOT_WINDOW_MS) {
        if(!service_busy() && app_valid((const uint8_t *)APP_FLASH,APP_CAPACITY)) launch_application();
        held=true;
    }
}

void firmware_main(void)
{
    clock_init(); panel_init();
#ifdef APPLICATION_BUILD
    service_init(false);
#else
    service_init(true);
#endif
    usb_init();
    window_start=time_ms(); /* Five seconds from USB attachment, not RAM setup. */
    for(;;) {
        leds_tick(); usb_poll(); update_poll(); mailbox_poll(); service_poll();
    }
}
