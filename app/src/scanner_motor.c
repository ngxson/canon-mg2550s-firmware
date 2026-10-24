/* Mechanical homing: stock seek controller, verified backoff, repeat seek.
 * Stock f4c56/f8682/f873a/f8a5c are the source evidence. The repeat seek and
 * absolute deadlines are replacement-firmware guards. Optical strip calibration
 * is separate and is NOT claimed by the mechanical-reference status.
 */
#include "app.h"
#include "motor_control.h"

#define P 0xc0c57000u
#define C 0xc0c56000u
#define T 0xc0140400u
#define COUNTER 0xc0c82008u
#define NEUTRAL 2000u
#define SEEK_SPAN 529134u
#define BACKOFF_SPAN 10582u
#define MIN_BACKOFF 3023u
#define REPEAT_TOLERANCE 3000u
#define COOLDOWN_MS 300u

enum phase { IDLE, WANT_JOG, WANT_HOME, JOG, SEEK, BACKOFF, RETURN, SETTLE,
             PAGE_MOVE, WANT_PAGE_MOVE, SCAN_MOVE, WANT_SCAN_MOVE, FAST_RETURN };
/* Fast return (experiment, vendor 0x7e): when a mechanical reference already
 * exists, a homing request first travels backward with the full backoff-style
 * drive authority to FAST_MARGIN counts before the known home, slowing over
 * the last FAST_DECEL counts, and only then runs the stock seek into the stop.
 * The stock seek's command clamp (u >= 3500) limits it to ~52,000 counts/s
 * and would also hit the stop at that speed. */
#define FAST_MARGIN 24000u
#define FAST_DECEL 16000u
#define FAST_MIN 8000u
/* Continuous scan travel: forward like the verified backoff (same ramp and PI
 * coefficients), cruise capped at scan_speed, then the backoff's 4000 counts/s
 * approach for the last 3000 counts before the hardware endpoint. Stock's own
 * scan endpoint lies ~469,000 counts from its origin; we stay within 458,000. */
#define SCAN_TRAVEL_LIMIT 458000u
#define SCAN_APPROACH 3000u
enum home_result { UNREFERENCED, HOMING, MECHANICAL_REFERENCE, HOME_FAILED, HOME_CANCELLED };
static enum phase phase, after_settle;
static enum home_result home_result;
static bool initialized, inhibited, cooling, home_power_owned;
static uint32_t saved_scanner_power;
static unsigned direction, last_ms, window_ms, same_ms;
static uint32_t started, x0, target, span, limit_ms, cool_until, settle_at;
static uint32_t first_stop, backoff_end, home_position, last_position;
static int32_t window_progress, measured_speed, qualified_progress;
static struct motor_control control;
static uint32_t page_target, scan_target, scan_speed, scan_limit_ms;
static bool move_ok;
/* Fast-return cruise (counts/s); 0 = classic homing only. 100,000 was run on
 * hardware (2026-10-24): ~45 % drive authority, full-page return 19.9 -> 9.5 s. */
#define RETURN_SPEED_MAX 120000u
#define RETURN_SPEED_DEFAULT 100000u
static uint32_t return_speed = RETURN_SPEED_DEFAULT;
static bool fast_return;                    /* this homing may start with a fast return */

bool scanner_motor_set_return_speed(unsigned counts_per_s)
{
    if ((counts_per_s && counts_per_s < 20000u) || counts_per_s > RETURN_SPEED_MAX || scanner_motor_busy()) return false;
    return_speed = counts_per_s;
    dbg_puts("[home] return speed "); dbg_dec(return_speed); dbg_putc('\n');
    return true;
}

static bool driving(void) { return (phase >= JOG && phase <= RETURN) || phase == PAGE_MOVE || phase == SCAN_MOVE || phase == FAST_RETURN; }
static bool homing(void) { return home_result == HOMING; }
bool scanner_motor_busy(void) { return phase != IDLE; }
unsigned scanner_home_result(void) { return home_result; }
uint32_t scanner_motor_position(void) { return rd(P + 0x100u); }
uint32_t scanner_motor_reference(void) { return home_position; }
bool scanner_motor_move_ok(void) { return move_ok; }

/* Internal page steps reuse the verified positive-backoff controller. Absolute
 * endpoints avoid accumulating per-step coast. Only the page sequencer owns
 * this path; each step is at most 1024 counts and has a 750 ms deadline. */
