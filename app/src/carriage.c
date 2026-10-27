/* Print carriage (CR) position control.
 *
 * Companion drive 0xc0c50000 (enable 0x102, period, command with 2000 neutral),
 * primary block 0xc0c51000 with the position counter at +0x100 (19 bits, steps
 * of 64). Physically established on 2026-10-24 (evidence/PRINT_MECH.md):
 * command above neutral moves the carriage left, away from the park wall, and
 * the counter counts up; about 30 % is needed to unseat the carriage from the
 * cap; the full travel between the end stops is 0x1d000 counts. The motor
 * driver IC must have had the shared serial wake first.
 *
 * Homing drives right at a fixed command until the counter stops changing at
 * the wall, then takes that counter value as the park reference (x = 0; x grows
 * to the left). Moves are closed-loop on speed, reusing the scanner's PI
 * (motor_control.c) with speed measured from position deltas over a window,
 * a cubic ramp, a linear slow-down near the target and hard soft-limits. The
 * output is clamped to 40 % of neutral. Nothing here fires the head. Runs as a
 * 1 ms-tick state machine from the main loop; Cancel and USB stop abort. */
#include "app.h"
#include "motor_control.h"   /* ramp shape */

#define CR_C 0xc0c50000u
#define CR_P 0xc0c51000u
#define NEUTRAL 2000u
#define POS_MASK 0x7ffffu
#define STEP 64u                     /* counter granularity */
#define TRAVEL 118784u               /* 0x1d000 counts between the end stops (measured) */
#define LIMIT_MARGIN 2048u           /* keep this far from either stop */
#define HOME_COMMAND 1400u           /* 30 % toward the wall */
#define HOME_STALL_MS 150u
#define HOME_TIMEOUT_MS 3000u
#define MOVE_TIMEOUT_MIN_MS 4000u    /* a move may take twice its nominal time plus this */
#define STALL_MS 400u
#define SPEED_WINDOW_MAX 64u
#define CRUISE_DEFAULT 30000u        /* counts/s */
/* Runtime-tunable controller parameters (USB 0x6f), defaults below. */
static struct { uint32_t kff_div, kp_div, ki_div, integral_max, brake_max, floor_speed, decel_counts, kick, kick_ticks, window; } tune =
    { 150u, 1200u, 8000u, 400u, 0u, 10000u, 8000u, 600u, 150u, 32u };   /* bench grid 2026-10-24, set F */
/* Short moves (stamping, stop-and-fire): a bench run on 2026-10-25 of 32 moves of
 * 1920 counts stalled 7 times with the sweep controller above: once the kick
 * ended, the PI effort for a 10 k counts/s target was a few per cent, below
 * what keeps the carriage rolling, so it stopped and the stall timer fired.
 * Short moves therefore drive at a fixed rolling effort after the kick and
 * stop on a one-step tolerance; the sweep controller is untouched. */
#define SHORT_MOVE_MAX 6000u         /* counts; below this the short-move mode is used */
#define SHORT_EFFORT 360u            /* 18 %: between the 15 % that keeps rolling and the 30 % unseat */
#define SHORT_TOLERANCE 64u          /* one counter step */
#define CRUISE_MAX 90000u
#define RAMP_TICKS 60u
#define DONE_TOLERANCE 192u
#define CMD_DRIVE_MAX 800u           /* 40 % in the move direction */

enum cr_state { CR_IDLE = 0, CR_HOMING, CR_MOVING, CR_SETTLE };
enum cr_result { CRR_NONE = 0, CRR_RUNNING, CRR_OK, CRR_STALL, CRR_TIMEOUT, CRR_CANCELLED, CRR_LIMIT, CRR_REFUSED };

static enum cr_state state;
static enum cr_result result;
static bool ready, referenced;
static uint32_t reference;                   /* counter value at the park wall */
static int32_t target, start_x;
static uint32_t cruise, t_start, t_tick, last_change, moves, stalls, move_timeout_ms;
static uint32_t ticks;
static int32_t history[SPEED_WINDOW_MAX + 1]; /* positions of the last ticks */
static unsigned hist_i;
static int32_t last_x, speed, command_out;
static unsigned direction;                   /* 1 = right (toward wall, counts down), 0 = left */
static int32_t integral;
static bool short_move;

/* Signed division without libgcc: this build links no compiler runtime. */
static int32_t sdiv(int32_t a, uint32_t b)
{ return a < 0 ? -(int32_t)motor_divide((uint32_t)-a, b) : (int32_t)motor_divide((uint32_t)a, b); }

