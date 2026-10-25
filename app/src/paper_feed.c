/* Closed-loop paper feed: pick a sheet, bring it to a position, eject it.
 *
 * Physically established on 2026-10-24 (evidence/PRINT_MECH.md): the paper-feed
 * motor (companion 0xc0c52000, primary 0xc0c53000) picks a sheet from the tray
 * when driven below neutral at about 30 % (command 1400) and transports/ejects
 * it above neutral (2300). The paper-end sensor is GPIO group 7 slot 0, read
 * alias 0xc0c84e08 bit 0: 1 = no paper, 0 = paper present. The trailing edge
 * clears the sensor after roughly 16,600 forward counts from the end of pickup.
 *
 * Stock (fw_dump/gpt/BRIEF_paper_pickup.md answer) picks in reverse: 12,300
 * counts before consulting the sensor, then up to 90,000 counts total until the
 * sensor sees paper, then a mode-dependent continuation past the edge; a miss
 * is followed by an 18,570-count forward swing-arm reset and up to three
 * attempts; alignment is forward (627 units = 3,762 counts, page corrections
 * omitted here); eject runs 21,576 counts past the trailing edge. This routine
 * mirrors those distances with fixed drive commands instead of stock's servo. Everything runs as a
 * non-blocking state machine from the main loop, so USB management, the Cancel
 * key and the USB stop request work throughout. Drive commands are the tested
 * fixed values; no speed control yet. The motor driver IC must have had its
 * shared serial wake (the scanner driver's initialize()) and the LF primary
 * block its init; both are done here on first use with every drive disabled. */
#include "app.h"

#define LF_C 0xc0c52000u
#define LF_P 0xc0c53000u
#define LF_NEUTRAL 2000u
#define LF_PICKUP 1400u            /* 30 % below neutral: pickup train */
#define LF_FORWARD 2300u           /* 15 % above neutral: transport and eject */
#define PE_SENSOR 0xc0c84e08u      /* bit 0: 1 no paper, 0 paper */
#define POS_MASK 0x7ffffu

/* Distances in LF encoder counts, from the stock ASF load records
 * (fw_dump/gpt/BRIEF_paper_pickup.md answer; 1 stock LF unit = 6 counts). */
#define PHASE1_COUNTS 2048u        /* reverse before the sensor is consulted (stock: 12,300; the owner's manual runs reached the sensor at ~8,200 and switched 300..2,600 later) */
#define SEARCH_COUNTS 90000u       /* RAS_LOAD_MAX: total reverse per attempt before giving up */
#define HANDOFF_DEFAULT 7968u      /* stock post-edge pickup travel A = 1328 Ulf (brief 10); was 2048u: a marginal nip handoff */
#define RESET_COUNTS 18570u        /* RAS_SWINGARM_INI: forward reset after a miss */
#define HOME_COUNTS 37140u         /* forward reset from an unknown arm position (2 x RESET_COUNTS); physically needed after an aborted pickup */
#define ADVANCE_DEFAULT 3876u      /* stock forward alignment T = 627 + 19 Ulf (brief 10); was 3762u */
#define EJECT_MARGIN 21576u        /* RAS_EJECT: forward after the trailing edge */
#define EJECT_MAX_COUNTS 60000u    /* sensor still covered after this much forward travel: jam */
#define PICKUP_RETRIES 3u
#define PAUSE_MS 300u
#define ATTEMPT_TIMEOUT_MS 15000u
#define HANDOFF_TIMEOUT_MS 6000u
#define RESET_TIMEOUT_MS 8000u
#define ADVANCE_TIMEOUT_MS 4000u
#define EJECT_TIMEOUT_MS 20000u
#define MARGIN_TIMEOUT_MS 8000u

enum feed_mode { FEED_AND_EJECT = 0, FEED_AND_HOLD = 1, EJECT_ONLY = 2, ADVANCE_ONLY = 3 };
enum feed_state { FS_IDLE = 0, FS_PICKUP, FS_PAUSE, FS_ADVANCE, FS_EJECT, FS_MARGIN, FS_STOP, FS_HANDOFF, FS_SETTLE, FS_RESET, FS_HOME };
enum feed_result { FR_NONE = 0, FR_RUNNING, FR_OK, FR_NO_PAPER, FR_TIMEOUT, FR_CANCELLED, FR_REFUSED, FR_JAM };

static enum feed_state state;
static enum feed_result result;
static enum feed_mode mode;
static bool ready, held;           /* driver woken + LF block initialised; sheet held at position */
static bool settle_to_pickup;      /* FS_SETTLE continues with a pickup retry instead of the advance */
static bool arm_dirty = true;      /* swing-arm position unknown: boot, aborted pickup, manual reverse */
static unsigned manual_dir;        /* manual run: 0 off, 1 reverse, 2 forward */
bool paper_manual_request(unsigned dir);
static unsigned attempts, advance_target, handoff_target, phase1_target;
static uint32_t t_state, t_start, pos_mark, pickup_counts, advance_counts, eject_counts, last_pos;