bool scanner_motor_page_move(uint32_t position)
{
    uint32_t current = scanner_motor_position();
    if (!scan_page_busy() || inhibited || scanner_motor_busy() || print_is_active() ||
        home_result != MECHANICAL_REFERENCE || position <= current ||
        position - current > 1024u || position < home_position ||
        position - home_position > 450048u) return false;
    page_target = position; move_ok = false; phase = WANT_PAGE_MOVE;
    return true;
}

bool scanner_motor_at_reference(void)
{
    uint32_t x = scanner_motor_position();
    return !scanner_motor_busy() && !inhibited && home_result == MECHANICAL_REFERENCE &&
           x + 256u >= home_position && x <= home_position + 256u;
}

/* Continuous scan travel to an absolute endpoint at a capped cruise speed. */
bool scanner_motor_scan_move(uint32_t endpoint, uint32_t speed)
{
    uint32_t current = scanner_motor_position();
    if (!scan_page_busy() || inhibited || scanner_motor_busy() || print_is_active() ||
        home_result != MECHANICAL_REFERENCE || endpoint <= current ||
        endpoint - current < 2u * SCAN_APPROACH || endpoint < home_position ||
        endpoint - home_position > SCAN_TRAVEL_LIMIT || speed < 1200u || speed > 60000u) return false;
    scan_target = endpoint; scan_speed = speed; move_ok = false;
    /* Generous deadline: cruise time, slow approach, ramp and margin. */
    scan_limit_ms = motor_divide((uint64_t)(endpoint - current) * 1000u, speed) +
                    SCAN_APPROACH * 1000u / 4000u + 2000u;
    phase = WANT_SCAN_MOVE;
    return true;
}

static void stop_outputs(void)
{
    wr(C, 0u); wr(C + 8u, NEUTRAL); wr(P + 0x11cu, 0u);
    wr(P + 0x124u, 0u); wr(C + 0x100u, 0u); wr(P + 0x400u, 0u);
    wr(T, 0x80000000u);
}

static void finish(const char *why, enum home_result result)
{
    uint32_t compare = driving() ? rd(C + 8u) : NEUTRAL;
    uint32_t elapsed = driving() ? (app_clock_ticks() - started) / CLOCK_TICKS_PER_MS : 0;
    stop_outputs();
    if (home_power_owned) {
        wr(0xc0c85614u, saved_scanner_power); home_power_owned = false;
    }
    dbg_puts(homing() ? "[home] stop: " : "[motor] stop: "); dbg_puts(why);
    dbg_puts(" pos="); dbg_u32(rd(P + 0x100u));
    dbg_puts(" elapsed_ms="); dbg_dec(elapsed);
    dbg_puts(" compare="); dbg_dec(compare); dbg_putc('\n');
    if (homing()) home_result = result;
    phase = IDLE; cooling = true; cool_until = time_ms() + COOLDOWN_MS;
}

void scanner_motor_cancel(void)
{
    if (scanner_motor_busy()) finish("cancelled", HOME_CANCELLED);
}
void scanner_motor_shutdown(void)
{
    inhibited = true;
    if (scanner_motor_busy()) finish("reboot", HOME_CANCELLED);
    else if (initialized) stop_outputs();
}
static bool available(void)
{
    if (inhibited || print_is_active()) return false;
    if (scanner_motor_busy()) { finish("overlapping request", HOME_CANCELLED); return false; }
    if (cooling && (int32_t)(time_ms() - cool_until) < 0) return false;
    cooling = false; return true;
}
bool scanner_motor_request(unsigned dir)
{
    if (dir > 1u || !available()) return false;
    direction = dir; phase = WANT_JOG; return true;
}
bool scanner_home_request(void)
{
    if (!available()) return false;
    fast_return = return_speed && home_result == MECHANICAL_REFERENCE && initialized;
    home_result = HOMING; phase = WANT_HOME; return true;
}
unsigned scanner_home_status(uint8_t *out)
{
    /* Existing 32-byte prefix is unchanged; a 48-byte read adds clock data. */
    uint32_t words[12] = {0x4d48474du, 1u, home_result, phase,
        rd(P + 0x100u), home_position, first_stop, backoff_end,
        rd(COUNTER), app_clock_ticks(), rd(0xc0c82010u), rd(0xc0c8200cu)};
    memcpy(out, words, sizeof(words)); return sizeof(words);
}

