/* Power manager: keep USB and management alive while unused hardware rests.
 *
 * Policy (fw_dump/gpt/BREIF_POWER_MANAGEMENT.md, first implementation):
 *   READY      no hardware owner; idle timer running; panel lit
 *   ACTIVE     at least one hardware owner is working; timer inhibited
 *   QUIESCING  bounded cleanup inside one poll (lamp, motor outputs, panel)
 *   STANDBY    supported outputs off, panel dark; USB, status, recovery and
 *              wake remain available; any accepted job wakes automatically
 *   WAKING     a job was admitted while asleep; panel relit, owner runs
 *   FAULT      a quiesce readback disagreed; outputs left safe, error kept
 *
 * Everything runs in the polled main loop, so the idle check and the shutdown
 * happen in the same power_poll() call: no job can start in between. Explicit
 * standby while an owner works is refused (BUSY); nothing is cancelled here.
 * Supported shutdown level: scanner analog power/illumination, scanner motor
 * outputs, panel LEDs. Print-side parking, head capping, driver-IC sleep and
 * rail switching are NOT performed and are reported as unsupported.
 *
 * Time base: time_ms() (accumulated millisecond clock, wrap-safe deltas). */
#include "app.h"
#include "print_job.h"

enum power_state { PW_READY, PW_ACTIVE, PW_QUIESCING, PW_STANDBY, PW_WAKING, PW_FAULT };
enum power_reason { RS_BOOT, RS_TIMEOUT, RS_HOST, RS_PANEL, RS_JOB, RS_FAULT };
enum power_error { PE_NONE, PE_SCANNER_POWER, PE_MOTOR_OUTPUT, PE_OWNER_ACTIVE };
enum power_result { PR_NONE, PR_ACCEPTED, PR_BUSY, PR_INVALID, PR_FAULT };

/* Hardware owners (bit per module); any set bit inhibits automatic standby. */
#define OWN_SCAN_MOTOR 0x001u
#define OWN_SCAN_PAGE 0x002u
#define OWN_SCAN_LAMP 0x004u
#define OWN_SCAN_ARMED 0x008u
#define OWN_PAPER 0x010u
#define OWN_CARRIAGE 0x020u
#define OWN_PAGE_JOB 0x040u
#define OWN_STAMP 0x080u
#define OWN_HYBRID 0x100u
#define OWN_CARTRIDGE 0x200u
#define OWN_HEAD_FIRE 0x400u
#define OWN_PRINT_MECH 0x800u
#define OWN_PRINT_RX 0x1000u
#define OWN_RASTER_RX 0x2000u
#define OWN_PRINT_JOB 0x4000u

#define TIMEOUT_DEFAULT_S 60u
#define TIMEOUT_MAX_S 3600u
#define SCANNER_POWER_REG 0xc0c85614u
#define SCAN_MOTOR_C 0xc0c56000u

static enum power_state state;
static enum power_reason reason;
static enum power_error last_error;
static enum power_result last_result;
static unsigned fail_stage;
static uint32_t timeout_s = TIMEOUT_DEFAULT_S;
static uint32_t last_active_ms, owners, transitions, standby_count, wake_count;
static bool panel_dark, dark_logged;

static const char *const state_names[] = {"ready", "active", "quiescing", "standby", "waking", "fault"};

bool app_panel_dark(void) { return panel_dark; }
bool power_asleep(void) { return state == PW_STANDBY; }
unsigned power_state_code(void) { return (unsigned)state; }

static uint32_t collect_owners(void)
{
    uint32_t m = 0;
    if (scanner_motor_busy()) m |= OWN_SCAN_MOTOR;
    if (scan_page_busy()) m |= OWN_SCAN_PAGE;
    if (scanner_lamp_on()) m |= OWN_SCAN_LAMP;
    if (scanner_armed()) m |= OWN_SCAN_ARMED;
    if (paper_feed_busy()) m |= OWN_PAPER;
    if (carriage_busy()) m |= OWN_CARRIAGE;
    if (page_job_busy()) m |= OWN_PAGE_JOB;
    if (stamp_busy()) m |= OWN_STAMP;
    if (hybrid_busy()) m |= OWN_HYBRID;
    if (cartridge_busy()) m |= OWN_CARTRIDGE;
    if (head_fire_busy()) m |= OWN_HEAD_FIRE;
    if (print_mech_lf_busy()) m |= OWN_PRINT_MECH;
    if (print_is_active()) m |= OWN_PRINT_RX;
    if (hybrid_raster_active()) m |= OWN_RASTER_RX;
    if (print_engine_busy()) m |= OWN_PRINT_JOB;
    return m;
}

static void enter(enum power_state s, enum power_reason why)
{
    if (s == state) return;
    state = s; reason = why; ++transitions;
    dbg_puts("[power] "); dbg_puts(state_names[s]);
    dbg_puts(" reason="); dbg_dec((uint32_t)why);
    dbg_puts(" owners=0x"); dbg_u32(owners); dbg_putc('\n');
}

/* Bounded cleanup; every step is idempotent and reversible by the next job.
 * Returns false (and records the stage) when a readback disagrees. */
