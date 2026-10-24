/* Dry-run page job: everything a print needs except firing the head.
 *
 * Sequence (non-blocking state machine over the carriage and paper-feed
 * modules): home the carriage if it has no reference, pick and hold a sheet,
 * then for each band sweep the carriage between the two print margins
 * (alternating direction), advance the paper by the band height, and finally
 * eject and re-park. Any sub-module failure, Cancel or USB stop aborts with the
 * motors off. Bands, band advance (LF counts), sweep margins and cruise speed
 * come from the request. Used to measure page timing and to exercise the
 * mechanical loop before the head-drive contract is available. */
#include "app.h"

#define SWEEP_LEFT_DEFAULT 100000u    /* counts from the park wall */
#define SWEEP_RIGHT_DEFAULT 8000u
#define BAND_ADVANCE_DEFAULT 2000u    /* LF counts per band */
#define CRUISE_DEFAULT 40000u
#define MAX_BANDS 64u

enum pj_state { PJ_IDLE = 0, PJ_HOME_CR, PJ_FEED, PJ_SWEEP, PJ_ADVANCE, PJ_EJECT, PJ_PARK };
enum pj_result { PJR_NONE = 0, PJR_RUNNING, PJR_OK, PJR_FEED_FAILED, PJR_CARRIAGE_FAILED, PJR_CANCELLED };

static enum pj_state state;
static enum pj_result result;
static unsigned bands, band, dir_left;
static uint32_t band_advance, sweep_left, sweep_right, cruise, t_start, t_band0, sweep_ms_total;
static bool started;                 /* sub-request issued for the current state */

static void finish(enum pj_result r)
{
    result = r; state = PJ_IDLE;
    dbg_puts("[page] done result="); dbg_dec(r); dbg_puts(" bands="); dbg_dec(band);
    dbg_puts(" ms="); dbg_dec((app_clock_ticks() - t_start) / CLOCK_TICKS_PER_MS);
    dbg_puts(" sweep_ms="); dbg_dec(sweep_ms_total); dbg_putc('\n');
}

bool page_job_busy(void) { return state != PJ_IDLE; }

bool page_job_request(unsigned n_bands, uint32_t advance, uint32_t left, uint32_t right, uint32_t speed)
{
    if (state != PJ_IDLE || !n_bands || n_bands > MAX_BANDS) return false;
    if (carriage_busy() || paper_feed_busy() || scanner_motor_busy() || scan_page_busy() || print_mech_lf_busy()) return false;
    bands = n_bands; band = 0; dir_left = 1;
    band_advance = advance ? advance : BAND_ADVANCE_DEFAULT;
    sweep_left = left ? left : SWEEP_LEFT_DEFAULT; sweep_right = right ? right : SWEEP_RIGHT_DEFAULT;
    if (sweep_left <= sweep_right || sweep_left > 116736u) return false;
    cruise = speed ? speed : CRUISE_DEFAULT;
    result = PJR_RUNNING; t_start = app_clock_ticks(); sweep_ms_total = 0; started = false;
    state = carriage_referenced() ? PJ_FEED : PJ_HOME_CR;
    dbg_puts("[page] dry run bands="); dbg_dec(bands); dbg_puts(" advance="); dbg_dec(band_advance);
    dbg_puts(" sweep "); dbg_dec(sweep_right); dbg_puts(".."); dbg_dec(sweep_left); dbg_puts(" cruise="); dbg_dec(cruise); dbg_putc('\n');
    return true;
}

void page_job_cancel(void)
{
    if (state == PJ_IDLE) return;
    carriage_cancel(); paper_feed_cancel();
    finish(PJR_CANCELLED);
}

static void next(enum pj_state s) { state = s; started = false; }

void page_job_poll(void)
{
    if (state == PJ_IDLE) return;
    switch (state) {
    case PJ_HOME_CR:
        if (!started) { if (!carriage_home_request()) { finish(PJR_CARRIAGE_FAILED); return; } started = true; return; }
        if (carriage_busy()) return;
        if (!carriage_referenced()) { finish(PJR_CARRIAGE_FAILED); return; }
        next(PJ_FEED); break;
    case PJ_FEED:
        if (!started) { if (!paper_feed_request(1u, 0)) { finish(PJR_FEED_FAILED); return; } started = true; return; }
        if (paper_feed_busy()) return;
        if (paper_feed_result() != 2u) { finish(PJR_FEED_FAILED); return; }
        next(PJ_SWEEP); break;
    case PJ_SWEEP:
        if (!started) {
            t_band0 = app_clock_ticks();
            if (!carriage_move_request((int32_t)(dir_left ? sweep_left : sweep_right), cruise)) { finish(PJR_CARRIAGE_FAILED); return; }
            started = true; return;
        }
        if (carriage_busy()) return;
        sweep_ms_total += (app_clock_ticks() - t_band0) / CLOCK_TICKS_PER_MS;
        ++band; dir_left ^= 1u;
        dbg_puts("[page] band "); dbg_dec(band); dbg_puts(" x="); dbg_dec((uint32_t)carriage_position()); dbg_putc('\n');
        next(band < bands ? PJ_ADVANCE : PJ_EJECT); break;
    case PJ_ADVANCE:
        if (!started) { if (!paper_feed_request(3u, (band_advance + 63u) / 64u)) { finish(PJR_FEED_FAILED); return; } started = true; return; }
        if (paper_feed_busy()) return;
        if (paper_feed_result() != 2u) { finish(PJR_FEED_FAILED); return; }
        next(PJ_SWEEP); break;
    case PJ_EJECT:
        if (!started) { if (!paper_feed_request(2u, 0)) { finish(PJR_FEED_FAILED); return; } started = true; return; }
        if (paper_feed_busy()) return;
        if (paper_feed_result() != 2u) { finish(PJR_FEED_FAILED); return; }
        next(PJ_PARK); break;
    case PJ_PARK:
        if (!started) { if (!carriage_home_request()) { finish(PJR_CARRIAGE_FAILED); return; } started = true; return; }
        if (carriage_busy()) return;
        finish(PJR_OK); break;
    default: finish(PJR_CANCELLED);
    }
}

/* Status: 10 little-endian words. */
unsigned page_job_status(uint8_t *out)
{
    put32(out, 0x4a50474du);            /* MGPJ */
    put32(out + 4, 1);
    put32(out + 8, state);
    put32(out + 12, result);
    put32(out + 16, band);
    put32(out + 20, bands);
    put32(out + 24, (uint32_t)carriage_position());
    put32(out + 28, state == PJ_IDLE ? 0 : (app_clock_ticks() - t_start) / CLOCK_TICKS_PER_MS);
    put32(out + 32, sweep_ms_total);
    put32(out + 36, band_advance);
    return 40;
}