static bool serial_send(uint32_t word)
{
    wr(0xc0c50204u, word); wr(0xc0c50210u, 0xffffffffu);
    uint32_t start = app_clock_ticks();
    for (unsigned budget = 0; budget < 50000u; ++budget) {
        if (!(rd(0xc0c50218u) & 1u)) return true;
        if ((uint32_t)(app_clock_ticks() - start) >= 2u * CLOCK_TICKS_PER_MS) break;
    }
    dbg_puts("[motor] serial timeout word="); dbg_u32(word); dbg_putc('\n');
    return false;
}

static void route(const uint32_t values[7])
{
    for (unsigned i = 0; i < 7u; ++i) wr(0xc0c41400u + 4u*i, values[i]);
}

static bool initialize(void)
{
    static const uint32_t transaction[7] = {0, 1, 2, 6, 7, 11, 3};
    static const uint32_t motor[7] = {0, 4, 5, 6, 7, 11, 10};
    static const uint32_t commands[] = {
        0x8000000bu, 0x8000000au, 0x80000009u, 0x80000008u,
        0x8000400fu, 0x8000400fu, 0x022au, 0xc4c0u, 0xc4c0u, 0xc4c0u,
    };
    if (rd(0xc0c50000u) || rd(0xc0c52000u)) {
        dbg_puts("[motor] shared setup refused: another motor active\n"); return false;
    }
    /* Setup waits are bounded and happen with ALL motor outputs disabled.
     * Shared configuration must not energize the print-carriage/feed motors. */
    wr(0xc0c41008u, 0u);
    for (unsigned i = 0; i < 3u; ++i) {
        uint32_t base = i == 0 ? 0xc0c50000u : i == 1 ? 0xc0c52000u : C;
        wr(base, 0u); wr(base + 4u, 4000u); wr(base + 8u, NEUTRAL);
    }
    wr(0xc0c41004u, 0u);
    route(transaction);
    delay_ticks(CLOCK_TICKS_PER_MS);
    wr(0xc0c41008u, 1u); delay_ticks(CLOCK_TICKS_PER_MS);
    wr(0xc0c41008u, 0u);
    wr(0xc0c50200u, 0xf07u); wr(0xc0c50208u, 3u);
    for (unsigned i = 0; i < ARRAY_SIZE(commands); ++i) {
        if (!serial_send(commands[i])) {
            route(motor); wr(0xc0c41008u, 0u);
            return false;
        }
    }
    route(motor);                           /* stock 116c9c, before enable */
    wr(0xc0c85604u, 1u);                   /* retained from physical bring-up */
    wr(0xc0c41008u, 1u);
    modify(0xc0c51000u, 0u, 0x20u);
    modify(0xc0c53000u, 0u, 0x20u);
    modify(P, 0u, 0x20u);
    wr(0xc0c85624u, 0u);
    delay_ticks(CLOCK_TICKS_PER_MS);        /* settle choice, not stock timing */
    wr(0xc0c81090u, 0x3fu);                 /* stock 1166a6; pin role unknown */
    modify(0xc0c51000u, 0x20u, 0u);
    modify(0xc0c53000u, 0x20u, 0u);
    modify(P, 0x20u, 0u);

    wr(P + 0x12cu, 0u); wr(P, 0xa00du); wr(P + 0x304u, 0u);
    wr(C, 0u); wr(C + 4u, 4000u); wr(C + 8u, NEUTRAL);
    wr(P + 0x100u, 0x700000u);              /* relative origin, NOT home */
    wr(P + 0x200u, 0xffffffu);
    wr(P + 0x108u, 0u); wr(P + 0x10cu, 0u);
    wr(C + 0x100u, 0u); wr(P + 0x400u, 0u);
    wr(C + 0x104u, 0u); wr(P + 0x404u, 0u); wr(P + 0x11cu, 0u);
    wr(C + 0x10u, NEUTRAL); wr(P + 0x120u, 0u); wr(C + 0x14u, 0u);
    wr(P + 0x220u, 0u);
    wr(P + 0x204u, 0xffffffu); wr(P + 0x208u, 0xffffffu); wr(P + 0x20cu, 0xffffffu);
    wr(P + 0x124u, 0u); wr(P + 0x128u, 0u); wr(C + 0x24u, 0u);
    wr(P + 4u, 0u); wr(P + 8u, 0x78u); wr(P + 0x12cu, 0u);
    wr(P + 0x148u, 1u); wr(P + 0x308u, 1u); wr(P + 0x140u, 0u);
    /* Match the stock home speed's period-measurement scale (shift=2,N=1). */
    modify(P, 0xc000u, 0u);                 /* 0xa00d -> 0x200d */
    static const uint32_t diagnostics[] = {
        0xc0c41404u, 0xc0c41408u, 0xc0c41418u, 0xc0c81090u,
        P, P + 0x100u, P + 0x148u, P + 0x308u,
    };
    for (unsigned i = 0; i < ARRAY_SIZE(diagnostics); ++i) {
        dbg_puts("[motor] setup "); dbg_u32(diagnostics[i]);
        dbg_putc('='); dbg_u32(rd(diagnostics[i])); dbg_putc('\n');
    }
    initialized = true;
    return true;
}