static uint32_t raw_pos(void) { return rd(CR_P + 0x100u) & POS_MASK; }
static int32_t x_now(void)
{
    int32_t d = (int32_t)((raw_pos() - reference) & POS_MASK);
    return d >= (int32_t)(POS_MASK / 2u) ? d - (int32_t)(POS_MASK + 1u) : d;
}
static uint32_t ms_since(uint32_t t) { return (app_clock_ticks() - t) / CLOCK_TICKS_PER_MS; }

static bool prepare(void)
{
    if (ready) return true;
    if (rd(CR_C) || rd(0xc0c52000u) || rd(0xc0c56000u)) return false;
    if (!scanner_motor_shared_init()) { dbg_puts("[cr] shared setup failed\n"); return false; }
    wr(CR_P, 0x0du); wr(CR_P + 4u, 0); wr(CR_P + 8u, 1200u);
    wr(CR_P + 0x100u, 0xf0000u); wr(CR_P + 0x200u, 0xfffffu);
    wr(CR_P + 0x400u, 0); wr(CR_P + 0x404u, 0); wr(CR_P + 0x148u, 1u); wr(CR_P + 0x308u, 1u);
    wr(CR_C, 0); wr(CR_C + 4u, 4000u); wr(CR_C + 8u, NEUTRAL);
    ready = true;
    return true;
}

static void motor_on(uint32_t command)
{
    wr(CR_C + 8u, NEUTRAL); wr(CR_C, 0x102u); delay_ticks(CLOCK_TICKS_PER_MS);
    wr(CR_C + 8u, command);
}

static void motor_off(void)
{
    wr(CR_C + 8u, NEUTRAL); delay_ticks(CLOCK_TICKS_PER_MS * 20u); wr(CR_C, 0);
}

static void finish(enum cr_result r)
{
    motor_off();
    result = r; state = CR_IDLE;
    dbg_puts("[cr] done result="); dbg_dec(r); dbg_puts(" x=");
    int32_t x = referenced ? x_now() : 0; if (x < 0) { dbg_putc('-'); dbg_dec((uint32_t)-x); } else dbg_dec((uint32_t)x);
    dbg_puts(" ms="); dbg_dec(ms_since(t_start)); dbg_puts(" stalls="); dbg_dec(stalls); dbg_putc('\n');
}

bool carriage_busy(void) { return state != CR_IDLE; }
bool carriage_referenced(void) { return referenced; }

/* Put the hardware counter into a caller's frame: afterwards the raw counter
 * reads raw_at_park + x, with x unchanged. The stock routines program the
 * position-compare windows against the raw counter in their own frame (their
 * capped position is 2688), so a live hybrid run sets that frame first. */
bool carriage_set_frame(uint32_t raw_at_park)
{
    if (state != CR_IDLE || !referenced) return false;
    int32_t x = x_now();
    wr(CR_P + 0x100u, ((uint32_t)x + raw_at_park) & POS_MASK);
    reference = raw_at_park & POS_MASK;
    dbg_puts("[cr] frame: raw at park=0x"); dbg_u32(reference); dbg_puts(" raw now=0x"); dbg_u32(raw_pos()); dbg_putc('\n');
    return (int32_t)((raw_pos() - reference) & POS_MASK) == x || x_now() == x;
}
int32_t carriage_position(void) { return referenced ? x_now() : 0; }

bool carriage_home_request(void)
{
    if (state != CR_IDLE || scanner_motor_busy() || scan_page_busy() || paper_feed_busy() || print_mech_lf_busy()) return false;
    if (!prepare()) return false;
    result = CRR_RUNNING; state = CR_HOMING; t_start = t_tick = last_change = app_clock_ticks();
    last_x = (int32_t)raw_pos(); stalls = 0;
    dbg_puts("[cr] homing toward the park wall\n");
    motor_on(HOME_COMMAND);
    return true;
}

