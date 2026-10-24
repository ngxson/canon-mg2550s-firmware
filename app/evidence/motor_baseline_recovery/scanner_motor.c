/* Restore the motor register path of the user's forward-working app.
 * Reference image ea0972750ec576445a99822ee8deec8b823548ae437a365960a1261735084919.
 * Recovered from its saved disassembly and checked by executing both images.
 * This configuration moved forward for BOTH commands in the user's tests;
 * restoring it is regression recovery, not a claim to have repaired reverse.
 * No stock executable code is linked. USB/I2C recovery and key debounce remain.
 */
#include "app.h"

#define P 0xc0c57000u
#define C 0xc0c56000u
#define T 0xc0140400u
#define COUNTER 0xc0c82008u
#define NEUTRAL 2000u
#define PULSE_TICKS (9u * CLOCK_TICKS_PER_MS)
#define TRAVEL_LIMIT 2048u
#define COOLDOWN_MS 300u

static bool initialized, pending, active, inhibited, cooling;
static unsigned direction, checked_ms, written_effort;
static uint32_t started, x0, cool_until;

static void stop_outputs(void)
{
    /* Original motor_stop, including the original write order. */
    wr(C, 0u); wr(C + 8u, NEUTRAL); wr(P + 0x11cu, 0u);
    wr(C + 0x100u, 0u); wr(P + 0x400u, 0u); wr(T, 0x80000000u);
}

static void stop(const char *reason)
{
    bool was_active = active;
    uint32_t compare = was_active ? rd(C + 8u) : NEUTRAL;
    if (was_active) wr(P + 0x210u, 0u); /* original end-of-jog cleanup */
    stop_outputs();
    uint32_t elapsed = was_active ? rd(COUNTER) - started : 0u;
    pending = active = false;
    cooling = true; cool_until = time_ms() + COOLDOWN_MS;
    dbg_puts("[motor] stop: "); dbg_puts(reason);
    if (was_active) {
        uint32_t x = rd(P + 0x100u);
        int32_t delta = (int32_t)(x - x0);
        dbg_puts(" elapsed_us="); dbg_dec(elapsed / (CLOCK_TICKS_PER_MS / 1000u));
        dbg_puts(" compare="); dbg_dec(compare);
        dbg_puts(" pos="); dbg_u32(x);
        dbg_puts(delta < 0 ? " delta=-" : " delta=+");
        dbg_dec(delta < 0 ? 0u - (uint32_t)delta : (uint32_t)delta);
        dbg_puts(" s304="); dbg_u32(rd(P + 0x304u));
    }
    dbg_putc('\n');
}

bool scanner_motor_busy(void) { return pending || active; }
void scanner_motor_cancel(void) { if (scanner_motor_busy()) stop("cancel"); }
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
    for (unsigned budget = 0; budget < 100000u; ++budget) {
        if (!(rd(0xc0c50218u) & 1u)) return true;
        if ((uint32_t)(rd(COUNTER) - start) >= 2u * CLOCK_TICKS_PER_MS) break;
    }
    dbg_puts("[motor] serial timeout word="); dbg_u32(word); dbg_putc('\n');
    return false;
}

static void original_routes(void)
{
    static const uint32_t values[7] = {0, 1, 2, 6, 7, 11, 3};
    for (unsigned i = 0; i < 7u; ++i) wr(0xc0c41400u + 4u*i, values[i]);
}