static bool start_move(enum phase next)
{
    stop_outputs();
    phase = next;
    if (next != JOG) direction = (next == BACKOFF || next == PAGE_MOVE || next == SCAN_MOVE) ? 0u : 1u;
    if (next == FAST_RETURN) direction = 1u;
    span = next == SEEK ? SEEK_SPAN : next == BACKOFF ? BACKOFF_SPAN :
           next == RETURN ? BACKOFF_SPAN + REPEAT_TOLERANCE : 512u;
    limit_ms = next == SEEK ? 22000u : next == JOG ? 40u : 2500u;
    x0 = rd(P + 0x100u);
    if (next == PAGE_MOVE) { span = page_target - x0; limit_ms = 750u; }
    if (next == SCAN_MOVE) { span = scan_target - x0; limit_ms = scan_limit_ms; }
    if (next == FAST_RETURN) {
        span = x0 - (home_position + FAST_MARGIN);
        limit_ms = motor_divide((uint64_t)span * 1000u, return_speed) + FAST_DECEL * 1000u / 4000u + 3000u;
    }
    if (x0 <= span || x0 >= 0xffffffu - span) {
        finish("position range invalid", HOME_FAILED); return false;
    }
    target = direction ? x0 - span : x0 + span;
    wr(P + 0x200u, 0xffffffu); wr(P + 0x204u, 0xffffffu);
    wr(P + 0x208u, 0xffffffu); wr(P + 0x20cu, 0xffffffu);
    wr(C + 8u, NEUTRAL); wr(P + 0x200u, 0xffffffu);
    wr(P + 0x160u, 1u); wr(P + 0x164u, x0);
    wr(P + 0x124u, 0u); wr(P + 0x11cu, 0u);
    wr(P + 0x120u, target); wr(C + 0x10u, NEUTRAL);
    wr(P + 0x128u, direction ? target - 1u : target + 1u);
    wr(C + 0x28u, 0u); wr(C + 0x1cu, 0u); wr(P + 0x124u, 1u);
    wr(C + 0x18u, 0u); wr(C + 0x0cu, 0u); wr(P + 0x11cu, 1u);
    wr(P + 0x130u, 0u); wr(P + 0x12cu, 0u);
    wr(C + 0x100u, 0u); wr(C + 0x104u, 0u); wr(C + 0x100u, 1u);
    motor_control_reset(&control);
    last_ms = same_ms = 0; window_ms = next == BACKOFF || next == FAST_RETURN ? 70u : 50u;
    measured_speed = window_progress = qualified_progress = 0;
    last_position = x0;
    dbg_puts(next == PAGE_MOVE ? "[motor] page step" : next == SEEK ? "[home] seek left" : next == BACKOFF ?
             "[home] backoff" : next == RETURN ? "[home] repeat left seek" : next == FAST_RETURN ? "[home] fast return" :
             direction ? "[motor] jog backward" : "[motor] jog forward");
    dbg_puts(" start="); dbg_u32(x0); dbg_puts(" target="); dbg_u32(target); dbg_putc('\n');
    started = app_clock_ticks();
    wr(C, 0x102u);  /* first non-neutral command comes from the 1ms controller */
    return true;
}

static void settle(enum phase next)
{
    stop_outputs(); phase = SETTLE; after_settle = next; settle_at = time_ms();
}

