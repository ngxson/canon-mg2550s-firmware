/* Keys are active from boot. Stock PANEL+0x28 bit assignments were physically
 * checked on 2026-10-23. Debounce at 1ms intervals; one action per press. */
#include "app.h"

#define KEY_REG (PANEL + 0x28u)
#define KEY_CANCEL 0x0020u
#define KEY_COPY_BW 0x0400u
#define KEY_COPY_CL 0x1000u
#define COPY_KEYS (KEY_COPY_BW | KEY_COPY_CL)
#define STABLE 3u

static bool btn_log, snapshot, ready, cancel_consumed;
static uint32_t btn_last, btn_cand, next_sample;
static unsigned btn_cand_n;

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
        ((v & KEY_CANCEL) || (v & COPY_KEYS) == COPY_KEYS)) {
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
    uint32_t pressed = ready ? btn_cand & ~btn_last : 0u;
    bool changed = !ready || btn_last != btn_cand;
    ready = true; btn_last = btn_cand;
    if (btn_log && (changed || snapshot)) {
        dbg_puts("[btn] 028=0x"); dbg_u32(btn_cand); dbg_putc('\n');
        snapshot = false;
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