static uint32_t pos(void) { return rd(LF_P + 0x100u) & POS_MASK; }
static int32_t delta(uint32_t from, uint32_t to)
{ int32_t d = (int32_t)((to - from) & POS_MASK); return d >= (int32_t)(POS_MASK / 2u) ? d - (int32_t)(POS_MASK + 1u) : d; }
static bool pe_last;
static bool paper_present(void)
{
    bool a = !(rd(PE_SENSOR) & 1u), b = !(rd(PE_SENSOR) & 1u);
    if (a == b) pe_last = a;
    return pe_last;
}
static uint32_t ms_since(uint32_t t) { return (app_clock_ticks() - t) / CLOCK_TICKS_PER_MS; }

static void drive(uint32_t command)
{
    wr(LF_C + 8u, LF_NEUTRAL);
    if (!(rd(LF_C) & 0x102u)) { wr(LF_C, 0x102u); delay_ticks(CLOCK_TICKS_PER_MS); }
    wr(LF_C + 8u, command);
}

static void motor_off(void)
{
    wr(LF_C + 8u, LF_NEUTRAL);
    delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    wr(LF_C, 0);
}

static void enter(enum feed_state s)
{
    state = s; t_state = app_clock_ticks(); pos_mark = pos();
    dbg_puts("[feed] state="); dbg_dec(s); dbg_puts(" pos=0x"); dbg_u32(pos_mark);
    dbg_puts(" paper="); dbg_dec(paper_present()); dbg_putc('\n');
}

static void finish(enum feed_result r)
{
    motor_off();
    result = r; state = FS_IDLE;
    dbg_puts("[feed] done result="); dbg_dec(r); dbg_puts(" attempts="); dbg_dec(attempts);
    dbg_puts(" pickup="); dbg_dec(pickup_counts); dbg_puts(" advance="); dbg_dec(advance_counts);
    dbg_puts(" eject="); dbg_dec(eject_counts); dbg_puts(" ms="); dbg_dec(ms_since(t_start)); dbg_putc('\n');
}

bool paper_feed_busy(void) { return state != FS_IDLE || manual_dir != 0; }
unsigned paper_feed_result(void) { return result; }   /* 2 = ok, see enum feed_result */
bool paper_feed_holding(void) { return held; }

/* wValue: bits 0..1 mode, bits 8..15 = first-phase counts / 256 (0 = default).
 * wIndex: low byte = advance counts / 64 (0 = default), high byte = handoff counts / 64 (0 = default). */
bool paper_feed_request(unsigned value, unsigned advance_units)
{
    unsigned m = value & 0xffu;
    if (m > ADVANCE_ONLY || state != FS_IDLE || manual_dir) return false;
    if (scanner_motor_busy() || scan_page_busy() || print_mech_lf_busy() || carriage_busy()) return false;
    if (rd(0xc0c50000u) || rd(LF_C) || rd(0xc0c56000u)) return false;
    if (!ready) {
        if (!scanner_motor_shared_init()) { dbg_puts("[feed] shared setup failed\n"); result = FR_REFUSED; return false; }
        wr(LF_P, 0x0du); wr(LF_P + 4u, 0); wr(LF_P + 8u, 1200u);
        wr(LF_P + 0x100u, 0xf0000u); wr(LF_P + 0x200u, 0xfffffu);
        wr(LF_P + 0x400u, 0); wr(LF_P + 0x404u, 0); wr(LF_P + 0x148u, 1u); wr(LF_P + 0x308u, 1u);
        wr(LF_C, 0); wr(LF_C + 4u, 4000u); wr(LF_C + 8u, LF_NEUTRAL);
        ready = true;
    }
    mode = (enum feed_mode)m;
    advance_target = (advance_units & 0xffu) ? (advance_units & 0xffu) * 64u : ADVANCE_DEFAULT;
    handoff_target = (advance_units >> 8) ? (advance_units >> 8) * 64u : HANDOFF_DEFAULT;
    phase1_target = (value >> 8) ? (value >> 8) * 256u : PHASE1_COUNTS;
    attempts = 0; pickup_counts = advance_counts = eject_counts = 0; settle_to_pickup = false;
    result = FR_RUNNING; t_start = app_clock_ticks(); held = false;
    dbg_puts("[feed] start mode="); dbg_dec(m); dbg_puts(" advance="); dbg_dec(advance_target);
    dbg_puts(" handoff="); dbg_dec(handoff_target); dbg_puts(" phase1="); dbg_dec(phase1_target); dbg_putc('\n');
    if (mode == ADVANCE_ONLY) {                        /* forward by advance_target counts, sensor ignored */
        enter(FS_ADVANCE); drive(LF_FORWARD);
    } else if (mode == EJECT_ONLY) {
        if (!paper_present()) { result = FR_OK; dbg_puts("[feed] eject: no paper at sensor, margin only\n"); }
        enter(FS_EJECT); drive(LF_FORWARD);
    } else if (paper_present()) {
        dbg_puts("[feed] paper already at sensor, skipping pickup\n");
        enter(FS_ADVANCE); drive(LF_FORWARD);
    } else if (arm_dirty && mode != ADVANCE_ONLY) {
        attempts = 1; enter(FS_HOME); drive(LF_FORWARD);   /* bring the swing arm to its rest position first */
    } else {
        attempts = 1; enter(FS_PICKUP); drive(LF_PICKUP);
    }
    if (mode != ADVANCE_ONLY) arm_dirty = true;             /* cleared only by a completed eject */
    return true;
}