static bool initialize(void)
{
    static const uint32_t commands[] = {
        0x8000000bu, 0x8000000au, 0x80000009u, 0x80000008u,
        0x8000400fu, 0x8000400fu, 0x022au, 0xc4c0u, 0xc4c0u, 0xc4c0u,
    };
    /* Do not silently change another output when reproducing the old path.
     * If another motor is already enabled, refuse shared setup. */
    if (rd(0xc0c50000u) || rd(0xc0c52000u)) {
        dbg_puts("[motor] another motor enabled; refusing setup\n"); return false;
    }
    wr(0xc0c41004u, 0u); original_routes();
    wr(0xc0c41008u, 0u); delay_ticks(CLOCK_TICKS_PER_MS);
    wr(0xc0c41008u, 1u); delay_ticks(CLOCK_TICKS_PER_MS);
    wr(0xc0c41008u, 0u);
    wr(0xc0c50200u, 0xf07u); wr(0xc0c50208u, 3u);
    for (unsigned i = 0; i < ARRAY_SIZE(commands); ++i) {
        if (!serial_send(commands[i])) { wr(0xc0c41008u, 0u); return false; }
    }
    wr(0xc0c50004u, 4000u); wr(0xc0c52004u, 4000u); wr(C + 4u, 4000u);
    wr(0xc0c85604u, 1u);
    original_routes(); wr(0xc0c41008u, 1u);

    wr(P + 0x12cu, 0u); wr(P, 0xa00du);
    modify(P, 0x3000u, 0x2000u); modify(P, 0xc000u, 0x8000u);
    wr(P + 0x304u, 0u);
    wr(C, 0u); wr(C + 4u, 4000u); wr(C + 8u, NEUTRAL);
    /* Original behavior: no position seed, P+148/P+308 writes, shared P reset
     * pulse, c0c81090 write or post-serial 4/5/10 routing change. */
    wr(P + 0x200u, 0xffffffu);
    wr(P + 0x108u, 0u); wr(P + 0x10cu, 0u);
    wr(C + 0x100u, 0u); wr(P + 0x400u, 0u);
    wr(C + 0x104u, 0u); wr(P + 0x404u, 0u); wr(P + 0x11cu, 0u);
    wr(C + 0x10u, NEUTRAL); wr(P + 0x120u, 0u); wr(C + 0x14u, 0u);
    wr(P + 0x220u, 0u);
    wr(P + 0x204u, 0xffffffu); wr(P + 0x208u, 0xffffffu); wr(P + 0x20cu, 0xffffffu);
    wr(P + 0x124u, 0u); wr(P + 0x128u, 0u); wr(C + 0x24u, 0u);
    wr(P + 4u, 0u); wr(P + 8u, 0x78u); wr(P + 0x12cu, 0u);
    initialized = true;
    dbg_puts("[motor] restored pre-takeover setup; reverse still unverified\n");
    return true;
}

static void start(void)
{
    if (!initialized && !initialize()) { stop("setup failed"); return; }
    stop_outputs();
    x0 = rd(P + 0x100u);
    uint32_t target = direction ? x0 - TRAVEL_LIMIT : x0 + TRAVEL_LIMIT;
    wr(P + 0x200u, 0xffffffu); wr(P + 0x204u, 0xffffffu);
    wr(P + 0x208u, 0xffffffu); wr(P + 0x20cu, 0xffffffu);
    wr(C + 8u, NEUTRAL); wr(P + 0x200u, 0xffffffu);
    wr(P + 0x160u, 1u); wr(P + 0x164u, rd(P + 0x100u));
    wr(P + 0x124u, 0u); wr(P + 0x11cu, 0u);
    wr(P + 0x120u, target); wr(C + 0x10u, NEUTRAL);
    wr(P + 0x128u, direction ? target - 1u : target + 1u);
    wr(C + 0x28u, 0u); wr(C + 0x1cu, 0u); wr(P + 0x124u, 1u);
    wr(C + 0x18u, 0u); wr(C + 0x0cu, 0u); wr(P + 0x11cu, 1u);
    wr(P + 0x130u, 0u); wr(P + 0x12cu, 0u);
    wr(C + 0x100u, 0u); wr(C + 0x104u, 0u); modify(C + 0x100u, 0u, 1u);
    dbg_puts("[motor] original command dir="); dbg_dec(direction);
    dbg_puts(" 9ms effort=60 target-span=2048 pos="); dbg_u32(x0); dbg_putc('\n');
    pending = false; active = true;
    checked_ms = 0u; written_effort = 12u;
    started = rd(COUNTER);
    wr(C, 0x102u); wr(C + 8u, direction ? NEUTRAL - 12u : NEUTRAL + 12u);
}

void scanner_motor_poll(void)
{
    if (pending) { start(); return; }
    if (!active) return;
    uint32_t elapsed = rd(COUNTER) - started;
    if (elapsed >= PULSE_TICKS) { stop("original 9ms deadline"); return; }
    unsigned ms = elapsed / CLOCK_TICKS_PER_MS;
    if (ms == checked_ms) return;
    checked_ms = ms;
    int32_t delta = (int32_t)(rd(P + 0x100u) - x0);
    if (delta > (int32_t)TRAVEL_LIMIT || delta < -(int32_t)TRAVEL_LIMIT) {
        stop("original overtravel guard"); return;
    }
    /* Original ramp: +/-12,24,36,48,60, one millisecond at each value;
     * then four further milliseconds at 60. Do not add a feedback servo or
     * treat unverified completion/direction bits as successful movement. */
    unsigned effort = ms < 4u ? (ms + 1u) * 12u : 60u;
    if (effort != written_effort) {
        wr(C + 8u, direction ? NEUTRAL - effort : NEUTRAL + effort);
        written_effort = effort;
    }
}
