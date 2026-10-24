/* Small open-loop direction experiment; no Canon executable code.
 * Stock evidence: setup 116da6/116eb0/11667a, P init f7c08/f6982,
 * arming f8a5c, output f8638/f7696, stop f9060 (flat stock addresses).
 * Electrical polarity and position tracking still need physical validation.
 * The independent deadline, not the unverified encoder, limits drive time.
 */
#include "app.h"

#define P 0xc0c57000u
#define C 0xc0c56000u
#define T 0xc0140400u
#define COUNTER 0xc0c82008u
#define NEUTRAL 2000u
#define PULSE_TICKS (150u * CLOCK_TICKS_PER_MS)
#define TRAVEL_LIMIT 512u       /* ~0.34 mm at stock scale; not calibrated */
#define STALL_TICKS (25u * CLOCK_TICKS_PER_MS)
#define EFFORT 120u             /* 1880 / 2120; previous +/-60 did not move */
#define RAMP_STEP (EFFORT / 4u)
#define COOLDOWN_MS 300u

static bool initialized, pending, active, inhibited, cooling;
static unsigned direction;
static uint32_t started, x0, cool_until, progress_at, progress_counts;

static void stop_outputs(void)
{
    wr(C, 0u); wr(C + 8u, NEUTRAL);
    wr(P + 0x11cu, 0u); wr(P + 0x124u, 0u);
    wr(C + 0x100u, 0u); wr(P + 0x400u, 0u);
    wr(T, 0x80000000u);       /* no unbounded timer-reset polling */
}

static void stop(const char *reason)
{
    bool was_active = active;
    /* Two bounded MMIO reads capture the commanded output before disabling;
     * all formatting and USB log delivery happen after output disable. */
    uint32_t control = 0u, compare = NEUTRAL, elapsed = 0u;
    if (was_active) { control = rd(C); compare = rd(C + 8u); }
    wr(C, 0u);
    if (was_active) elapsed = rd(COUNTER) - started;
    stop_outputs();
    pending = active = false;
    cooling = true; cool_until = time_ms() + COOLDOWN_MS;
    dbg_puts("[motor] stop: "); dbg_puts(reason);
    if (was_active) {
        uint32_t x = rd(P + 0x100u);
        int32_t delta = (int32_t)(x - x0);
        dbg_puts(" elapsed_us="); dbg_dec(elapsed / (CLOCK_TICKS_PER_MS / 1000u));
        dbg_puts(" control="); dbg_u32(control);
        dbg_puts(" compare="); dbg_dec(compare);
        dbg_puts(" pos="); dbg_u32(x);
        dbg_puts(delta < 0 ? " delta=-" : " delta=+");
        dbg_dec(delta < 0 ? 0u - (uint32_t)delta : (uint32_t)delta);
        dbg_puts(" s304="); dbg_u32(rd(P + 0x304u));
    }
    dbg_putc('\n');
}

bool scanner_motor_busy(void) { return pending || active; }

void scanner_motor_cancel(void)
{
    if (scanner_motor_busy()) stop("cancel");
}

void scanner_motor_shutdown(void)
{
    inhibited = true;
    if (scanner_motor_busy()) stop("reboot");
    else if (initialized) stop_outputs();
}

bool scanner_motor_request(unsigned dir)
{
    if (dir > 1u || inhibited) return false;
    if (scanner_motor_busy()) { stop("overlapping request"); return false; }
    if (cooling && (int32_t)(time_ms() - cool_until) < 0) return false;
    cooling = false; direction = dir; pending = true;
    return true;
}

static bool serial_send(uint32_t word)
{
    wr(0xc0c50204u, word); wr(0xc0c50210u, 0xffffffffu);
    uint32_t start = rd(COUNTER);
    for (unsigned budget = 0; budget < 50000u; ++budget) {
        if (!(rd(0xc0c50218u) & 1u)) return true;
        if ((uint32_t)(rd(COUNTER) - start) >= 2u * CLOCK_TICKS_PER_MS) break;
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
    /* Match the scale observed at the first stock home arm. This does not
     * configure a velocity servo; it is identical for both jog directions. */
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

static void start(void)
{
    if (!initialized && !initialize()) { stop("setup failed"); return; }
    stop_outputs();
    x0 = rd(P + 0x100u);
    if (x0 < TRAVEL_LIMIT + 1u || x0 > 0x00ffffffu - TRAVEL_LIMIT - 1u) {
        stop("position outside test range"); return;
    }
    uint32_t target = direction ? x0 - TRAVEL_LIMIT : x0 + TRAVEL_LIMIT;
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
    dbg_puts(direction ? "[motor] tiny backward" : "[motor] tiny forward");
    dbg_puts(" max=150ms limit=512 stall=25ms effort="); dbg_dec(EFFORT);
    dbg_puts(" pos="); dbg_u32(x0); dbg_putc('\n');
    pending = false; active = true;
    started = rd(COUNTER);
    progress_at = progress_counts = 0u;
    wr(C, 0x102u);
    wr(C + 8u, direction ? NEUTRAL - RAMP_STEP : NEUTRAL + RAMP_STEP);
}

void scanner_motor_poll(void)
{
    if (pending) { start(); return; }
    if (!active) return;
    uint32_t elapsed = rd(COUNTER) - started;
    if (elapsed >= PULSE_TICKS) { stop("150ms deadline"); return; }
    int32_t delta = (int32_t)(rd(P + 0x100u) - x0);
    if (delta >= (int32_t)TRAVEL_LIMIT || delta <= -(int32_t)TRAVEL_LIMIT) {
        stop("travel limit"); return;
    }
    if ((direction && delta > 16) || (!direction && delta < -16)) {
        stop("wrong direction feedback"); return;
    }
    /* Require four new counts in the requested direction each 25ms window.
     * Only increasing progress refreshes the deadline; jitter cannot keep a
     * stationary drive alive. This does not certify a physical encoder. */
    int32_t forward = direction ? -delta : delta;
    if (forward >= (int32_t)(progress_counts + 4u)) {
        progress_counts = (uint32_t)forward; progress_at = elapsed;
    }
    if (elapsed - progress_at >= STALL_TICKS) { stop("no progress 25ms"); return; }
    if (rd(C + 0x104u) & 1u) { stop("completion"); return; }
    /* 4ms ramp, then hold to target/deadline; no escalation on missing feedback. */
    unsigned step = elapsed / CLOCK_TICKS_PER_MS + 1u;
    unsigned effort = step < 4u ? step * RAMP_STEP : EFFORT;
    wr(C + 8u, direction ? NEUTRAL - effort : NEUTRAL + effort);
}