static void control_tick(unsigned ms, uint32_t x, int32_t progress)
{
    /* f9130: startup estimate, direction-qualified period velocity, and
     * decay when position stops. At home speed P[15:14]=0 => shift=2,N=1. */
    bool correct = ((rd(P + 0x304u) & 4u) != 0) == (direction != 0);
    if (!correct) qualified_progress = progress;
    if (progress < 8 || progress <= qualified_progress + 4) {
        measured_speed = 0; same_ms = 0;
    } else if (x == last_position) {
        same_ms += ms - last_ms;
        uint32_t cap = motor_divide(4000u, same_ms);
        if ((uint32_t)measured_speed > cap) measured_speed = (int32_t)cap;
    } else {
        same_ms = 0;
        wr(P + 0x210u, 0u);
        uint32_t period = rd(P + 0x200u);
        measured_speed = period && period < 0xffffffu ?
                         (int32_t)motor_divide(48000000u, period) : 0;
    }
    if (!correct) measured_speed = 0;
    if (measured_speed < 0 || measured_speed > 1000000) {
        finish("invalid velocity input", HOME_FAILED); return;
    }
    bool backoff = phase == BACKOFF || phase == PAGE_MOVE || phase == SCAN_MOVE || phase == FAST_RETURN;
    uint32_t wanted = phase == SCAN_MOVE ? motor_control_ramp_to(ms - 1u, 1200u, scan_speed, 70u)
                    : phase == FAST_RETURN ? motor_control_ramp_to(ms - 1u, 1200u, return_speed, 70u * return_speed / 20000u)
                                         : motor_control_ramp(ms - 1u, backoff);
    /* Backoff approach is deliberately slower in its last 3000 counts.
     * It uses the same PI, without importing the whole stock finite planner. */
    if (phase == PAGE_MOVE) { if (wanted > 4000u) wanted = 4000u; }
    else if (phase == SCAN_MOVE) {
        uint32_t cap = progress >= (int32_t)(span - SCAN_APPROACH) ? 4000u : scan_speed;
        if (wanted > cap) wanted = cap;
    }
    else if (phase == FAST_RETURN) {
        /* Linear slow-down over the last FAST_DECEL counts, floor 4000. */
        int32_t remaining = (int32_t)span - progress;
        if (remaining < (int32_t)FAST_DECEL) {
            uint32_t cap = remaining <= 0 ? 4000u : motor_divide((uint64_t)return_speed * (uint32_t)remaining, FAST_DECEL);
            if (cap < 4000u) cap = 4000u;
            if (wanted > cap) wanted = cap;
        }
    }
    else if (backoff && progress >= (int32_t)(span - REPEAT_TOLERANCE)) wanted = 4000u;
    uint32_t compare = motor_control_step(&control, (int32_t)wanted,
                                         measured_speed, backoff, direction);
    if (compare > 4000u) { finish("invalid controller output", HOME_FAILED); return; }
    wr(C + 8u, compare);
    /* Trace every 100 ms through the ramp, then every 500 ms: the 4 KiB log
     * ring must keep the page and line-setup messages of a 25 s travel. */
    uint32_t cadence = ms < 1000u ? 100u : 500u;
    if (ms / cadence != last_ms / cadence) {
        dbg_puts("[home] phase="); dbg_dec(phase); dbg_puts(" ms="); dbg_dec(ms);
        dbg_puts(" pos="); dbg_u32(x); dbg_puts(" compare="); dbg_dec(compare);
        dbg_puts(" speed="); dbg_dec((uint32_t)measured_speed); dbg_putc('\n');
    }
    last_position = x; last_ms = ms;
}