void paper_feed_cancel(void)
{
    if (manual_dir) paper_manual_request(0);
    if (state == FS_IDLE) return;
    finish(FR_CANCELLED);
}

void paper_feed_poll(void)
{
    if (state == FS_IDLE) return;
    uint32_t p = pos(); last_pos = p;
    switch (state) {
    case FS_PICKUP: {
        uint32_t reverse = (uint32_t)(-delta(pos_mark, p));
        if (reverse >= phase1_target && paper_present()) {
            pickup_counts += reverse;
            enter(FS_HANDOFF);                         /* keep the pickup command running */
        } else if (reverse >= SEARCH_COUNTS || ms_since(t_state) >= ATTEMPT_TIMEOUT_MS) {
            pickup_counts += reverse;
            wr(LF_C + 8u, LF_NEUTRAL); enter(FS_PAUSE);
        }
        break;
    }
    case FS_PAUSE:                                     /* settle, then the stock swing-arm reset (forward) */
        if (ms_since(t_state) >= PAUSE_MS) { enter(FS_RESET); drive(LF_FORWARD); }
        break;
    case FS_HOME: {
        int32_t d = delta(pos_mark, p);
        if (d > 0 && (uint32_t)d >= HOME_COUNTS) { wr(LF_C + 8u, LF_NEUTRAL); settle_to_pickup = true; enter(FS_SETTLE); }
        else if (ms_since(t_state) >= EJECT_TIMEOUT_MS) finish(FR_TIMEOUT);
        break;
    }
    case FS_RESET: {
        int32_t d = delta(pos_mark, p);
        if (d > 0 && (uint32_t)d >= RESET_COUNTS) {
            wr(LF_C + 8u, LF_NEUTRAL);
            if (attempts >= PICKUP_RETRIES) finish(FR_NO_PAPER);
            else { ++attempts; settle_to_pickup = true; enter(FS_SETTLE); }
        } else if (ms_since(t_state) >= RESET_TIMEOUT_MS) finish(FR_TIMEOUT);
        break;
    }
    case FS_HANDOFF:
        if ((uint32_t)(-delta(pos_mark, p)) >= handoff_target) {
            pickup_counts += (uint32_t)(-delta(pos_mark, p));
            wr(LF_C + 8u, LF_NEUTRAL); enter(FS_SETTLE);
        } else if (ms_since(t_state) >= HANDOFF_TIMEOUT_MS) finish(FR_TIMEOUT);
        break;
    case FS_SETTLE:                                    /* let motion stop before changing direction */
        if (ms_since(t_state) >= PAUSE_MS) {
            if (settle_to_pickup) { settle_to_pickup = false; enter(FS_PICKUP); drive(LF_PICKUP); }
            else { enter(FS_ADVANCE); drive(LF_FORWARD); }
        }
        break;
    case FS_ADVANCE: {
        int32_t d = delta(pos_mark, p);
        advance_counts = d > 0 ? (uint32_t)d : 0u;
        if (advance_counts >= advance_target) {
            if (mode == FEED_AND_HOLD || mode == ADVANCE_ONLY) { held = true; finish(FR_OK); }
            else { enter(FS_EJECT); drive(LF_FORWARD); }
        } else if (ms_since(t_state) >= ADVANCE_TIMEOUT_MS) finish(FR_TIMEOUT);
        break;
    }
    case FS_EJECT: {
        int32_t d = delta(pos_mark, p);
        eject_counts = d > 0 ? (uint32_t)d : 0u;
        if (!paper_present()) { enter(FS_MARGIN); }
        else if (eject_counts >= EJECT_MAX_COUNTS) finish(FR_JAM);
        else if (ms_since(t_state) >= EJECT_TIMEOUT_MS) finish(FR_TIMEOUT);
        break;
    }
    case FS_MARGIN:
        if ((uint32_t)delta(pos_mark, p) >= EJECT_MARGIN) { held = false; arm_dirty = false; finish(FR_OK); }
        else if (ms_since(t_state) >= MARGIN_TIMEOUT_MS) finish(FR_TIMEOUT);
        break;
    default:
        finish(FR_CANCELLED);
    }
}