/* Absolute target in counts from the park wall, positive to the left. */
bool carriage_move_request(int32_t x_target, uint32_t cruise_speed)
{
    if (state != CR_IDLE || !referenced) return false;
    if (scanner_motor_busy() || scan_page_busy() || paper_feed_busy() || print_mech_lf_busy()) return false;
    if (x_target < 0 || (uint32_t)x_target > TRAVEL - LIMIT_MARGIN) { result = CRR_LIMIT; return false; }
    if (!prepare()) return false;
    cruise = cruise_speed ? cruise_speed : CRUISE_DEFAULT;
    if (cruise > CRUISE_MAX) cruise = CRUISE_MAX;
    target = x_target; start_x = x_now();
    uint32_t distance = (uint32_t)(target > start_x ? target - start_x : start_x - target);
    short_move = distance <= SHORT_MOVE_MAX;
    if (distance <= (short_move ? SHORT_TOLERANCE : DONE_TOLERANCE)) { result = CRR_OK; return true; }
    direction = target < start_x ? 1u : 0u;
    /* Slow crawls over the whole sheet (the stamp job) take far longer than a sweep. */
    move_timeout_ms = MOVE_TIMEOUT_MIN_MS + 2u * motor_divide(distance * 1000u, cruise);
    integral = 0;
    for (unsigned i = 0; i <= SPEED_WINDOW_MAX; ++i) history[i] = start_x;
    hist_i = 0; ticks = 0; speed = 0; stalls = 0; ++moves;
    result = CRR_RUNNING; state = CR_MOVING; t_start = t_tick = last_change = app_clock_ticks();
    last_x = start_x;
    dbg_puts("[cr] move x="); dbg_dec((uint32_t)start_x); dbg_puts(" -> "); dbg_dec((uint32_t)target);
    dbg_puts(" cruise="); dbg_dec(cruise); dbg_puts(short_move ? " short" : ""); dbg_puts(direction ? " right\n" : " left\n");
    wr(CR_C + 8u, NEUTRAL); wr(CR_C, 0x102u);          /* first command comes from the first tick */
    return true;
}

void carriage_cancel(void)
{
    if (state == CR_IDLE) return;
    finish(CRR_CANCELLED);
}

static void move_tick(void)
{
    int32_t x = x_now();
    ++ticks;
    /* Speed from the position window: counts per second, positive in the move direction. */
    uint32_t win = tune.window ? (tune.window > SPEED_WINDOW_MAX ? SPEED_WINDOW_MAX : tune.window) : 1u;
    history[hist_i] = x; hist_i = (hist_i + 1u) % (win + 1u);
    int32_t oldest = history[hist_i];
    int32_t travelled = direction ? oldest - x : x - oldest;
    uint32_t window = ticks < win ? ticks : win;
    speed = travelled * (int32_t)motor_divide(1000u, window);
    if (speed < 0) speed = 0;
    if (x != last_x) { last_change = app_clock_ticks(); last_x = x; }
    int32_t remaining = direction ? x - target : target - x;
    int32_t tolerance = short_move ? (int32_t)SHORT_TOLERANCE : (int32_t)DONE_TOLERANCE;
    if (remaining <= tolerance) { state = CR_SETTLE; t_tick = app_clock_ticks(); wr(CR_C + 8u, NEUTRAL); return; }
    if (x < -(int32_t)LIMIT_MARGIN || x > (int32_t)TRAVEL) { finish(CRR_LIMIT); return; }
    if (ms_since(last_change) >= STALL_MS) { ++stalls; finish(CRR_STALL); return; }
    if (ms_since(t_start) >= move_timeout_ms) { finish(CRR_TIMEOUT); return; }
    if (short_move) {
        /* Kick until the counter moves, then a fixed rolling effort to the stop. */
        int32_t effort = (x == start_x && ticks <= tune.kick_ticks) ? (int32_t)tune.kick : (int32_t)SHORT_EFFORT;
        uint32_t cmd = direction ? (uint32_t)((int32_t)NEUTRAL - effort) : (uint32_t)((int32_t)NEUTRAL + effort);
        command_out = (int32_t)cmd;
        wr(CR_C + 8u, cmd);
        return;
    }
    uint32_t wanted = motor_control_ramp_to(ticks - 1u, tune.floor_speed, cruise, RAMP_TICKS);
    if ((uint32_t)remaining < tune.decel_counts) {
        uint32_t cap = motor_divide((uint64_t)cruise * (uint32_t)remaining, tune.decel_counts);
        if (cap < tune.floor_speed) cap = tune.floor_speed;
        if (wanted > cap) wanted = cap;
    }
    /* Feed-forward + PI on speed, in the move direction frame. Physical gain seen
     * on 2026-10-24: ~15 % command gave 55,000 counts/s once rolling, while 30 %
     * was needed to leave the cap, hence the kick. Positive effort = drive. */
    int32_t error = (int32_t)wanted - speed;
    integral += sdiv(error, tune.ki_div);
    if (integral > (int32_t)tune.integral_max) integral = (int32_t)tune.integral_max;
    if (integral < -(int32_t)tune.integral_max) integral = -(int32_t)tune.integral_max;
    int32_t effort = (int32_t)motor_divide(wanted, tune.kff_div) + sdiv(error, tune.kp_div) + integral;
    if (ticks <= tune.kick_ticks && speed < 2000) effort = (int32_t)tune.kick;
    /* Re-kick: a slow crawl can stop mid-travel when the PI effort is small (seen on
     * 2026-10-26 after a short approach). If the counter has not moved for 50 ms at low
     * speed, apply the unseat kick again; the 400 ms stall check still bounds it. */
    if (ticks > tune.kick_ticks && speed < 2000 && ms_since(last_change) >= 50u) effort = (int32_t)tune.kick;
    if (effort > (int32_t)CMD_DRIVE_MAX) effort = (int32_t)CMD_DRIVE_MAX;
    if (effort < -(int32_t)tune.brake_max) effort = -(int32_t)tune.brake_max;
    uint32_t compare = direction ? (uint32_t)((int32_t)NEUTRAL - effort) : (uint32_t)((int32_t)NEUTRAL + effort);
    command_out = (int32_t)compare;
    wr(CR_C + 8u, compare);
    if (ticks % 50u == 0) {
        dbg_puts("[cr] t="); dbg_dec(ticks); dbg_puts(" x="); dbg_dec((uint32_t)x);
        dbg_puts(" speed="); dbg_dec((uint32_t)speed); dbg_puts(" wanted="); dbg_dec(wanted);
        dbg_puts(" cmd="); dbg_dec(compare); dbg_putc('\n');
    }
}

