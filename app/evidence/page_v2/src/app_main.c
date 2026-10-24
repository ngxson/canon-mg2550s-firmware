/* Application entry and main loop.
 *
 * The bootloader hands control to app_entry (entry.c) with the board, DRAM,
 * clocks and SPI already initialised, in ARM supervisor mode with IRQ/FIQ
 * masked and a valid stack. We re-init the free-running clock and panel (as
 * the demo does), bring up USB, then poll forever.
 *
 * Every iteration must keep management serviced so fwctl can always reboot us
 * into rescue: usb_poll() answers INFO/REBOOT and service_poll() performs the
 * reset once the control transfer is acknowledged. Feature work (buttons, the
 * raw-CIS scanner PoC) will add bounded *_tick() calls to this loop. */
#include "app.h"
#include "print.h"

/* Decode an embedded minimal BJL/BJRaster3 band through the open print receiver
 * and report the result over the debug log. Runs the same parser the (future)
 * USB print path will use — validated natively (test_print.c) and in emulation. */
static print_state_t print_st;
static uint8_t print_buf[8192];
static unsigned print_total, print_recv;
static bool print_active;

/* Decode a complete BJL band through the open receiver and emit the dots. */
static void print_process(const uint8_t *data, unsigned len)
{
    print_reset(&print_st);
    int r = print_decode(data, len, &print_st);
    dbg_puts("[print] r="); dbg_dec((unsigned)(r & 0xff));
    dbg_puts(" mandatory=0x"); dbg_u32(print_st.mandatory);
    dbg_puts(" planes="); dbg_dec(print_st.num_planes);
    dbg_puts(" rows="); dbg_dec(print_st.rows_decoded);
    dbg_puts(" rowbytes="); dbg_dec(print_st.row_bytes[0]);
    dbg_puts(" row0:");
    for (unsigned i = 0; i < print_st.row_bytes[0] && i < 16; ++i) {
        dbg_putc(' '); dbg_u32(print_st.row[0][i]);
    }
    dbg_putc('\n');
    /* Placeholder head-data emit: stream the decoded plane dots to the head-data
     * data port (0xc0c25000). The real nozzle mapping / timing / DMA protocol is
     * a TODO (needs RE of the 0xc0c2xxxx head driver); this validates the
     * decode -> head-output path end-to-end in emulation (PrinterModel observes),
     * and is harmless on hardware (the head is unconnected). */
    for (unsigned pl = 0; pl < print_st.num_planes; ++pl)
        for (unsigned i = 0; i < print_st.row_bytes[pl]; ++i)
            wr(0xc0c25000u, print_st.row[pl][i]);
    dbg_puts("[print] emitted head bytes: "); dbg_dec(print_st.row_bytes[0]); dbg_putc('\n');
}

/* USB bulk print transport: PRINT_BEGIN sets the expected total; bulk data on the
 * printer-class OUT endpoint accumulates; the band is processed once complete. */
bool print_is_active(void) { return print_active; }
void print_bulk_begin(unsigned total)
{
    if (total > sizeof(print_buf)) total = sizeof(print_buf);
    print_total = total; print_recv = 0; print_active = (total != 0);
    dbg_puts("[print] bulk begin total="); dbg_dec(total); dbg_putc('\n');
}
void print_bulk_feed(const uint8_t *data, unsigned len)
{
    if (!print_active) return;
    for (unsigned i = 0; i < len && print_recv < sizeof(print_buf); ++i)
        print_buf[print_recv++] = data[i];
    if (print_recv >= print_total) { print_active = false; print_process(print_buf, print_recv); }
}

void print_selftest(void)
{
    /* I J b c d l p t (8 mandatory) + L "K" + F(5xFF,5x00 PackBits) + ESC@ */
    static const uint8_t band[] = {
        0x1b,0x28,0x49,0x01,0x00, 0x01,          /* ESC(I 01 */
        0x1b,0x28,0x4a,0x01,0x00, 0x01,          /* ESC(J 01 (1 row/plane) */
        0x1b,0x28,0x62,0x01,0x00, 0x01,          /* ESC(b 01 */
        0x1b,0x28,0x63,0x00,0x00,                /* ESC(c */
        0x1b,0x28,0x64,0x00,0x00,                /* ESC(d */
        0x1b,0x28,0x6c,0x00,0x00,                /* ESC(l */
        0x1b,0x28,0x70,0x00,0x00,                /* ESC(p */
        0x1b,0x28,0x74,0x00,0x00,                /* ESC(t */
        0x1b,0x28,0x4c,0x01,0x00, 0x4b,          /* ESC(L "K" */
        0x1b,0x28,0x46,0x05,0x00, 0xfc,0xff,0xfc,0x00,0x80, /* ESC(F raster */
        0x1b,0x40,                               /* ESC@ */
    };
    print_reset(&print_st);
    int r = print_decode(band, sizeof(band), &print_st);
    dbg_puts("[print] r="); dbg_dec((unsigned)(r & 0xff));
    dbg_puts(" mandatory=0x"); dbg_u32(print_st.mandatory);
    dbg_puts(" planes="); dbg_dec(print_st.num_planes);
    dbg_puts(" rows="); dbg_dec(print_st.rows_decoded);
    dbg_puts(" rowbytes="); dbg_dec(print_st.row_bytes[0]);
    dbg_puts(" row0:");
    for (unsigned i = 0; i < print_st.row_bytes[0] && i < 16; ++i) {
        dbg_putc(' '); dbg_u32(print_st.row[0][i]);
    }
    dbg_putc('\n');

    /* Placeholder head-data emit: stream the decoded plane dots to the head-data
     * data port (0xc0c25000). The real nozzle mapping / timing / DMA protocol is
     * a TODO (needs RE of the 0xc0c2xxxx head driver); this validates the
     * decode -> head-output path end-to-end in emulation (PrinterModel observes
     * these writes), and is harmless on hardware (the head is unconnected). */
    for (unsigned pl = 0; pl < print_st.num_planes; ++pl)
        for (unsigned i = 0; i < print_st.row_bytes[pl]; ++i)
            wr(0xc0c25000u, print_st.row[pl][i]);
    dbg_puts("[print] emitted head bytes: "); dbg_dec(print_st.row_bytes[0]); dbg_putc('\n');
}

void firmware_main(void)
{
    clock_init();
    panel_init();
    service_init(false);
    dbg_init();
    dbg_puts("\n[app] MG2550s open app\n");
    dbg_hex("app_flash", APP_FLASH);
    dbg_hex("app_ram", APP_RAM);

    usb_init();
    dbg_puts("[app] usb up, entering main loop\n");
    dbg_puts("[btn] B/W=short forward jog; Colour=home; Cancel=stop\n");
    dbg_puts("[home] stock PI seek + verified backoff + repeat seek; mechanical reference\n");
    dbg_puts("[page] USB stop-and-capture page scan; returns home; Cancel=stop\n");

    scanner_request();                 /* one peek at boot; re-trigger via USB 0x51 */

    for (;;) {
        scanner_motor_poll();
        leds_tick();
        usb_poll();
        scanner_motor_poll();
        service_poll();
        buttons_poll();
        scan_page_poll();
        /* Long scanner captures and I2C retry loops cannot extend a jog. */
        if (!scanner_motor_busy()) {
            mailbox_poll();
            if (!scan_page_busy()) scanner_poll();
        }
    }
}