/* Status: 12 little-endian words. */
unsigned paper_feed_status(uint8_t *out)
{
    put32(out, 0x4446504du);            /* MPFD */
    put32(out + 4, 1);
    put32(out + 8, state);
    put32(out + 12, result);
    put32(out + 16, attempts);
    put32(out + 20, pickup_counts);
    put32(out + 24, advance_counts);
    put32(out + 28, eject_counts);
    put32(out + 32, paper_present());
    put32(out + 36, state == FS_IDLE ? pos() : last_pos);
    put32(out + 40, (held ? 1u : 0u) | (arm_dirty ? 2u : 0u));
    put32(out + 44, state == FS_IDLE ? 0 : ms_since(t_start));
    return 48;
}

/* Manual continuous run of the paper-feed motor (bench): one fixed command until
 * stopped, direction switched, Cancel, or the 10 s auto-stop. USB 0x6b wValue:
 * 0 = stop, 1 = reverse (pickup/"feeder"), 2 = forward (transport). The Copy
 * keys toggle these for the owner's hands-on tests; the emulator uses the USB
 * request. Shares the LF init/driver-wake path and busy gating with the feed. */
#define MANUAL_TIMEOUT_MS 10000u
static uint32_t manual_t0;

static bool lf_prepare_once(void)
{
    if (ready) return true;
    if (rd(0xc0c50000u) || rd(LF_C) || rd(0xc0c56000u)) return false;
    if (!scanner_motor_shared_init()) { dbg_puts("[feed] shared setup failed\n"); return false; }
    wr(LF_P, 0x0du); wr(LF_P + 4u, 0); wr(LF_P + 8u, 1200u);
    wr(LF_P + 0x100u, 0xf0000u); wr(LF_P + 0x200u, 0xfffffu);
    wr(LF_P + 0x400u, 0); wr(LF_P + 0x404u, 0); wr(LF_P + 0x148u, 1u); wr(LF_P + 0x308u, 1u);
    wr(LF_C, 0); wr(LF_C + 4u, 4000u); wr(LF_C + 8u, LF_NEUTRAL);
    ready = true;
    return true;
}

static uint32_t manual_pos0;
static void manual_log(const char *what)
{
    int32_t d = ready ? delta(manual_pos0, pos()) : 0;
    dbg_puts("[feed] manual "); dbg_puts(what); dbg_puts(" after_ms="); dbg_dec(manual_dir ? ms_since(manual_t0) : 0);
    dbg_puts(" counts="); if (d < 0) { dbg_putc('-'); dbg_dec((uint32_t)-d); } else dbg_dec((uint32_t)d);
    dbg_puts(" paper="); dbg_dec(paper_present()); dbg_putc('\n');
}

bool paper_manual_request(unsigned dir)
{
    if (dir > 2u) return false;
    if (dir == 0) {
        if (manual_dir) { if (manual_dir == 2 && delta(manual_pos0, pos()) >= (int32_t)HOME_COUNTS) arm_dirty = false; manual_log("stop"); motor_off(); manual_dir = 0; }
        return true;
    }
    if (state != FS_IDLE || scanner_motor_busy() || scan_page_busy() || print_mech_lf_busy()) return false;
    if (!manual_dir && !lf_prepare_once()) return false;
    if (manual_dir && manual_dir != dir) { manual_log("switch"); wr(LF_C + 8u, LF_NEUTRAL); delay_ticks(CLOCK_TICKS_PER_MS * 300u); }
    if (dir == 1) arm_dirty = true;
    manual_dir = dir; manual_t0 = app_clock_ticks(); manual_pos0 = pos();
    drive(dir == 1 ? LF_PICKUP : LF_FORWARD);
    dbg_puts(dir == 1 ? "[feed] manual reverse (feeder) on" : "[feed] manual forward (transport) on");
    dbg_puts(" paper="); dbg_dec(paper_present()); dbg_putc('\n');
    return true;
}

bool paper_manual_running(void) { return manual_dir != 0; }

void paper_manual_poll(void)
{
    if (manual_dir && ms_since(manual_t0) >= MANUAL_TIMEOUT_MS) {
        if (manual_dir == 2 && delta(manual_pos0, pos()) >= (int32_t)HOME_COUNTS) arm_dirty = false;
        manual_log("auto-stop"); motor_off(); manual_dir = 0;
    }
}

/* Key toggles: pressing the running direction stops; the other direction switches. */
bool paper_manual_toggle(unsigned dir)
{
    if (manual_dir == dir) return paper_manual_request(0);
    return paper_manual_request(dir);
}
