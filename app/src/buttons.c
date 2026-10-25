/* Keys are active from boot. Stock PANEL+0x28 bit assignments were physically
 * checked on 2026-10-23. Debounce at 1ms intervals; one action per press. */
#include "app.h"

#define KEY_REG (PANEL + 0x28u)
#define KEY_CANCEL 0x0020u
#define KEY_POWER 0x0040u          /* measured through the register tunnel on 2026-10-26 */
#define KEY_COPY_BW 0x0400u
#define KEY_COPY_CL 0x1000u
#define COPY_KEYS (KEY_COPY_BW | KEY_COPY_CL)
#define STABLE 3u

/* Button maps (USB 0x8b selects one). The owner drives bench tests from the
 * panel at their own pace; every action is logged with the feed counter and
 * the paper-end sensor so the log can be matched to what they saw.
 *   map 0 (default): B/W = feeder reverse toggle, Colour = transport forward
 *                    toggle (10 s auto-stop), Cancel = stop / lamp.
 *   map 1 (feed bench): B/W = pick up and place a sheet (hold, then advance
 *                    8000 counts); Colour = advance 1024 counts; Power =
 *                    carriage park <-> 60000 counts; Cancel = eject if a
 *                    sheet is held, else stop. */
static unsigned button_map;
static unsigned place_step;        /* feed bench: 0 idle, 1 hold requested, 2 advance requested */
static bool btn_log, snapshot, ready, cancel_consumed;
static uint32_t btn_last, btn_cand, next_sample;
static unsigned btn_cand_n;

bool buttons_select_map(unsigned map)
{
    if (map > 1u) return false;
    button_map = map; place_step = 0;
    dbg_puts("[btn] map "); dbg_dec(map); dbg_putc('\n');
    return true;
}

static void bench_log(const char *what)
{
    dbg_puts("[bench] "); dbg_puts(what); dbg_puts(" lf="); dbg_dec(rd(0xc0c53100u) & 0x7ffffu);
    dbg_puts(" pe="); dbg_dec(rd(0xc0c84e08u) & 1u); dbg_puts(" cr="); dbg_dec((uint32_t)carriage_position()); dbg_putc('\n');
}

/* Feed bench placement: hold, and when that completes, advance 8000 counts. */
static void bench_poll(void)
{
    if (place_step == 1u && !paper_feed_busy()) {
        if (paper_feed_result() == 2u && paper_feed_request(3u, 8000u / 64u)) { place_step = 2u; bench_log("place: advance 8000"); }
        else { place_step = 0; bench_log("place: hold failed"); }
    } else if (place_step == 2u && !paper_feed_busy()) {
        place_step = 0; bench_log(paper_feed_result() == 2u ? "placed" : "place: advance failed");
    }
}

static void bench_press(uint32_t pressed)
{
    bool busy = paper_feed_busy() || carriage_busy();
    if (pressed & KEY_COPY_BW) {
        if (!busy && paper_feed_request(1u, 8000u / 64u)) { place_step = 1u; bench_log("place: hold 8000"); } else bench_log("place: refused");
    } else if (pressed & KEY_COPY_CL) {
        if (!busy && paper_feed_request(3u, 1024u / 64u)) bench_log("advance 1024"); else bench_log("advance: refused");
    } else if (pressed & KEY_POWER) {
        if (busy) { bench_log("carriage: refused"); return; }
        if (!carriage_referenced() || carriage_position() > 30000) { if (carriage_home_request()) bench_log("carriage: park"); }
        else if (carriage_move_request(60000, 0)) bench_log("carriage: to 60000");
    } else if (pressed & KEY_CANCEL) {
        if (!busy && paper_feed_holding() && paper_feed_request(2u, 0)) { cancel_consumed = true; bench_log("eject"); }   /* the held key must not stop its own eject */
        else bench_log("cancel");
    }
}

void buttons_request(void)
{
    btn_log = snapshot = true;
    dbg_puts("[btn] probe enabled; Copy keys already active\n");
}

void buttons_poll(void)
{
    uint32_t now = time_ms();
    if ((int32_t)(now - next_sample) < 0) return;
    next_sample = now + 1u;
    uint32_t v = rd(KEY_REG);
    /* Stop on the first sample; starting motion requires debounce. */
    if ((scanner_motor_busy() || scan_page_busy() || paper_feed_busy() || carriage_busy() || page_job_busy()) &&
        (((v & KEY_CANCEL) && !cancel_consumed) || (v & COPY_KEYS) == COPY_KEYS)) {
        page_job_cancel();
        scan_page_cancel();
        scanner_motor_cancel();
        paper_feed_cancel();
        carriage_cancel();
        if (v & KEY_CANCEL) cancel_consumed = true;
    }
    if (!(v & KEY_CANCEL)) cancel_consumed = false;
    if (v == btn_cand) {
        if (btn_cand_n < STABLE) ++btn_cand_n;
    } else { btn_cand = v; btn_cand_n = 1u; }
    if (btn_cand_n < STABLE) return;
    /* A key held through startup is the baseline, not a new press. */
    if (button_map == 1u) bench_poll();
    uint32_t pressed = ready ? btn_cand & ~btn_last : 0u;
    bool changed = !ready || btn_last != btn_cand;
    ready = true; btn_last = btn_cand;
    if (btn_log && (changed || snapshot)) {
        dbg_puts("[btn] 028=0x"); dbg_u32(btn_cand); dbg_putc('\n');
        snapshot = false;
    }
    if (button_map == 1u) {
        bench_poll();
        if (pressed & (KEY_COPY_BW | KEY_COPY_CL | KEY_POWER | KEY_CANCEL)) {
            if ((pressed & KEY_CANCEL) && cancel_consumed) return;   /* the stop above already handled it */
            bench_press(pressed);
        }
        return;
    }
    if (pressed & KEY_CANCEL) {
        if (!cancel_consumed) scanner_lamp_toggle();
        return;
    }
    if (scan_page_busy() || carriage_busy() || page_job_busy() || (paper_feed_busy() && !paper_manual_running())) return;
    if ((btn_cand & KEY_CANCEL) || (btn_cand & COPY_KEYS) == COPY_KEYS) return;
    /* Bench only: Copy B/W toggles the paper motor in reverse (pickup/feeder), Copy
     * Colour toggles it forward (transport); 10 s auto-stop. Emulator uses USB 0x6b. */
    if (pressed & KEY_COPY_BW) (void)paper_manual_toggle(1u);
    else if (pressed & KEY_COPY_CL) (void)paper_manual_toggle(2u);
}