static bool quiesce(void)
{
    fail_stage = 0; last_error = PE_NONE;
    if (owners) { last_error = PE_OWNER_ACTIVE; fail_stage = 1; return false; }
    scanner_lamp_off();                      /* SCNIO stop + analog power off; ring rows untouched */
    bool motor_idle = scanner_motor_quiesce();   /* outputs neutral when initialised; no inhibit */
    panel_dark = true;
    if (rd(SCANNER_POWER_REG) & 1u) { last_error = PE_SCANNER_POWER; fail_stage = 2; return false; }
    if (!motor_idle) { last_error = PE_MOTOR_OUTPUT; fail_stage = 3; return false; }
    return true;
}

static void go_standby(enum power_reason why)
{
    enter(PW_QUIESCING, why);
    if (quiesce()) { ++standby_count; enter(PW_STANDBY, why); }
    else { dbg_puts("[power] quiesce failed stage "); dbg_dec(fail_stage); dbg_putc('\n'); enter(PW_FAULT, RS_FAULT); }
}

static void relight(void) { panel_dark = false; dark_logged = false; }

void power_init(void)
{
    state = PW_READY; reason = RS_BOOT; owners = 0;
    last_active_ms = time_ms();
    panel_dark = false;
    dbg_puts("[power] ready; idle timeout "); dbg_dec(timeout_s); dbg_puts(" s (USB 0xa0 status, 0xa1 control)\n");
}

void power_poll(void)
{
    uint32_t now = time_ms();
    owners = collect_owners();
    if (owners) {
        last_active_ms = now;
        if (state == PW_STANDBY) { ++wake_count; relight(); enter(PW_WAKING, RS_JOB); }
        if (state == PW_WAKING || state == PW_READY) enter(PW_ACTIVE, RS_JOB);
        return;
    }
    if (state == PW_ACTIVE || state == PW_WAKING) { last_active_ms = now; enter(PW_READY, RS_JOB); return; }
    if (state == PW_READY && timeout_s && now - last_active_ms >= timeout_s * 1000u) go_standby(RS_TIMEOUT);
}

/* Explicit control (USB 0xa1 / panel). Returns true when accepted. */
bool power_request(unsigned op, unsigned arg)
{
    owners = collect_owners();
    switch (op) {
    case POWER_OP_STANDBY:
        if (state == PW_STANDBY) { last_result = PR_ACCEPTED; return true; }
        if (owners) { last_result = PR_BUSY; dbg_puts("[power] standby refused: busy 0x"); dbg_u32(owners); dbg_putc('\n'); return false; }
        go_standby(arg == 1u ? RS_PANEL : RS_HOST);
        last_result = state == PW_STANDBY ? PR_ACCEPTED : PR_FAULT;
        return state == PW_STANDBY;
    case POWER_OP_WAKE:
        relight(); last_active_ms = time_ms();
        if (state == PW_STANDBY) ++wake_count;
        if (state == PW_STANDBY || state == PW_FAULT) enter(PW_READY, arg == 1u ? RS_PANEL : RS_HOST);
        last_result = PR_ACCEPTED; return true;
    case POWER_OP_TIMEOUT:
        if (arg > TIMEOUT_MAX_S) { last_result = PR_INVALID; return false; }
        timeout_s = arg; last_active_ms = time_ms();   /* rebase: a change never cuts straight into standby */
        dbg_puts("[power] idle timeout "); dbg_dec(timeout_s); dbg_puts(" s\n");
        last_result = PR_ACCEPTED; return true;
    default:
        last_result = PR_INVALID; return false;
    }
}

/* Panel Power key: wake when asleep or faulted, otherwise request standby. */
void power_button(void)
{
    if (state == PW_STANDBY || state == PW_FAULT) (void)power_request(POWER_OP_WAKE, 1u);
    else (void)power_request(POWER_OP_STANDBY, 1u);
}

/* USB 0xa0: 16 little-endian words. Read-only; never wakes anything. */
unsigned power_status(uint8_t *out)
{
    uint32_t now = time_ms();
    put32(out, 0x50575231u);                                       /* "PWR1": protocol version 1 */
    put32(out + 4, (uint32_t)state | (uint32_t)reason << 8 | (uint32_t)last_result << 16 | (uint32_t)fail_stage << 24);
    put32(out + 8, timeout_s);
    put32(out + 12, now - last_active_ms);                         /* idle age ms (0 while owned) */
    put32(out + 16, collect_owners());
    put32(out + 20, (uint32_t)last_error);
    put32(out + 24, POWER_CAP_SCANNER | POWER_CAP_SCAN_MOTOR | POWER_CAP_PANEL | POWER_CAP_TIMEOUT);
    put32(out + 28, (rd(SCANNER_POWER_REG) & 1u) | (panel_dark ? 2u : 0u) | (head_if_prepared() ? 4u : 0u) |
                    (scanner_line_ready() ? 8u : 0u) | (paper_feed_holding() ? 16u : 0u));   /* logical output readbacks, not rail voltages */
    put32(out + 32, rd(SCAN_MOTOR_C));
    put32(out + 36, rd(SCAN_MOTOR_C + 8u));
    put32(out + 40, transitions);
    put32(out + 44, standby_count);
    put32(out + 48, wake_count);
    put32(out + 52, now);
    put32(out + 56, 0); put32(out + 60, 0);
    return 64;
}

/* Called from leds_tick(): keep a dark panel dark, log the first dark frame. */
void power_panel_note(void)
{
    if (panel_dark && !dark_logged) { dark_logged = true; dbg_puts("[power] panel dark\n"); }
}