void scanner_motor_poll(void)
{
    if (phase == WANT_PAGE_MOVE) { (void)start_move(PAGE_MOVE); return; }
    if (phase == WANT_SCAN_MOVE) { (void)start_move(SCAN_MOVE); return; }
    if (phase == WANT_HOME || phase == WANT_JOG) {
        enum phase next = phase == WANT_HOME ? SEEK : JOG;
        if (next == SEEK && fast_return) {
            uint32_t x = rd(P + 0x100u);
            if (x > home_position && x - home_position > FAST_MARGIN + FAST_MIN) next = FAST_RETURN;
        }
        if (next == SEEK || next == FAST_RETURN) {
            stop_outputs();
            /* Stock scanner initialization powers this domain at fa280 before
             * home. Its electrical role is not inferred from the address. */
            saved_scanner_power = rd(0xc0c85614u); home_power_owned = true;
            wr(0xc0c85614u, 1u); delay_ticks(20u * CLOCK_TICKS_PER_MS);
        }
        if (!initialized && !initialize()) { finish("setup failed", HOME_FAILED); return; }
        (void)start_move(next); return;
    }
    if (phase == SETTLE) {
        if ((uint32_t)(time_ms() - settle_at) < 100u) return;
        uint32_t x = rd(P + 0x100u);
        if (after_settle == BACKOFF) first_stop = x;
        else if (after_settle == SEEK) { /* fast return done: stock seek from here */ }
        else if (after_settle == RETURN) {
            backoff_end = x;
            if ((int32_t)(x - first_stop) < (int32_t)MIN_BACKOFF) {
                finish("backoff travel not verified", HOME_FAILED); return;
            }
        } else {
            int32_t error = (int32_t)(x - first_stop);
            if (error < -(int32_t)REPEAT_TOLERANCE || error > (int32_t)REPEAT_TOLERANCE) {
                finish("home end did not repeat", HOME_FAILED); return;
            }
            home_position = x;
            finish("mechanical reference established; optical calibration pending", MECHANICAL_REFERENCE);
            return;
        }
        (void)start_move(after_settle); return;
    }
    if (!driving()) return;
    uint32_t elapsed = app_clock_ticks() - started;
    unsigned ms = elapsed / CLOCK_TICKS_PER_MS;
    if (ms >= limit_ms) { finish("phase deadline", HOME_FAILED); return; }
    if (ms == last_ms) return;
    if (ms - last_ms > 4u) {
        dbg_puts("[motor] late tick in "); dbg_puts(app_section); dbg_putc('\n');
        finish("control tick delayed", HOME_FAILED); return;
    }
    if (ms - last_ms > 2u) {                    /* early warning, before a failure */
        dbg_puts("[motor] slow tick "); dbg_dec(ms - last_ms); dbg_puts("ms in ");
        dbg_puts(app_section); dbg_putc('\n');
    }
    uint32_t x = rd(P + 0x100u);
    int32_t delta = (int32_t)(x - x0), progress = direction ? -delta : delta;
    if (x > 0xffffffu || progress < -32) { finish("opposite position feedback", HOME_FAILED); return; }
    bool endpoint = (rd(C + 0x104u) & 1u) != 0;
    if (progress >= (int32_t)span || endpoint) {
        if ((phase == PAGE_MOVE || phase == SCAN_MOVE) && progress >= (int32_t)(span - 4u)) {
            move_ok = true; finish(phase == SCAN_MOVE ? "scan endpoint" : "page endpoint", HOME_FAILED);
        } else if (phase == BACKOFF && progress >= (int32_t)(span - 4u)) settle(RETURN);
        else if (phase == FAST_RETURN && progress >= (int32_t)(span - 64u)) settle(SEEK);
        else finish(phase == JOG ? "jog endpoint" : "unexpected seek/early endpoint", HOME_FAILED);
        return;
    }
    /* Stock's seek stall window is 500 ticks with minimum progress 3000.
     * Its first stall is provisional; never call it a home reference yet. */
    if (phase != JOG && phase != PAGE_MOVE && ms >= window_ms && ms - window_ms >= 500u) {
        int32_t minimum = phase == BACKOFF ? 1700 : phase == SCAN_MOVE || phase == FAST_RETURN ? 1000 : 3000;
        bool stalled = progress - window_progress <= minimum;
        window_progress = progress; window_ms = ms;
        if (stalled) {
            if (phase == SCAN_MOVE) finish("scan travel stalled", HOME_FAILED);
            else if (phase == FAST_RETURN) finish("fast return stalled", HOME_FAILED);
            else if (phase == SEEK) settle(BACKOFF);
            else if (phase == RETURN && progress >= (int32_t)(backoff_end - first_stop) - (int32_t)REPEAT_TOLERANCE) {
                int32_t error = (int32_t)(x - first_stop);
                if (error >= -(int32_t)REPEAT_TOLERANCE && error <= (int32_t)REPEAT_TOLERANCE) settle(IDLE);
                else finish("return stalled away from first stop", HOME_FAILED);
            } else finish("backoff/return stalled without verified travel", HOME_FAILED);
            return;
        }
    }
    control_tick(ms, x, progress);
}
