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
#include "print_job.h"

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
    print_process(band, sizeof(band));
}

/* Diagnostic: pattern the idle print buffer during scan pages; the first
 * damaged byte (offset, value) locates a writer that app_loop_count's canaries
 * below it cannot see. print_buf sits at bss+... directly above app_loop_count. */
void scratch_pattern_fill(void)
{
    if (print_active) return;
    for (unsigned i = 0; i < sizeof(print_buf); ++i) {
        if (!(i & 127u)) app_yield();
        print_buf[i] = (uint8_t)(0x5au ^ i);
    }
}
int scratch_pattern_check(uint32_t *value)
{
    if (print_active) return -1;
    for (unsigned i = 0; i < sizeof(print_buf); ++i) {
        if (!(i & 127u)) app_yield();
        if (print_buf[i] != (uint8_t)(0x5au ^ i)) {
            *value = 0; for (unsigned k = 0; k < 4u && i + k < sizeof(print_buf); ++k) *value |= (uint32_t)print_buf[i + k] << (8u * k);
            return (int)i;
        }
    }
    return -1;
}

const char *app_section = "main";
uint32_t app_loop_max_ticks, app_loop_count;   /* main-loop iteration profile (escl.c prints and resets it) */
/* Corruption canaries (2026-10-27: bss globals were overwritten during a long
 * colour page). Filled at boot; canary_check() reports the first damaged word. */
uint32_t canary_a[64], canary_b[64];
void canary_init(void) { for (unsigned i = 0; i < 64; ++i) { canary_a[i] = 0xc0ffee00u + i; canary_b[i] = 0xdeadbe00u + i; } }
int canary_check(uint32_t *value)
{
    for (unsigned i = 0; i < 64; ++i) {
        if (canary_a[i] != 0xc0ffee00u + i) { *value = canary_a[i]; return (int)i; }
        if (canary_b[i] != 0xdeadbe00u + i) { *value = canary_b[i]; return 64 + (int)i; }
    }
    return -1;
}

void app_yield(void)
{
    if (scanner_motor_busy()) scanner_motor_poll();
    scanner_line_poll();                 /* one IRQ19 event at most; cheap when idle */
}

void firmware_main(void)
{
    clock_init();
    panel_init();
    service_init(false);
    dbg_init();
    dbg_puts("\n[app] MG2550s open app\n");
    canary_init();
    exception_report();
    dbg_hex("app_flash", APP_FLASH);
    dbg_hex("app_ram", APP_RAM);

    /* The scanner's conversion rate needs I-cache even when an ordinary
     * eSCL client never sends the bench-only 0x8c command. DMA stays uncached;
     * page capture enables write-through caching of CPU-owned data. */
    cpu_icache_set(true);

    usb_init();
    power_init();
    dbg_puts("[app] usb up, entering main loop\n");
    if (hybrid_panel_available()) {
        buttons_select_map(2);
        dbg_puts("[btn] B/W=one stock paper advance; release between presses; Cancel=stop; no firing\n");
    } else dbg_puts("[btn] B/W=toggle feeder (LF reverse); Colour=toggle transport (LF forward); Cancel=stop\n");
    dbg_puts("[home] stock PI seek + verified backoff + repeat seek; mechanical reference\n");
    dbg_puts("[page] USB stop-and-capture page scan; returns home; Cancel=stop\n");
    dbg_puts("[escl] eSCL over IPP-USB (interfaces 2-3)\n");
    dbg_puts("[cart] USB 0x62 reads cartridge fuse ROM (read-only); 0x63 status\n");
    dbg_puts("[head] USB 0x81 stationary black prefire (stock ID24 list); 0x82 status\n");

    uint32_t loop_t0 = app_clock_ticks();
    for (;;) {
        { uint32_t t = app_clock_ticks(); if (t - loop_t0 > app_loop_max_ticks) app_loop_max_ticks = t - loop_t0; loop_t0 = t; ++app_loop_count; }
        if (!(app_loop_count & 63u)) heartbeat(app_loop_count, app_section);
        scanner_motor_poll();
        leds_tick();
        scanner_line_poll();
        usb_poll();
        ippusb_poll();
        app_section = "main";
        scanner_motor_poll();
        service_poll();
        buttons_poll();
        power_poll();                        /* ownership, idle timer, standby/wake; cheap */
        scan_page_poll();
        /* Long scanner captures and I2C retry loops cannot extend a jog. */
        if (!scanner_motor_busy()) {
            mailbox_poll();
            if (!scan_page_busy()) { scanner_poll(); cartridge_poll(); head_fire_poll(); print_mech_poll(); paper_feed_poll(); paper_manual_poll(); carriage_poll(); page_job_poll(); hybrid_poll(); print_engine_poll(); }
        }
    }
}
