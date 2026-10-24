/* Lean management state for the application.
 *
 * The app does not accept firmware uploads itself: fwctl reboots it into the
 * bootloader (rescue) first, and the bootloader does the programming. So the
 * only management the app must honour is INFO (so fwctl recognises it) and
 * REBOOT (the cooperative entry into rescue). That also leaves the bulk USB
 * endpoints free for the scanner protocol later.
 *
 * The reboot mailbox format matches the bootloader's take_boot_request(): a
 * cookie-guarded one-shot word in reserved internal SRAM that survives the
 * software reset. */
#include "app.h"

#define MAILBOX 0xe0000800u
#define COOKIE 0x4d474252u
static bool reboot_pending, reboot_acknowledged;
static bool app_ok;
static unsigned reboot_mode;

void service_init(bool bootloader)
{
    (void)bootloader;                 /* always the application in this build */
    reboot_pending = reboot_acknowledged = false;
    app_ok = app_valid((const uint8_t *)APP_FLASH, APP_CAPACITY);
}

bool service_is_bootloader(void) { return false; }
bool service_busy(void) { return scanner_motor_busy() || scan_page_busy() || paper_feed_busy() || carriage_busy() || page_job_busy(); }
void service_hold(void) {}
bool service_stay(void) { return false; } /* only the bootloader can hold rescue */

bool service_reboot(unsigned mode)
{
    if (mode > REBOOT_APPLICATION) return false;
    page_job_cancel(); scan_page_cancel(); scanner_lamp_off(); paper_feed_cancel(); carriage_cancel();
    scanner_motor_shutdown();
    reboot_mode = mode; reboot_pending = true; reboot_acknowledged = false;
    return true;
}

void service_reboot_ack(void)
{
    if (reboot_pending) reboot_acknowledged = true;
}

unsigned service_info(uint8_t *data)
{
    put32(data, 0x5746474du);                 /* MGFW */
    put32(data + 4, APP_VERSION);
    put32(data + 8, 2);                        /* mode: application */
    put32(data + 12, app_ok ? 4u : 0u);        /* cached: app cannot write flash */
    put32(data + 16, 0);                       /* no boot window in the app */
    put32(data + 20, 0x80);                    /* status: ready */
    put32(data + 24, APP_FLASH);
    put32(data + 28, APP_CAPACITY);
    return 32;
}

void service_poll(void)
{
    if (reboot_pending && reboot_acknowledged) {
        wr(MAILBOX, 0); wr(MAILBOX + 4, reboot_mode);
        wr(MAILBOX + 8, ~COOKIE); wr(MAILBOX + 12, ~reboot_mode); wr(MAILBOX, COOKIE);
        usb_disconnect(); rescue_release_bus();
        delay_ticks(CLOCK_TICKS_PER_MS * 100u); cpu_reset();
    }
}