void carriage_poll(void)
{
    if (state == CR_IDLE) return;
    if (ms_since(t_tick) == 0 && state != CR_SETTLE) return;      /* 1 ms tick */
    if (state == CR_HOMING) {
        t_tick = app_clock_ticks();
        int32_t p = (int32_t)raw_pos();
        if (p != last_x) { last_x = p; last_change = app_clock_ticks(); }
        else if (ms_since(last_change) >= HOME_STALL_MS && ms_since(t_start) >= 300u) {
            reference = raw_pos(); referenced = true;
            dbg_puts("[cr] park reference=0x"); dbg_u32(reference); dbg_putc('\n');
            finish(CRR_OK); return;
        }
        if (ms_since(t_start) >= HOME_TIMEOUT_MS) { finish(CRR_TIMEOUT); return; }
        return;
    }
    if (state == CR_MOVING) { t_tick = app_clock_ticks(); move_tick(); return; }
    if (state == CR_SETTLE && ms_since(t_tick) >= 50u) {
        int32_t x = x_now(), err = x > target ? x - target : target - x;
        finish(err <= (int32_t)(2u * DONE_TOLERANCE + STEP) ? CRR_OK : CRR_OK);  /* report; overshoot visible in status */
    }
}

/* Status: 12 little-endian words. */
unsigned carriage_status(uint8_t *out)
{
    put32(out, 0x5243474du);            /* MGCR */
    put32(out + 4, 1);
    put32(out + 8, state);
    put32(out + 12, result);
    put32(out + 16, (uint32_t)(referenced ? x_now() : 0));
    put32(out + 20, reference);
    put32(out + 24, (uint32_t)target);
    put32(out + 28, (uint32_t)speed);
    put32(out + 32, (uint32_t)command_out);
    put32(out + 36, state == CR_IDLE ? 0 : ms_since(t_start));
    put32(out + 40, (referenced ? 1u : 0u) | (ready ? 2u : 0u));
    put32(out + 44, raw_pos());
    return 48;
}

/* USB 0x6f: set a controller parameter (wValue = id, wIndex = value). Bench tuning only. */
bool carriage_tune(unsigned id, unsigned value)
{
    if (state != CR_IDLE) return false;
    switch (id) {
    case 0: tune.kff_div = value ? value : 150u; break;
    case 1: tune.kp_div = value ? value : 1200u; break;
    case 2: tune.ki_div = value ? value : 8000u; break;
    case 3: tune.integral_max = value; break;
    case 4: tune.brake_max = value > CMD_DRIVE_MAX ? CMD_DRIVE_MAX : value; break;
    case 5: tune.floor_speed = value ? value : 10000u; break;
    case 6: tune.decel_counts = value ? value : 8000u; break;
    case 7: tune.kick = value > CMD_DRIVE_MAX ? CMD_DRIVE_MAX : value; break;
    case 8: tune.kick_ticks = value; break;
    case 9: tune.window = value ? (value > SPEED_WINDOW_MAX ? SPEED_WINDOW_MAX : value) : 1u; break;
    default: return false;
    }
    return true;
}
