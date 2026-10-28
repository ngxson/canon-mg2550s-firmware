/* Hybrid shape B shim: the services stock maintenance routines expect, so
 * they can run in place from flash under the open app (USB 0x95..0x98).
 *
 * The stock kernel entry points listed in app/hybrid/build.py are replaced
 * at package-build time by Thumb veneers that jump here (ip clobbered, r0-r3
 * and the stack untouched). This module implements, from RE briefs 16/17 and
 * the emulator fixture traces (emu/evidence/stockapp/maintenance):
 *   - event flags, semaphores, message pools and mailboxes (RTOS replacement);
 *   - the mechanism status server on mailbox 0x17 (f008/f009 getters on the
 *     stock RAM banks, f004/f005 masked publications);
 *   - adapters for the carriage task (mailbox 0x19) and line-feed task
 *     (mailbox 0x18): in dry mode they only log and complete the events, in
 *     live mode the carriage adapter drives our carriage driver;
 *   - time: stock ticks are taken as 10 ms (fixture assumption), delays and
 *     waits keep USB management polled;
 *   - stock RAM initialisation: the scatter table's zero-init and decompress
 *     records, minus the pages our own app occupies (0x18xxxxxx aliases our
 *     0x20xxxxxx RAM, measured on hardware 2026-10-26).
 * Everything stock-side is an explicitly tracked intermediate dependency:
 * the routine addresses, message layouts and RAM addresses are stock facts.
 * Nothing here moves a motor unless live mode is requested. */
#include "app.h"
#include "vectors.h"
extern unsigned usb_reattach_count;

/* ---- stock facts (brief 16/17, fixture) ---- */
#define STOCK_DECOMPRESS 0x119acu      /* (src, dst, len): scatter record 0x1ee7d0 */
#define STOCK_CLEAN      0xe8324u      /* (bk_type, cl_type, reserved, mask, context, bookkeeping) */
#define STOCK_WIPE       0xe8668u      /* (0, 1) */
#define STOCK_CAP        0xe7dd6u      /* (2, 1, 1) */
#define STOCK_UNCAP      0xe7a2au      /* (0, 1) */
#define STOCK_INIT_E8610 0xe8610u
#define STOCK_ALLOC_JOB  0xadc14u      /* (job): print-buffer manager allocation (brief 8 §1) */
#define STOCK_RASTER     0xa8422u      /* (packet): raster worker, RB2PB conversion (brief 8 §2) */
#define STOCK_Q_BUILD    0xb12a2u      /* (0): band descriptor builder (brief 7) */
#define STOCK_MEMOP_DONE 0x9f19au      /* RB2PB memory-operation completion handler (brief 8) */
#define STOCK_CONV_DONE  0x9f592u      /* RB2PB conversion completion handler (brief 8) */
#define STOCK_LANE_PREP  0xe6aeau      /* (Q): lane/resolution/registration preparation e3ad6 (brief 11) */
#define STOCK_BAND_EXEC  0xe6b80u      /* (Q): band executor (brief 11) */
#define STOCK_PROFILES_ROM 0x1847c8u   /* CR motion profile table, 14 x 0xf8 bytes -> 0x18089900 */
#define MANAGER6         0x1879b144u
#define MANAGERS         0x1879b09cu   /* seven print-buffer managers, 0x1c each (brief 8): output groups 0..6 */
#define MATRIX           0x1806cd04u
#define SNAPSHOT_FLASH   0x1f2000u     /* app/hybrid/build.py: page table in the erased stock tail; page sources are absolute, pages word-run encoded */
static uint32_t snapshot_pages;
#define STOCK_INIT_C763E 0xc763eu
#define F008_BANK 0x180662ecu
#define F009_BANK 0x18066468u
#define RAM_ALIAS 0x08000000u          /* our 0x20xxxxxx == stock 0x18xxxxxx */

#define MAILBOXES 0x40
#define EVENT_GROUPS 0x40
#define QUEUE_DEPTH 8
#define TICK_MS 10u                    /* fixture assumption: stock delay/countdown unit */
#define TRACE_RECORDS 160

extern uint32_t __bss_end[];
extern void app_entry(void);
void shim_exc_undef(void), shim_exc_pabort(void), shim_exc_dabort(void), shim_exc_irq(void), shim_exc_fiq(void);

static uint32_t events[EVENT_GROUPS];
static uint32_t *mailbox[MAILBOXES][QUEUE_DEPTH];
static unsigned mailbox_count[MAILBOXES];
/* Stock pool allocations (job, print-buffer managers, Q) come from this bump
 * arena. Black mode 1 needs 81 bytes per column + 11 KiB (4224 columns:
 * 349 KB); colour mode 7 (black + C/M/Y at double width) needs 455 bytes per
 * column + 131 KiB, measured in the emulator. The arena lives in our bss,
 * which the stock RAM init skips; stock's zero-initialised RAM up to
 * 0x18420000 has no snapshot page and no fixture ever touched it (LAYOUT). */
static uint8_t arena[1280 * 1024] __attribute__((aligned(8)));   /* 1.25 MiB: a 2112-column mode-7 band (~1.09 MB) plus messages; the on-device print rings need the rest of RAM */
static unsigned arena_used;
/* Band allocations (job, managers, raster storage, Q, pattern buffers) stay
 * live from band-prepare through upload and execution: band_mark is the
 * arena high-water mark at the end of a successful prepare, and every later
 * routine's short-lived messages are allocated above it (presets). Resetting
 * the cursor to zero per routine let executor/status messages overwrite the
 * prepared pattern buffers from the second pass on (brief 22 answer §1). */
static unsigned band_mark;
static bool band_live;
static bool live, motion_only, lf_live, cr_stock, cr_stock_dry, ram_ready, running, fresh_run;
/* Stock paper load (brief 10 / paper_path): mechanism command 0x4011 handler
 * 0xdce50(msg); its LF requests go to the LF task body 0xd9ad0 (one call per
 * message), whose motion is a 1 ms software servo normally run from timer
 * IRQ 1 (0xdbf0c). With IRQs masked the shim calls that handler each
 * millisecond while the timer state says running (u16[0x180c6ede] == 1). */
#define STOCK_LOAD      0xdce50u
#define STOCK_LF_TASK   0xd9ad0u
#define STOCK_LF_TICK   0xdbf0cu
#define LF_TIMER_STATE  0x180c6edcu                    /* u16 mode, u16 running */
/* Stock ISR table (RAM 0x1811bc30, 8 bytes per VIC source, from the boot
 * snapshot): source 12 -> 0xd8b50 services the LF companion event register
 * 0xc0c52104 (enables 0xc0c52100), source 14 -> 0xd8a8e the LF primary event
 * register 0xc0c53404 (enables 0xc0c53400). With IRQs masked the shim polls
 * the enabled status bits and runs those handlers in place. */
#define STOCK_LF_IRQ12  0xd8b50u
#define STOCK_LF_IRQ14  0xd8a8eu
/* Stock CR task body 0xd4820 (mailbox 0x19 receiver): on a 0x4041 move it
 * programs the CR drive, starts timer 2 (0xc0140200, reload 0x1770) whose
 * VIC source 2 handler 0xd5b7a is the CR servo tick, and sequences the pass
 * on events from that tick; source 9 (0xd369a) services the CR primary
 * event register 0xc0c51404 (enables 0xc0c51400). */
#define STOCK_CR_TASK   0xd4820u
#define STOCK_CR_TICK   0xd5b7au
#define STOCK_CR_IRQ9   0xd369au
#define CR_TIMER        0xc0140200u
#define CR_SERVO_ACTIVE 0x1807b12cu                    /* u8 the source-2 tick handler tests first */
/* Head preparation/acquire timing: VIC source 3 -> 0xddfde (e1c82/e1d12/e1e32),
 * timer 3 at 0xc0140300 (reload 0x493e). Stock's owner-3 acquire e19ae(3)
 * waits while head state u8[0x180cd401] == 3; the handler counts expiries,
 * reaches state 2 on the fifth and posts 1b:1 (brief 19 §1). */
#define STOCK_HEAD_TIMER_IRQ 0xddfdeu
#define HEAD_STATE_BYTE 0x180cd401u
#define HEAD_OWNER_BYTE 0x180cd405u
static uint32_t head_tick_last, head_ticks;
static bool cr_running(void) { return *(volatile uint8_t *)CR_SERVO_ACTIVE != 0 || (rd(CR_TIMER) & 1u); }
#define ASF_STATE_BYTE  0x180c6f54u
#define STOCK_CAP_POSITION 2688u                       /* stock CR counts of the capped carriage (cap routine's move) */
static unsigned routine_pending, routine_type, ambient_half, stage_limit;
static unsigned advance_ulf;
static bool advance_active, advance_cancelled, panel_motor_ready;
static uint32_t last_result, routines_run, errors;

/* ---- timing profile (print-speed brief step 1) ----
 * Device time per shim activity, accumulated in clock ticks since boot and
 * read in ms through hybrid_profile_get(); the print engine reports the
 * delta of one job. Only counts, no per-pixel logging. */
enum { HP_CR, HP_LF, HP_WAIT, HP_TIMEOUT, HP_DELAY, HP_RAMINIT, HP_PREPARE, HP_FINISH, HP_DRAIN, HP_N };
static uint32_t prof_msacc[HP_N], prof_rem[HP_N], prof_calls[HP_N], prof_arena_peak, cr_req_t0, lf_req_t0;
static uint32_t prof_routine, prof_rdelay_ms[11], prof_rdelay_calls[11], prof_rwait_ms[11], prof_rtimeouts[11];   /* per running routine 1..10 (0: none) */
/* 32-bit only (no libgcc): whole milliseconds accumulate, the tick remainder carries. */
static void prof_add(unsigned k, uint32_t t0)
{
    prof_rem[k] += app_clock_ticks() - t0; prof_msacc[k] += prof_rem[k] / CLOCK_TICKS_PER_MS; prof_rem[k] %= CLOCK_TICKS_PER_MS; ++prof_calls[k];
}
static uint32_t prof_ms(unsigned k) { return prof_msacc[k]; }
void hybrid_profile_get(struct hybrid_profile *o)
{
    o->cr_ms = prof_ms(HP_CR); o->cr_moves = prof_calls[HP_CR]; o->lf_ms = prof_ms(HP_LF); o->lf_moves = prof_calls[HP_LF];
    o->wait_ms = prof_ms(HP_WAIT); o->waits = prof_calls[HP_WAIT]; o->timeout_ms = prof_ms(HP_TIMEOUT); o->timeouts = prof_calls[HP_TIMEOUT];
    o->delay_ms = prof_ms(HP_DELAY); o->delays = prof_calls[HP_DELAY]; o->raminit_ms = prof_ms(HP_RAMINIT);
    o->prepare_ms = prof_ms(HP_PREPARE); o->finish_ms = prof_ms(HP_FINISH); o->drain_ms = prof_ms(HP_DRAIN); o->arena_peak = prof_arena_peak; o->errors = errors;
}
/* Per-routine split (USB 0xa2 wIndex 3): for routines 1..10 the stock delay() time and calls, and the
 * satisfied event-wait time and timeout count, since boot; the engine reports per-job deltas. */
void hybrid_profile_routines(uint32_t *out)
{
    for (unsigned r = 0; r < 11; ++r) { out[r] = prof_rdelay_ms[r] | prof_rdelay_calls[r] << 20; out[11 + r] = prof_rwait_ms[r] | prof_rtimeouts[r] << 20; }
}

struct trace { uint32_t kind, a, b, c; };
static struct trace trace[TRACE_RECORDS];
static unsigned trace_n;
/* trace kinds */
enum { T_CR_MOVE = 1, T_CR_PROFILE, T_CR_RELEASE, T_LF_MOVE, T_STATUS_GET, T_STATUS_PUB, T_NVM, T_NOTIFY, T_WAIT_TIMEOUT, T_DELAY, T_ROUTINE, T_UNKNOWN };

static uint32_t service_calls, dry_budget = 600;
static void dry_abort(void);
static void note(uint32_t kind, uint32_t a, uint32_t b, uint32_t c)
{
    trace[trace_n % TRACE_RECORDS] = (struct trace){ kind, a, b, c };   /* ring: the last records survive */
    ++trace_n;
}

/* Call a stock routine (Thumb address with bit 0) with up to six arguments.
 * Two things C cannot express, hence assembly:
 *  - every callee-saved register is preserved around the call, because the
 *    stock scatter decompressor (and possibly more) ignore the AAPCS rule;
 *  - stock runs in System mode. Its kernel wrappers (0x150850 and friends)
 *    enter the kernel with `svc #0` and return through `bx lr`; that only
 *    works when the caller's lr is not the banked supervisor lr the exception
 *    overwrites. Supervisor mode keeps a private stack for the stock SVC
 *    handler (registered through the bootloader vector ABI) while the stock
 *    code itself continues on our stack in System mode, IRQ/FIQ masked.
 * Arguments 5 and 6 go on the stack as the AAPCS requires; sp stays 8-aligned. */
static uint32_t saved_svc_sp __attribute__((used));
static volatile uint32_t in_stock_call __attribute__((used));
static uint8_t svc_stack[1024] __attribute__((aligned(8), used));

__attribute__((naked)) static uint32_t stock_call(uint32_t fn, uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3, uint32_t a4, uint32_t a5)
{
    (void)fn; (void)a0; (void)a1; (void)a2; (void)a3; (void)a4; (void)a5;
    /* Re-entrant: the shim calls stock handlers from inside stock's own waits.
     * Frame (from the final sp): [a4, a5] [prev saved_svc_sp] [caller cpsr, sp
     * after the first push] [r4-r11, lr]. Only an outermost call (caller in
     * supervisor mode) installs the private supervisor stack and records
     * saved_svc_sp; every call restores the caller's mode on return. */
    __asm__ volatile(
        "push {r4-r11, lr}\n"          /* 36 bytes; caller's stack args at sp+36 (a3), +40 (a4), +44 (a5) */
        "mrs r4, cpsr\n"
        "mov r5, sp\n"
        "push {r4, r5}\n"              /* 44 */
        "ldr r7, =saved_svc_sp\n"
        "ldr r6, [r7]\n"
        "push {r6}\n"                  /* 48 */
        "and r9, r4, #0x1f\n"
        "cmp r9, #0x13\n"
        "streq r5, [r7]\n"             /* outermost call: remember the supervisor stack */
        "ldr r7, =in_stock_call\n"
        "mov r8, #1\n"
        "str r8, [r7]\n"
        "ldr r10, [sp, #48]\n"         /* a3 */
        "ldr r11, [sp, #52]\n"         /* a4 */
        "ldr r6, [sp, #56]\n"          /* a5 */
        "mov r8, sp\n"
        "ldreq r9, =svc_stack + 1024\n"
        "moveq sp, r9\n"               /* supervisor mode: private stack for the stock SVC handler */
        "msr cpsr_c, #0xdf\n"          /* System mode, IRQ/FIQ masked */
        "mov sp, r8\n"                 /* continue on the caller's stack */
        "sub sp, sp, #8\n"             /* 56 bytes below an 8-aligned entry sp */
        "str r11, [sp]\n"
        "str r6, [sp, #4]\n"
        "mov ip, r0\n"
        "mov r0, r1\n"
        "mov r1, r2\n"
        "mov r2, r3\n"
        "mov r3, r10\n"
        "blx ip\n"                     /* no register survives this; sp is trusted, r0 is the result */
        "add sp, sp, #8\n"
        "pop {r6}\n"                   /* previous saved_svc_sp */
        "pop {r4, r5}\n"               /* caller cpsr, caller sp */
        "ldr r7, =saved_svc_sp\n"
        "str r6, [r7]\n"
        "and r9, r4, #0x1f\n"
        "cmp r9, #0x13\n"
        "ldr r7, =in_stock_call\n"
        "moveq r8, #0\n"
        "movne r8, #1\n"
        "str r8, [r7]\n"
        "msr cpsr_c, r4\n"             /* back to the caller's mode */
        "moveq sp, r5\n"               /* supervisor caller: its stack again */
        "pop {r4-r11, lr}\n"
        "bx lr\n"
        ".ltorg\n");
}

/* Exceptions while stock code runs. The bootloader's default handler for an
 * unregistered vector spins forever, which is what a hung board looks like.
 * These handlers record the exception kind, link register and SPSR, mask every
 * VIC source for an IRQ/FIQ, and return into `stock_call_abort` in System mode,
 * which unwinds to the outermost stock_call's caller with result 0xdead.
 * Outside a stock call they record and spin like the default. Assembly:
 * exception entry/return and banked-register handling cannot be written in C. */
static volatile uint32_t exc_kind __attribute__((used)), exc_lr __attribute__((used)), exc_spsr __attribute__((used)), exc_count __attribute__((used));
/* Persistent exception record (magic 'EXC1', kind, lr, spsr) and heartbeat in
 * the last KiB of DRAM, above the stack base 0x207ff000: untouched by the
 * bootloader's warm reset, the app's bss, the scan ring and stock's RAM
 * (0x2075f000 inside the ring/stock alias broke the band executor). */
#define EXC_RECORD 0x207ffc00u
#define EXC_RECORD_S "0x207ffc00"

void exception_report(void)
{
    volatile uint32_t *r = (volatile uint32_t *)EXC_RECORD;
    if (r[0] == 0x31435845u) {
        dbg_puts("[exc] previous run died: kind="); dbg_dec(r[1]); dbg_puts(" lr=0x"); dbg_u32(r[2]);
        dbg_puts(" spsr=0x"); dbg_u32(r[3]); dbg_putc('\n');
    }
    if (r[4] == 0x31304248u) {                           /* 'HB01': last heartbeat of the previous run */
        dbg_puts("[hb] previous run: loops="); dbg_dec(r[5]); dbg_puts(" usb_reattach="); dbg_dec(r[6]);
        dbg_puts(" section="); for (unsigned i = 0; i < 4; ++i) dbg_putc((char)(r[7] >> (8 * i) & 0xff));
        dbg_puts(" page=0x"); dbg_u32(r[8]); dbg_puts(" line=0x"); dbg_u32(r[9]); dbg_puts(" motor=0x"); dbg_u32(r[10]); dbg_putc('\n');
    }
    r[0] = 0; r[4] = 0;
}

/* Called from the main loop: the persistent heartbeat (survives a warm reset). */
void heartbeat(uint32_t loops, const char *section)
{
    volatile uint32_t *r = (volatile uint32_t *)EXC_RECORD;
    uint32_t tag = 0;
    for (unsigned i = 0; i < 4 && section[i]; ++i) tag |= (uint32_t)(uint8_t)section[i] << (8 * i);
    r[5] = loops; r[6] = usb_reattach_count; r[7] = tag;
    r[8] = scan_page_status_word(); r[9] = scanner_line_status_word(); r[10] = scanner_motor_status_word();
    r[4] = 0x31304248u;
}

__attribute__((naked, used)) static void stock_call_abort(void)
{
    __asm__ volatile(
        "msr cpsr_c, #0xd3\n"
        "ldr r1, =saved_svc_sp\n"
        "ldr sp, [r1]\n"
        "ldr r1, =in_stock_call\n"
        "mov r0, #0\n"
        "str r0, [r1]\n"
        "ldr r0, =0xdead\n"
        "pop {r4-r11, lr}\n"
        "bx lr\n"
        ".ltorg\n");
}

#define EXCEPTION_HANDLER(name, kind, mask_vic) \
__attribute__((naked, used)) void name(void) \
{ \
    __asm__ volatile( \
        "ldr r0, =exc_kind\n  mov r1, #" #kind "\n  str r1, [r0]\n" \
        "ldr r0, =exc_lr\n    str lr, [r0]\n" \
        "ldr r0, =exc_spsr\n  mrs r1, spsr\n  str r1, [r0]\n" \
        "ldr r0, =exc_count\n ldr r1, [r0]\n  add r1, r1, #1\n  str r1, [r0]\n" \
        ".if " #mask_vic "\n" \
        "  ldr r0, =0xc0130000\n  mvn r1, #0\n  mov r2, #15\n" \
        "1: str r1, [r0, #12]\n  add r0, r0, #16\n  subs r2, r2, #1\n  bne 1b\n" \
        "  ldr r0, =0xc0131080\n  ldr r1, [r0]\n  str r1, [r0, #8]\n" \
        ".endif\n" \
        "ldr r0, =in_stock_call\n  ldr r0, [r0]\n  cmp r0, #0\n" \
        "bne 3f\n" \
        /* Outside a stock call: keep the record where a warm reset leaves DRAM \
         * alone (EXC_RECORD, above the scan ring) and reset through the \
         * bootloader; the app logs it at its next boot (exception_report). */ \
        "ldr r0, =" EXC_RECORD_S "\n  ldr r1, =0x31435845\n  str r1, [r0]\n" \
        "mov r1, #" #kind "\n  str r1, [r0, #4]\n  str lr, [r0, #8]\n  mrs r1, spsr\n  str r1, [r0, #12]\n" \
        "msr cpsr_c, #0xd3\n  mov pc, #0\n" \
        "3:\n" \
        "msr spsr_c, #0xdf\n" \
        "ldr lr, =stock_call_abort\n" \
        "movs pc, lr\n" \
        ".ltorg\n"); \
}
EXCEPTION_HANDLER(shim_exc_undef, 1, 0)
EXCEPTION_HANDLER(shim_exc_pabort, 2, 0)
EXCEPTION_HANDLER(shim_exc_dabort, 3, 0)
EXCEPTION_HANDLER(shim_exc_irq, 5, 1)
EXCEPTION_HANDLER(shim_exc_fiq, 6, 1)

/* Dry-run budget exhausted inside stock code: unwind through the exception
 * landing (result 0xdead) instead of returning a failure stock may ignore. */
static void dry_abort(void)
{
    if (!in_stock_call) return;
    dbg_puts("[shim] dry-run budget exhausted: aborting the stock call\n");
    stock_call_abort();
}

static uint32_t w(uint32_t a) { return *(volatile uint32_t *)a; }
static void ww(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

/* Keep the host alive while a stock routine blocks. Management I2C is left
 * alone: stock EEPROM traffic may be using the same controller. */
static void shim_poll(void) { usb_poll(); ippusb_poll(); }
static void recompute_summary(void);

static uint32_t lf_tick_last, lf_ticks, cr_tick_last, cr_ticks;
static bool lf_motion_pending, lf_paper_at_start, cr_motion_pending;
static void poll_hardware_events(void)
{
    if (advance_active && !advance_cancelled &&
        *(volatile uint16_t *)(LF_TIMER_STATE + 2u) == 1u && (rd(PANEL + 0x28u) & 0x20u)) {
        /* Stock timer stop dbef6, then stock neutral/disable d8022. Stop
         * polling the servo before releasing the pending completion wait. */
        advance_cancelled = true;
        stock_call(0xdbef7u, 0, 0, 0, 0, 0, 0);
        stock_call(0xd8023u, 0, 0, 0, 0, 0, 0);
        lf_motion_pending = false;
        events[0x1a] |= 0x2000u; events[0x1d] |= 0x400u;
        dbg_puts("[shim] paper advance cancelled\n");
        return;
    }
    if (*(volatile uint8_t *)HEAD_STATE_BYTE == 3u || (rd(0xc0140300u) & 1u)) {
        uint32_t now = app_clock_ticks();
        if (now - head_tick_last >= 6u * CLOCK_TICKS_PER_MS) {        /* reload 0x493e at 3 MHz: 6.25 ms */
            head_tick_last = now; ++head_ticks;
            stock_call(STOCK_HEAD_TIMER_IRQ | 1u, 0, 0, 0, 0, 0, 0);
        }
    }
    if (cr_stock) {
        static unsigned cr_irq_notes;
        if (cr_running()) {
            uint32_t now = app_clock_ticks();
            if (now - cr_tick_last >= 2u * CLOCK_TICKS_PER_MS) {      /* reload 0x1770 at the LF tick's 3 MHz: 2 ms */
                cr_tick_last = now; ++cr_ticks;
                stock_call(STOCK_CR_TICK | 1u, 0, 0, 0, 0, 0, 0);
            }
        } else if (cr_motion_pending) {
            cr_motion_pending = false; prof_add(HP_CR, cr_req_t0);
            *(volatile uint16_t *)0x180d5784u &= (uint16_t)~0x1000u;
            events[0x1a] |= 0x10u;
            note(T_UNKNOWN, 0x4041, cr_ticks, rd(0xc0c51100u));
            dbg_puts("[shim] CR motion complete ticks="); dbg_dec(cr_ticks); dbg_puts(" raw=0x"); dbg_u32(rd(0xc0c51100u)); dbg_putc('\n');
        }
        if (rd(0xc0c51404u) & rd(0xc0c51400u)) { if (cr_irq_notes < 8) { ++cr_irq_notes; note(T_UNKNOWN, 0xd369a, rd(0xc0c51404u), 0); } stock_call(STOCK_CR_IRQ9 | 1u, 0, 0, 0, 0, 0, 0); }
    }
    /* Completion of a stock LF request: the LF task body returns right after
     * starting the trajectory and stock's task-side completion processing is
     * interrupt/scheduler driven (brief 18 asks for its contract). Until then
     * the host raises the requester's events when the servo timer has stopped:
     * 0x1a:0x2000 done, 0x1d:0x400 accepted, 0x1a:0x200000 edge if the paper
     * sensor saw a sheet arrive during the move. Distances, profiles, the
     * edge stop itself and retries remain stock's. */
    if (lf_live && lf_motion_pending && *(volatile uint16_t *)(LF_TIMER_STATE + 2u) == 0u) {
        lf_motion_pending = false; prof_add(HP_LF, lf_req_t0);
        bool paper = !(rd(0xc0c84e08u) & 1u);
        events[0x1d] |= 0x400u; events[0x1a] |= 0x2000u;
        if (paper && !lf_paper_at_start) events[0x1a] |= 0x200000u;
        note(T_UNKNOWN, 0x4031, paper, lf_ticks);
        dbg_puts("[shim] LF motion complete, paper="); dbg_dec(paper); dbg_puts(" ticks="); dbg_dec(lf_ticks); dbg_putc('\n');
    }
    if (lf_live) {
        /* source 14 (0xd8a8e -> 0xd8b54) services the companion block, source 12 (0xd8b50 -> 0xd8a96) the primary block */
        static unsigned irq_notes;
        if (rd(0xc0c52104u) & rd(0xc0c52100u)) { if (irq_notes < 8) { ++irq_notes; note(T_UNKNOWN, 0xd8a8e, rd(0xc0c52104u), 0); } stock_call(STOCK_LF_IRQ14 | 1u, 0, 0, 0, 0, 0, 0); }
        if (rd(0xc0c53404u) & rd(0xc0c53400u)) { if (irq_notes < 8) { ++irq_notes; note(T_UNKNOWN, 0xd8b50, rd(0xc0c53404u), 0); } stock_call(STOCK_LF_IRQ12 | 1u, 0, 0, 0, 0, 0, 0); }
    }
    /* Stock LF servo iteration (timer IRQ 1 body) while its timer is "running". */
    if (lf_live && *(volatile uint16_t *)(LF_TIMER_STATE + 2u) == 1u) {
        uint32_t now = app_clock_ticks();
        if (now - lf_tick_last >= CLOCK_TICKS_PER_MS) {
            lf_tick_last = now; ++lf_ticks;
            stock_call(STOCK_LF_TICK | 1u, 0, 0, 0, 0, 0, 0);
        }
    }
    /* Head work completion: stock waits for event 0x1c bit 31, raised by its
     * IRQ 0x2f handler after the engine sets 0xc0c29030 bit 0. With IRQs
     * masked we poll the bit and run that stock handler in place; it performs
     * the acknowledge and the work-list bookkeeping and calls our event_set. */
    if (rd(0xc0c29030u) & 1u) stock_call(0xca900u | 1u, 0, 0, 0, 0, 0, 0);
    /* EEPROM transfer completion (event 0x33): the stock driver programmed the
     * I2C controller in place; completion is status bit 0. */
    if (rd(0xc0b20100u) & 1u) events[0x33] |= 0xf01u;
}

/* ---- events ---- */
uint32_t shim_event_wait(uint32_t id, uint32_t mask, uint32_t mode, uint32_t *out, uint32_t timeout)
{
    if (id >= EVENT_GROUPS) return 1;
    if (!live && !lf_live && ++service_calls > dry_budget) { ++errors; note(T_WAIT_TIMEOUT, id, mask, 0xd0); dry_abort(); }   /* dry-run budget */
    /* Dry runs cannot complete head work or sweeps: fail those waits fast. */
    uint32_t limit_ms = live ? 15000u : 300u;
    uint32_t deadline_ms = timeout == 0xffffffffu ? limit_ms : timeout * TICK_MS + 1u;
    if (deadline_ms > limit_ms) deadline_ms = limit_ms;
    uint32_t t0 = app_clock_ticks();
    if (id == 0x1b && (mask & 1u)) {
        /* Owner-3 / head-power acquire wait. Stock's timer-3 owner service
         * would complete the acquisition and clear status bank 0x200 bit 0
         * ("released") that the cold preparation published; without it the
         * CR/LF submission gate 0xebbac refuses every request (brief 17 §4). */
        uint32_t old = w(F008_BANK + 4 * 9);
        ww(F008_BANK + 4 * 9, old & ~1u);
        recompute_summary();
        note(T_STATUS_PUB, 0xf004, 0x200, old & ~1u);
        /* 1b:1 itself comes from stock's timer-3 service (0xddfde) run by the poll
         * while the head state is 3; it also advances the head state to 2 so
         * e19ae(3) proceeds to the real acquire e1984 (brief 19 §1). */
        head_ticks = 0;
        /* The acquire completes within a few timer-3 periods when it works at
         * all; PLACED printed after this wait expired. Cap it per band. */
        if (deadline_ms > 2000u) deadline_ms = 2000u;
    }
    if (id == 0x1c && (mask & 0x80000000u)) {
        /* "Previous head work finished" (posted by stock's completion callback
         * 0xca41e, which the hybrid never reaches: 0xc0c29030 bit 0 stays clear
         * after a band pass). Non-fresh passes wait for it twice before their
         * CR requests and run normally after the timeout (twopass_B_nofresh):
         * cap it, open question for the next brief. */
        if (deadline_ms > 500u) deadline_ms = 500u;
    }
    if ((id == 0x2e || id == 0x2b) && !(events[id] & mask)) {
        /* RB2PB memory operation / conversion: stock waits for its IRQ handler.
         * With IRQs masked, give the engine a moment, then run that handler in
         * place; it acknowledges the hardware and sets this event through us. */
        uint32_t t1 = app_clock_ticks();
        while ((app_clock_ticks() - t1) / CLOCK_TICKS_PER_MS < 5u) shim_poll();
        stock_call((id == 0x2e ? STOCK_MEMOP_DONE : STOCK_CONV_DONE) | 1u, 0, 0, 0, 0, 0, 0);
        note(T_UNKNOWN, 0x2e2b, id, events[id]);
    }
    for (;;) {
        uint32_t have = events[id] & mask;
        if (mode == 0 ? have == mask : have != 0) {
            if (out) *out = events[id];
            prof_add(HP_WAIT, t0); prof_rwait_ms[prof_routine] += (app_clock_ticks() - t0) / CLOCK_TICKS_PER_MS;
            return 0;
        }
        poll_hardware_events(); shim_poll();
        if ((app_clock_ticks() - t0) / CLOCK_TICKS_PER_MS > deadline_ms) {
            ++errors; note(T_WAIT_TIMEOUT, id, mask, mode); prof_add(HP_TIMEOUT, t0); ++prof_rtimeouts[prof_routine];
            dbg_puts("[shim] event wait timeout id=0x"); dbg_u32(id); dbg_puts(" mask=0x"); dbg_u32(mask); dbg_putc('\n');
            if (out) *out = events[id];
            return 1;
        }
    }
}
uint32_t shim_event_set(uint32_t id, uint32_t mask) { if (id < EVENT_GROUPS) events[id] |= mask; return 0; }
uint32_t shim_event_clear(uint32_t id, uint32_t preserve) { if (id < EVENT_GROUPS) events[id] &= preserve; return 0; }
/* Counting semaphores start empty: a poll (timeout 0) reports -50 so stock's
 * drain loops (brief 8: 0xa03d6 drains semaphore 0x1d) terminate; a blocking
 * wait succeeds at once. */
uint32_t shim_sem_wait(uint32_t id, uint32_t timeout) { (void)id; return timeout == 0 ? 0xffffffceu : 0; }
uint32_t shim_sem_release(uint32_t id) { (void)id; return 0; }

/* ---- pools: bump allocator, reset per routine ---- */
static uint32_t *take(uint32_t size)
{
    size = ((size + 7u) & ~7u) + 64u;   /* + 64: room to align a manager's raster storage (manager_raster) */
    if (arena_used + size > sizeof arena) { ++errors; dbg_puts("[shim] arena exhausted\n"); return 0; }
    uint32_t *p = (uint32_t *)(arena + arena_used); arena_used += size;
    for (unsigned i = 0; i < size / 4; ++i) p[i] = 0;
    return p;
}
uint32_t shim_pool_alloc(uint32_t *out, uint32_t pool, uint32_t size)
{
    uint32_t *p = take(size);
    if (!p) return 1;
    p[1] = pool; ((uint16_t *)p)[4] = (uint16_t)size;   /* +4 pool, +8 length (brief 17 §1) */
    *out = (uint32_t)p; return 0;
}
uint32_t shim_pool_alloc_wait(uint32_t pool, uint32_t size, uint32_t *out, uint32_t timeout)
{ (void)timeout; return shim_pool_alloc(out, pool, size); }
uint32_t shim_pool_free(void *msg, uint32_t b) { (void)msg; (void)b; return 0; }
uint32_t shim_free_wrapper(uint32_t *p) { p[0] = 0; return 0; }   /* 0x89544: clears *p, frees p[1] */

/* ---- mailboxes ---- */
static bool enqueue(uint32_t id, uint32_t *msg)
{
    if (id >= MAILBOXES || mailbox_count[id] >= QUEUE_DEPTH) { ++errors; return false; }
    mailbox[id][mailbox_count[id]++] = msg; return true;
}
uint32_t shim_mbx_receive(uint32_t id, uint32_t *out, uint32_t timeout)
{
    (void)timeout;
    if (id >= MAILBOXES || !mailbox_count[id]) { ++errors; dbg_puts("[shim] receive on empty mailbox 0x"); dbg_u32(id); dbg_putc('\n'); return 1; }
    *out = (uint32_t)mailbox[id][0];
    for (unsigned i = 1; i < mailbox_count[id]; ++i) mailbox[id][i - 1] = mailbox[id][i];
    --mailbox_count[id]; return 0;
}

static void recompute_summary(void)
{
    uint32_t summary = 0;
    if (w(F008_BANK + 4 * 9) || w(F008_BANK + 4 * 10)) summary |= 1u;
    if (w(F008_BANK + 4 * 6) || w(F008_BANK + 4 * 7) || w(F008_BANK + 4 * 8)) summary |= 2u;
    if (w(F008_BANK + 4 * 11)) summary |= 4u;
    ww(F008_BANK + 4 * 5, summary);
}

/* Status server, STMMainTask on mailbox 0x17 (brief 17 §1-3). */
static void status_server(uint32_t *m)
{
    uint32_t control = m[9], command = control & 0xffffu, selector = m[10];
    int index = -1;
    for (int i = 0; i < 32; ++i) if (selector >> i & 1u) { index = i; break; }
    uint32_t result = 1;
    if ((command == 0xf008u || command == 0xf009u) && index >= 0) {
        uint32_t bank = command == 0xf008u ? F008_BANK : F009_BANK;
        m[11] = w(bank + 4u * index);
        note(T_STATUS_GET, command, selector, m[11]);
    } else if ((command == 0xf004u || command == 0xf005u) && index >= 0) {
        uint32_t bank = command == 0xf004u ? F008_BANK : F009_BANK;
        uint32_t preserve = m[11], replacement = m[12], old = w(bank + 4u * index);
        uint32_t value = (old & preserve) | (replacement & ~preserve);
        ww(bank + 4u * index, value);
        if (command == 0xf004u) recompute_summary();
        note(T_STATUS_PUB, command, selector, value);
    } else { result = 2; note(T_UNKNOWN, 0x17, command, selector); }
    if (!(control & 0x00200000u)) return;              /* no reply requested */
    m[9] = (control & ~0x00200000u & ~0x07000000u) | 0x00400000u | (result << 24);
    enqueue(m[4], m);                                   /* reply mailbox at +0x10 */
}

/* Carriage adapter. Stock CR units are encoder counts from the stock origin;
 * the offset to our park-wall origin is unmeasured, so live mode only accepts
 * targets the caller has mapped through `cr_offset` (counts to subtract). */
static int32_t cr_offset = 0;
static uint32_t cr_position = 2688;                     /* stock units, software notion */
static void cr_request_done(void);

static void cr_move(int32_t target, uint32_t profile)
{
    note(T_CR_MOVE, (uint32_t)target, profile, (live ? 1u : 0u) | (motion_only ? 2u : 0u));
    dbg_puts("[shim] CR move target="); dbg_dec((uint32_t)target); dbg_puts(" profile=0x"); dbg_u32(profile); dbg_puts(live ? " live\n" : " dry\n");
    if (live && carriage_referenced()) {
        if (motion_only) {
            /* Sweep without firing: raster engine start and CR event mask off, no owner acquire. */
            *(volatile uint32_t *)0xc0c28000u = 0; *(volatile uint32_t *)0xc0c51400u = 0;
        } else { head_block_init(); head_if_prepare(); head_if_acquire(); }
        if (carriage_move_request(target - cr_offset, 0)) {
            uint32_t t0 = app_clock_ticks();
            while (carriage_busy()) { carriage_poll(); shim_poll(); }
            prof_add(HP_CR, t0);
        } else ++errors;
        if (!motion_only) head_if_release();
        dbg_puts("[shim] CR move done x="); dbg_dec((uint32_t)carriage_position()); dbg_putc('\n');
    }
    cr_position = (uint32_t)target;
    cr_request_done();
}

/* What the stock carriage task's completion leaves behind, as far as the
 * executor's readiness step 0xd305c checks it: u16[0x180d5784] bit 12
 * ("request armed", set by 0xd30aa when a move is prepared) cleared, CR done
 * event raised. Stock's full completion bookkeeping is larger (brief 11 §5). */
static void cr_request_done(void)
{
    *(volatile uint16_t *)0x180d5784u &= (uint16_t)~0x1000u;
    events[0x1a] |= 0x10u;
}

static void carriage_task(uint32_t *m)
{
    if (cr_stock && (live || cr_stock_dry)) {
        uint32_t command = m[9] & 0xffffu;
        if (command == 0x4041u) note(T_CR_MOVE, m[10], m[11], 5u);
        else if (command == 0x4043u) note(T_CR_PROFILE, m[10], ((uint8_t *)m)[0x30], m[13]);
        else if (command == 0x4042u) note(T_CR_RELEASE, 0, 0, 1);
        else note(T_UNKNOWN, 0x19, command, m[10]);
        dbg_puts("[shim] CR request 0x"); dbg_u32(command); dbg_puts(" -> stock CR task; 5784=0x"); dbg_u32(*(volatile uint16_t *)0x180d5784u);
        dbg_puts(" 965a="); dbg_dec(*(volatile uint8_t *)0x1808965au); dbg_puts(" 578e="); dbg_dec(*(volatile uint8_t *)0x180d578eu); dbg_puts(" raw=0x"); dbg_u32(rd(0xc0c51100u));
        dbg_puts(" cd6b6="); dbg_dec(stock_call(0xcd6b6u | 1u, 0, 0, 0, 0, 0, 0)); dbg_putc('\n');
        if (command == 0x4041u && live && motion_only) *(volatile uint32_t *)0xc0c28000u = 0;
        if (command == 0x4041u) {                       /* brief 19 §2 checkpoints before the sweep */
            dbg_puts("[shim] head state="); dbg_dec(*(volatile uint8_t *)HEAD_STATE_BYTE); dbg_puts(" owner="); dbg_dec(*(volatile uint8_t *)HEAD_OWNER_BYTE);
            dbg_puts(" 87000="); dbg_dec(rd(0xc0c87000u)); dbg_puts(" 812c0=0x"); dbg_u32(rd(0xc0c812c0u)); dbg_puts(" 295f0="); dbg_dec(rd(0xc0c295f0u));
            dbg_puts(" 51400=0x"); dbg_u32(rd(0xc0c51400u)); dbg_puts(" 28000="); dbg_dec(rd(0xc0c28000u)); dbg_puts(" 28080="); dbg_dec(rd(0xc0c28080u));
            dbg_puts(" 51d04="); dbg_dec(rd(0xc0c51d04u)); dbg_puts(" 51d0c="); dbg_dec(rd(0xc0c51d0cu)); dbg_puts(" headticks="); dbg_dec(head_ticks); dbg_putc('\n');
        }
        if (!enqueue(0x19, m)) return;
        cr_tick_last = cr_req_t0 = app_clock_ticks(); cr_ticks = 0;
        uint32_t r = stock_call(STOCK_CR_TASK | 1u, 0, 0, 0, 0, 0, 0);
        dbg_puts("[shim] CR task body -> "); dbg_dec(r); dbg_puts(" timer=0x"); dbg_u32(rd(CR_TIMER)); dbg_puts(" active="); dbg_dec(*(volatile uint8_t *)CR_SERVO_ACTIVE);
        dbg_puts(" drive=0x"); dbg_u32(rd(0xc0c50000u)); dbg_puts(" cmd="); dbg_dec(rd(0xc0c50008u)); dbg_puts(" ev1d=0x"); dbg_u32(events[0x1d]); dbg_puts(" raw=0x"); dbg_u32(rd(0xc0c51100u)); dbg_puts(" ticks="); dbg_dec(cr_ticks); dbg_putc('\n');
        if (command == 0x4041u) { cr_motion_pending = true; cr_position = m[10]; }
        else if (command == 0x4043u) events[0x1a] |= 0x10u;
        else if (command == 0x4042u) { *(volatile uint16_t *)0x180d5784u &= (uint16_t)~0x1000u; events[0x1a] |= 0x20u; }
        return;
    }
    uint32_t command = m[9] & 0xffffu;
    if (command == 0x4041u) cr_move((int32_t)m[10], m[11]);
    else if (command == 0x4043u) {
        note(T_CR_PROFILE, m[10], ((uint8_t *)m)[0x30], m[13]);
        dbg_puts("[shim] CR profile motion kind=0x"); dbg_u32(m[10]); dbg_putc('\n');
        events[0x1a] |= 0x10u;                          /* dry and live: no model of the profile motion yet; the
                                                         * "request armed" bit stays as stock left it (the head-work
                                                         * completion loop 0xc87f0 tests it after this event) */
    } else if (command == 0x4042u) {
        note(T_CR_RELEASE, 0, 0, 0);
        dbg_puts("[shim] CR release\n");
        *(volatile uint16_t *)0x180d5784u &= (uint16_t)~0x1000u;
        events[0x1a] |= 0x20u;
    } else note(T_UNKNOWN, 0x19, command, m[10]);
}

static void linefeed_task(uint32_t *m)
{
    uint32_t command = m[9] & 0xffffu;
    if (command == 0x4031u) {
        note(T_LF_MOVE, m[10], m[11], m[13]);
        dbg_puts("[shim] LF move ulf="); dbg_dec(m[10]); dbg_puts(" dir="); dbg_dec(((uint8_t *)m)[0x30]); dbg_puts(" profile=0x"); dbg_u32(m[13]); dbg_puts(lf_live ? " stock LF task\n" : " dry\n");
    } else { note(T_UNKNOWN, 0x18, command, m[10]); dbg_puts("[shim] LF command 0x"); dbg_u32(command); dbg_putc('\n'); }
    if (lf_live) {
        /* Hand the message to stock's LF task body: it receives it through our
         * mailbox, converts units, programs the controller and starts its timer;
         * completion (event 0x1a:0x2000 / edge 0x200000) comes from the servo
         * iterations the hardware-event poll runs. */
        if (!enqueue(0x18, m)) return;
        lf_tick_last = lf_req_t0 = app_clock_ticks(); lf_ticks = 0;
        lf_paper_at_start = !(rd(0xc0c84e08u) & 1u);
        uint32_t r = stock_call(STOCK_LF_TASK | 1u, 0, 0, 0, 0, 0, 0);
        dbg_puts("[shim] LF task body -> "); dbg_dec(r); dbg_puts(" running="); dbg_dec(*(volatile uint16_t *)(LF_TIMER_STATE + 2u)); dbg_putc('\n');
        if (command == 0x4031u) lf_motion_pending = true;   /* completion raised by the poll once the servo timer stops */
        else events[0x1a] |= 0x2000u;                       /* 0x4030 init and others: done */
        return;
    }
    if (command == 0x4031u) {
        events[0x1d] |= 0x400u;                         /* accepted */
        events[0x1a] |= 0x2000u;                        /* complete: dry, no motion */
    } else events[0x1a] |= 0x2000u;                     /* 0x4030 init and others: report done */
}

/* Stock eject: stock's own LF request builder 0xd614c(L, P, D, 0x20) with
 * RAS_EJECT = 3596 Ulf, profile 7, direction 0 (brief paper_path §7),
 * repeated until the paper-end sensor clears, then one more for the margin;
 * each completion through 0xd707c (event 0x1a:0x2000). Preceded by the LF
 * init exchange 0xd6ff0 and the resource acquire 0xe97f0 as the loader does. */
#define STOCK_LF_REQUEST 0xd614cu
#define STOCK_LF_WAIT    0xd707cu
#define STOCK_LF_INIT    0xd6ff0u
#define STOCK_RESOURCE   0xe97f0u
static uint32_t feed_eject(void)
{
    *(volatile uint16_t *)0x180d571eu = 1;
    lf_motion_pending = false;
    stock_call(STOCK_RESOURCE | 1u, 0, 0, 0, 0, 0, 0);
    stock_call(STOCK_LF_INIT | 1u, 0, 0, 0, 0, 0, 0);
    unsigned moves = 0; bool cleared = false;
    for (; moves < 14; ++moves) {
        uint32_t r = stock_call(STOCK_LF_REQUEST | 1u, 3596, 7, 0, 0x20, 0, 0);
        stock_call(STOCK_LF_WAIT | 1u, 0, 0, 0, 0, 0, 0);
        bool paper = !(rd(0xc0c84e08u) & 1u);
        dbg_puts("[shim] eject move "); dbg_dec(moves); dbg_puts(" request->"); dbg_dec(r); dbg_puts(" paper="); dbg_dec(paper); dbg_putc('\n');
        if (cleared) { ++moves; break; }                   /* one margin move after the sensor cleared */
        if (!paper) cleared = true;
    }
    note(T_UNKNOWN, 0xd614c, moves, cleared);
    *(volatile uint8_t *)ASF_STATE_BYTE = 0;
    return cleared ? 0 : 0xbad9;
}

/* One bounded forward paper step through the same stock LF builder, task
 * and servo as eject. This is relative travel, not a new pickup or an
 * assumed first-print position. USB routine 10 supplies 1..1200 Ulf in
 * wIndex; 1 Ulf is six stock controller counts. No firing is submitted. */
static uint32_t feed_advance(void)
{
    if (lf_live && (rd(0xc0c84e08u) & 1u)) return 0xbada; /* require paper */
    *(volatile uint16_t *)0x180d571eu = 1;
    lf_motion_pending = false;
    stock_call(STOCK_RESOURCE | 1u, 0, 0, 0, 0, 0, 0);
    stock_call(STOCK_LF_INIT | 1u, 0, 0, 0, 0, 0, 0);
    uint32_t before = rd(0xc0c53100u);
    if (rd(PANEL + 0x28u) & 0x20u) return 0xbadb;
    advance_cancelled = false; advance_active = true;
    uint32_t r = stock_call(STOCK_LF_REQUEST | 1u, advance_ulf, 6, 0, 0x20, 0, 0);
    stock_call(STOCK_LF_WAIT | 1u, 0, 0, 0, 0, 0, 0);
    advance_active = false;
    dbg_puts("[shim] advance ulf="); dbg_dec(advance_ulf);
    dbg_puts(" raw before=0x"); dbg_u32(before);
    dbg_puts(" after=0x"); dbg_u32(rd(0xc0c53100u));
    dbg_puts(" paper="); dbg_dec(!(rd(0xc0c84e08u) & 1u)); dbg_putc('\n');
    return advance_cancelled ? 0xbadbu : r;
}

/* Stock paper load: mechanism request 0x4011 with the ordinary-mode fields
 * (mode/submode 0, P = 0, dimension 3661 selects record 0x186a70: A = 1328
 * Ulf, profiles 11 -> 12, alignment profile 6, brief paper_path §example). */
static uint32_t feed_msg[0x80 / 4] __attribute__((aligned(8)));
static uint32_t feed_load(void)
{
    for (unsigned i = 0; i < sizeof feed_msg / 4; ++i) feed_msg[i] = 0;
    feed_msg[9] = 0x4011u;
    *(volatile uint16_t *)((uint8_t *)feed_msg + 0x4a) = 3661u;
    *(volatile uint8_t *)ASF_STATE_BYTE = 0;            /* no sheet loaded */
    lf_motion_pending = false;
    /* Resource coordination 0xe97f0: while u16[0x180d571e] (95 in the boot
     * snapshot) is nonzero it runs its acquire step 0xc6252 once per 350 ms
     * iteration; zero skips the acquire altogether and every later LF/CR
     * submission is refused by the "released" gate 0xebbac. One iteration. */
    *(volatile uint16_t *)0x180d571eu = 1;
    lf_ticks = 0;
    uint32_t r = stock_call(STOCK_LOAD | 1u, (uint32_t)feed_msg, 0, 0, 0, 0, 0);
    note(T_UNKNOWN, STOCK_LOAD, r, lf_ticks);
    dbg_puts("[shim] stock load -> "); dbg_dec(r); dbg_puts(" servo ticks="); dbg_dec(lf_ticks); dbg_puts(" asf="); dbg_dec(*(volatile uint8_t *)ASF_STATE_BYTE); dbg_putc('\n');
    return r;
}

static void dispatch(uint32_t id, uint32_t *m)
{
    switch (id) {
    case 0x17: status_server(m); break;
    case 0x19: carriage_task(m); break;
    case 0x18: linefeed_task(m); break;
    case 0x02: note(T_NVM, m[7] & 0xffffffu, m[11], m[12]);   /* opcode, EEPROM offset, length (brief 17 §6) */
               dbg_puts("[shim] NVM write offset=0x"); dbg_u32(m[11]); dbg_puts(" len="); dbg_dec(m[12]); dbg_puts(" (dropped)\n"); break;
    case 0x07: note(T_NOTIFY, m[9] & 0xffffu, m[10], 0); break;
    default:   enqueue(id, m); break;                   /* replies and anything we do not model */
    }
}

uint32_t shim_mbx_send_raw(uint32_t id, uint32_t *msg) { if (msg) dispatch(id, msg); return 0; }
uint32_t shim_mbx_send(uint32_t id, uint32_t *msg)
{
    if (!msg) return 1;
    msg[0] = 0; msg[5] = 0x40; msg[6] = id;             /* link, sender task (ours), destination */
    dispatch(id, msg); return 0;
}

/* ---- time and kernel queries ---- */
uint32_t shim_delay(uint32_t ticks)
{
    note(T_DELAY, ticks, 0, 0);
    /* Readiness countdown u16[0x180d5720] (brief 16): a stock periodic service
     * decrements it; the fixture models one decrement per delay, so do we.
     * Stock's resource coordination (0xe97b2) and maintenance pre-phase loops
     * poll it to zero between delays. */
    { volatile uint16_t *cd = (volatile uint16_t *)0x180d5720u; if (*cd) --*cd; }
    if (!live && !lf_live && ++service_calls > dry_budget) { ++errors; dry_abort(); }
    uint32_t t0 = app_clock_ticks(), ms = ticks * TICK_MS;
    if (ms > 3000u) ms = 3000u;
    while ((app_clock_ticks() - t0) / CLOCK_TICKS_PER_MS < ms) { poll_hardware_events(); shim_poll(); }
    prof_add(HP_DELAY, t0); prof_rdelay_ms[prof_routine] += ms; ++prof_rdelay_calls[prof_routine];
    return 0;
}
uint32_t shim_kquery(uint32_t what, uint32_t *out) { (void)what; if (out) out[0] = 0; return 0; }
uint32_t shim_task_id(uint32_t *out) { if (out) *out = 0x40; return 0; }
uint32_t shim_kernel_unknown(uint32_t a, uint32_t b, uint32_t c, uint32_t d) { note(T_UNKNOWN, 0xffff, a, b); (void)c; (void)d; return 0; }
/* NVM manager stand-in. Stock's logical-field API wrappers do
 * `obj = manager(); vtable = *obj; vtable[slot](obj, args...)`. Until
 * persistence is implemented every slot lands in one logged method that
 * returns 0 (reads: field value 0, writes: success). */
static uint32_t shim_nvm_vcall(uint32_t self, uint32_t a, uint32_t b, uint32_t c)
{
    (void)self; note(T_NVM, 0xffffffffu, a, b); (void)c;
    return 0;
}
static uint32_t nvm_vtable[64];
#define NVM_MANAGER_A 0x1878ff34u   /* returned by stock getter 0x111a14 */
#define NVM_MANAGER_B 0x1878ff3cu   /* returned by stock getter 0x111984 */
/* System services singleton returned by stock getter 0x126098: interface
 * pointers at +0x2c, +0x30, +0x34, ... (callers 0x1260ec/f4/fc load one and
 * call a virtual method on it). Stock constructors fill them; the shim points
 * every slot at one object whose methods all land in shim_nvm_vcall. */
#define SYSTEM_SINGLETON 0x18798744u
static uint32_t generic_object[4];
static void install_nvm_objects(void)
{
    for (unsigned i = 0; i < 64; ++i) nvm_vtable[i] = (uint32_t)shim_nvm_vcall;
    ww(NVM_MANAGER_A, (uint32_t)nvm_vtable);
    ww(NVM_MANAGER_B, (uint32_t)nvm_vtable);
    generic_object[0] = (uint32_t)nvm_vtable;
    for (unsigned i = 0; i < 16; ++i) ww(SYSTEM_SINGLETON + 0x2c + 4u * i, (uint32_t)generic_object);
}
/* Kernel privilege/cache helper bodies (0x150084/94/a4/b4, 0x1500d8): no-ops. */
uint32_t shim_noop(uint32_t a, uint32_t b, uint32_t c, uint32_t d) { (void)a; (void)b; (void)c; (void)d; return 0; }
/* 0x97f7c: stock status-change notification (key, value, kind, extra). The open
 * app owns the host interface, so the record is only logged. */
uint32_t shim_notify(uint32_t key, uint32_t value, uint32_t kind, uint32_t extra)
{
    note(T_NOTIFY, key, value, kind); (void)extra;
    dbg_puts("[shim] notify key=0x"); dbg_u32(key); dbg_puts(" value=0x"); dbg_u32(value); dbg_putc('\n');
    return 0;
}

/* ---- stock RAM initialisation ---- */
static void install_nvm_objects(void);
static void zero_range(uint32_t lo, uint32_t hi)
{
    for (volatile uint32_t *p = (uint32_t *)lo; p < (uint32_t *)hi; ++p) *p = 0;
}

/* Stock code enters its kernel through `svc #0` with r0 = the routine to run
 * in supervisor mode (0x150850 masks IRQs and returns the old mask, 0x150870
 * restores it; the handler at 0x11858 simply branches to r0 for svc #0). Our
 * app already runs in supervisor mode with IRQs masked, so the stock handler
 * itself is registered as the SVC target through the bootloader's vector ABI:
 * it needs no RAM of its own for the svc #0 path. */
#define STOCK_SVC_HANDLER 0x11858u
static bool svc_ready;

static bool register_stock_svc(void)
{
    /* The API block lives at a fixed flash address; go through a volatile
     * pointer variable so GCC does not treat the constant as a null-page object. */
    volatile uintptr_t api_address = BOOT_VECTOR_API_ADDRESS;
    const volatile struct boot_vector_api *api = (const volatile struct boot_vector_api *)api_address;
    if (api->magic != BOOT_VECTOR_API_MAGIC || api->version != BOOT_VECTOR_API_VERSION || api->bytes != sizeof *api) return false;
    uint32_t targets[BOOT_VECTOR_COUNT] = { 0 };
    targets[BOOT_VECTOR_SVC] = STOCK_SVC_HANDLER;
    targets[BOOT_VECTOR_UNDEF] = (uint32_t)shim_exc_undef;
    targets[BOOT_VECTOR_PREFETCH_ABORT] = (uint32_t)shim_exc_pabort;
    targets[BOOT_VECTOR_DATA_ABORT] = (uint32_t)shim_exc_dabort;
    targets[BOOT_VECTOR_IRQ] = (uint32_t)shim_exc_irq;
    targets[BOOT_VECTOR_FIQ] = (uint32_t)shim_exc_fiq;
    svc_ready = api->install(targets);
    dbg_puts(svc_ready ? "[shim] stock SVC handler registered\n" : "[shim] SVC registration refused\n");
    return svc_ready;
}

/* Diagnostic (sheet CT0142: the first pass after a snapshot restore prints
 * solid black without the 32-row white lines, every later pass has them):
 * re-copy the snapshot pages that fall inside one of these ranges before a
 * non-fresh prepare, to find which stock state the first pass leaves behind.
 * 0x9b wIndex=4 selects the range set (bit mask). */
static const uint32_t restore_ranges[][2] = {
    {0x180cd000u, 0x180ce000u},   /* bit 0: lane records 0x180cd3a0.., scheduler A/C blocks, previous masks */
    {0x1806b000u, 0x1806e000u},   /* bit 1: builder lane block, selection matrix */
    {0x18070000u, 0x18077000u},   /* bit 2: 0x18070f86 / 0x18076204 (brief 20 transition state, feed carry) */
    {0x1802e000u, 0x18030000u},   /* bit 3: 0x1802ef84 previous-state byte and neighbours */
    {0x180d0000u, 0x180d6000u},   /* bit 4: band-task flags 0x180d0320, LF/CR request state */
    {0x18079000u, 0x1807c000u},   /* bit 5: head work lists, profiles (0x18079e54, 0x1807acac) */
};
static uint32_t restore_mask;
static bool snapshot_present(void) { return w(SNAPSHOT_FLASH) == 0x50414e53u && w(SNAPSHOT_FLASH + 12) == 1u; }
/* One snapshot page from flash (app/hybrid/build.py zero_rle): byte token t,
 * t & 0x80 = (t & 0x7f) + 1 zero words, else that many literal words follow
 * (unaligned in flash, so they are read bytewise). */
static void snapshot_copy(uint32_t dst, uint32_t src, uint32_t len)
{
    for (uint32_t end = dst + len; dst < end;) {
        unsigned t = *(volatile uint8_t *)src++, n = (t & 0x7fu) + 1u;
        if (t & 0x80u) { while (n--) { ww(dst, 0); dst += 4; } continue; }
        while (n--) {
            const volatile uint8_t *b = (const volatile uint8_t *)src;
            ww(dst, (uint32_t)b[0] | (uint32_t)b[1] << 8 | (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24);
            src += 4; dst += 4;
        }
    }
}
static void snapshot_restore_ranges(uint32_t mask)
{
    if (!mask || !snapshot_present()) return;
    uint32_t n = w(SNAPSHOT_FLASH + 4), done = 0;
    for (uint32_t i = 0; i < n; ++i) {
        uint32_t dst = w(SNAPSHOT_FLASH + 16 + 12 * i), src = w(SNAPSHOT_FLASH + 20 + 12 * i), len = w(SNAPSHOT_FLASH + 24 + 12 * i);
        bool hit = false;
        for (unsigned r = 0; r < sizeof restore_ranges / sizeof restore_ranges[0]; ++r)
            if ((mask >> r & 1u) && dst < restore_ranges[r][1] && dst + len > restore_ranges[r][0]) hit = true;
        if (!hit) continue;
        snapshot_copy(dst, src, len);
        ++done;
    }
    dbg_puts("[shim] snapshot ranges restored, mask=0x"); dbg_u32(mask); dbg_puts(" pages="); dbg_dec(done); dbg_putc('\n');
}

static void stock_ram_init(void)
{
    uint32_t app_lo = ((uint32_t)app_entry - RAM_ALIAS) & ~0xfffu;
    uint32_t app_hi = ((uint32_t)__bss_end - RAM_ALIAS + 0xfffu) & ~0xfffu;
    /* Scatter table 0x1ee7d0: zero-init 0x18032bf8 + 0x768e94, skipping our app and stack. */
    zero_range(0x18032bf8u, app_lo);
    zero_range(app_hi, 0x1879c000u);
    /* 0xe0000000: 0x58 bytes copied from 0x1f1c50, then 0x2fc zeroed. */
    for (unsigned i = 0; i < 0x58; i += 4) ww(0xe0000000u + i, w(0x1f1c50u + i));
    zero_range(0xe0000058u, 0xe0000354u);
    /* Compressed initialized data: 0x1eeadc -> 0x1802c000, 0x6bf8 bytes, stock decompressor. */
    stock_call(STOCK_DECOMPRESS | 1u, 0x1eeadcu, 0x1802c000u, 0x6bf8u, 0, 0, 0);
    /* Boot-state snapshot of the pages the borrowed routines touch (taken
     * from the emulated stock boot by emu/stockapp/ram_snapshot.py, placed in
     * the flash hole by app/hybrid/build.py): stock's constructed object
     * graph without running its component initialisation. */
    uint32_t restored = 0;
    if (snapshot_present()) {
        uint32_t n = w(SNAPSHOT_FLASH + 4);
        for (uint32_t i = 0; i < n; ++i) {
            uint32_t dst = w(SNAPSHOT_FLASH + 16 + 12 * i), src = w(SNAPSHOT_FLASH + 20 + 12 * i), len = w(SNAPSHOT_FLASH + 24 + 12 * i);
            if (dst >= app_lo && dst < app_hi) continue;   /* never over our own image */
            snapshot_copy(dst, src, len);
            ++restored;
        }
        snapshot_pages = restored;
    } else {
        install_nvm_objects();                          /* fallback stand-ins without a snapshot */
    }
    /* The sparse snapshot omits the profile table, and a direct CR move
     * does not perform stock's separate d3e06 profile initialization.
     * Restore it here, before the paper loader can submit a CR request.
     * This is the same ROM table previously restored only by band_run(). */
    for (uint32_t k = 0; k < 14u * 0xf8u; k += 4)
        ww(0x18089900u + k, w(STOCK_PROFILES_ROM + k));
    ram_ready = true;
    dbg_puts("[shim] stock RAM initialised (zero-init, SRAM copy, decompress, snapshot pages "); dbg_dec(restored); dbg_puts(")\n");
}

static void presets(bool capped)
{
    stock_call(STOCK_INIT_E8610 | 1u, 0, 0, 0, 0, 0, 0);        /* repeat count 1, cap-busy clear */
    stock_call(STOCK_INIT_C763E | 1u, 0, 0, 0, 0, 0, 0);        /* gates 0x18079330/331/332, age thresholds */
    *(volatile uint8_t *)0x180d578eu = 1;               /* executor enable */
    *(volatile uint8_t *)0x18079332u = 1; *(volatile uint8_t *)0x18079333u = 1;   /* cap enable, readiness */
    *(volatile uint8_t *)0x18079335u = capped ? 1 : 0;  /* software cap state */
    *(volatile uint8_t *)0x18079a31u = 0; *(volatile uint8_t *)0x180d55bdu = 0;   /* reentry guards */
    *(volatile uint8_t *)0x180d57d6u = 1;               /* wipe wrapper gate (brief 16 §2: OR of 0x180d57d6..d8 nonzero; meaning open) */
    *(volatile uint8_t *)0xe0000172u = 1;               /* permission (brief 17 §4) */
    /* maintenance parameters 0xe8446 would install (brief 16 §2) */
    *(volatile uint8_t *)0x18079aadu = 1; *(volatile uint8_t *)0x18079aaeu = 1; *(volatile uint8_t *)0x18079aafu = 1;
    *(volatile int16_t *)0x18079ab0u = 50; ww(0x18079ab4u, 50); ww(0x18079ab8u, 50);
    *(volatile uint8_t *)0x18079abcu = 1; *(volatile int16_t *)0x18079abeu = 80; *(volatile int16_t *)0x18079ac0u = 80;
    /* cached head temperatures, half-degrees */
    *(volatile int16_t *)0x1807acd4u = (int16_t)ambient_half; *(volatile int16_t *)0x1807acd6u = (int16_t)ambient_half;
    /* status banks: clear, cap bit mirrors the software state, pointer slots as the booted stock has them */
    for (unsigned i = 0; i < 16; ++i) { ww(F008_BANK + 4 * i, 0); ww(F009_BANK + 4 * i, 0); }
    ww(F009_BANK + 4 * 1, capped ? 1u : 0u);
    ww(F009_BANK + 4 * 7, 0x18066898u); for (unsigned i = 0; i < 24; i += 4) ww(0x18066898u + i, 0);
    ww(F008_BANK + 4 * 13, 0x18066768u);
    for (unsigned i = 0; i < EVENT_GROUPS; ++i) events[i] = 0;
    for (unsigned i = 0; i < MAILBOXES; ++i) mailbox_count[i] = 0;
    arena_used = band_live ? band_mark : 0; trace_n = 0; service_calls = 0;   /* messages above the live band */
    events[0x1a] = 0x2010u;                             /* CR and LF initial synchronisation */
    events[0x1b] = 0x1u;                                /* head power owner available */
        events[0x1c] = 0xc0000000u;                        /* idle head-work context: bits 31 and 30 (stock c8d30 ends with ca42c, ca41e, ca3e6; brief 20 §1) */
    events[0x1a] |= 0x2010u;                            /* CR (0x10) and LF (0x2000) idle: stock clears them before each request */
    *(volatile uint16_t *)0x180d5784u &= (uint16_t)~0x1000u;   /* no carriage request armed */
}

/* Band preparation (brief 8's executed recipe, no motion, no firing): stock
 * allocates the print buffers, converts a synthetic 8-row black packet with
 * the real RB2PB engine, and builds a band descriptor Q from a hand-filled
 * selection matrix. The executor is NOT called. Results are left in stock RAM
 * for the tunnel: manager 6 at 0x1879b144 (base at +0x18, raster at base+0x780),
 * Q in mailbox 0x2d. */
static uint8_t band_job[0x80] __attribute__((aligned(8)));
static uint8_t band_packet[0x40] __attribute__((aligned(8)));
static uint8_t band_source[320] __attribute__((aligned(8)));
static uint32_t band_q, band_rc[3];
/* Band geometry (USB 0x9a/0x9b): columns along the sweep (multiple of 32),
 * black nozzle rows selected (multiple of 8, up to the 40 eight-row groups of
 * the BK selection matrix) and stock's common horizontal placement Q+0x3a. */
static uint32_t band_cols = 320, band_rows = 8, band_mode = 1, band_plane = 0x18, finish_mask = 0xffu; static int16_t band_place = 5000; static bool band_colour;
/* Output group -> logical lane (brief 11 §6): groups 0..6 -> lanes 2,3,5,4,7,6,(0,1); C/M/Y are groups 0/2/4. */
static uint32_t manager_of_lane(unsigned lane)
{
    switch (lane) { case 0: case 1: return MANAGER6; case 2: return MANAGERS; case 5: return MANAGERS + 2u * 0x1cu; case 7: return MANAGERS + 4u * 0x1cu; default: return 0; }
}
static uint32_t manager_slots(uint32_t m) { return *(volatile uint16_t *)m; }
static uint32_t manager_stride(uint32_t m) { return *(volatile uint16_t *)(m + 6u); }
static uint32_t raster_mode = 0x100u;   /* 0x100: 64-byte aligned raster start; else an explicit byte shift 0..63 (0x9b wIndex=3, alignment tests) */
static uint32_t manager_raster(uint32_t m)
{
    /* Storage follows the 24-byte slot descriptors. 81 or 61 descriptors leave it at 24 or 56
     * mod 64 (mode 7) where mode 1's 80 leave it aligned; the lanes with a 0x18 mod 64 start
     * showed a missing row group every 32 rows (DOC0019). Every pool allocation carries 64
     * bytes of slack (take), so the raster may be moved up to the next 64-byte boundary. */
    uint32_t b = w(m + 0x18u) + 24u * manager_slots(m);
    return (raster_mode & 0x100u) ? (b + 63u) & ~63u : b + (raster_mode & 63u);
}
static unsigned raster_remaining;
bool hybrid_band_params(unsigned cols, unsigned rows, int place, bool set_place)
{
    if (running || routine_pending || raster_remaining) return false;
    if (set_place) {
        if (place & 0x10000) { band_plane = place & 0xffu; return true; }            /* 0x9b wIndex=1: synthetic packet plane id */
        if (place & 0x20000) { finish_mask = place & 0xffu; return true; }           /* 0x9b wIndex=2: band_finish step mask (diagnostic bisect) */
        if (place & 0x40000) { raster_mode = place & 0x1ffu; return true; }          /* 0x9b wIndex=3: raster start 0x100 = aligned, else shift 0..63 */
        if (place & 0x80000) { restore_mask = place & 0xffu; return true; }          /* 0x9b wIndex=4: snapshot ranges re-copied before a non-fresh prepare (diagnostic) */
        band_place = (int16_t)place; return true;
    }
    /* rows field: bits 0..8 nozzle rows, bit 11 colour selection (C/M/Y groups), bits 12..15 stock print mode (0 = 1) */
    unsigned mode = rows >> 12 & 0xfu, r = rows & 0x1ffu;
    if (cols < 32 || cols > 8160 || cols % 32 || r < 8 || r > 304 || r % 8 || mode > 14) return false;
    band_cols = cols; band_rows = r; band_mode = mode ? mode : 1u; band_colour = (rows & 0x800u) != 0; return true;
}

/* Stock boot's raster-feed defaults (0xe5ba4), observed in an unmodified
 * stock boot trace. Restoring its RAM snapshot does not restore these MMIO
 * registers, and the per-band executor never writes them. Keep this small
 * initializer in C; the units of the lane offsets/sizes are not established.
 * See app/hybrid/evidence/BAND_FEED_INIT.md. */
static void band_feed_init(void)
{
    wr(0xc0c28000u, 0);
    wr(0xc0c28180u, 0x1fu); wr(0xc0c28184u, 0x1fu);
    for (unsigned i = 0; i < 10; ++i)
        wr(0xc0c28300u + 4u * i, i < 8 ? 40u * i : 0);
    for (unsigned i = 0; i < 10; ++i)
        wr(0xc0c28400u + 4u * i, i < 8 ? 40u : 0);
}

static uint32_t band_prepare(void)
{
    band_feed_init();
    if (!fresh_run) snapshot_restore_ranges(restore_mask);
    /* Fresh allocator state every run: stock would otherwise free the previous
     * pools first and wait (event 0x2c) until their queued slots are consumed,
     * which never happens without a completed pass. Descriptor B 0x18075d88,
     * pool state 0x180689d0 (brief 8 §1), the seven managers at 0x1879b09c and
     * the eight dot managers at 0x1879b338 are cleared. */
    for (uint32_t a = 0x18075d88u; a < 0x18075d88u + 0xe0u; a += 4) ww(a, 0);
    for (uint32_t a = 0x180689d0u; a < 0x180689d0u + 0x20u; a += 4) ww(a, 0);
    for (uint32_t a = 0x1879b09cu; a < 0x1879b09cu + 7u * 0x1cu; a += 4) ww(a, 0);
    for (uint32_t a = 0x1879b338u; a < 0x1879b338u + 8u * 8u; a += 4) ww(a, 0);
    /* Raster-worker packet slots (two 5-entry tables at 0x1806b1dc, claimed by
     * 0xa90ca/0xa90f0 and otherwise freed only when stock finishes a packet):
     * our synthetic packet is consumed at once, so free them every prepare.
     * Without this the sixth prepare's builder spins in delay(5) forever. */
    for (unsigned i = 0; i < 10; ++i) *(volatile uint8_t *)(0x1806b1dcu + i) = 0;
    band_live = false; band_mark = 0; arena_used = 0;   /* the previous band is reclaimed here, and only here */
    for (unsigned i = 0; i < sizeof band_job; ++i) band_job[i] = 0;
    *(uint16_t *)(band_job + 0x28) = (uint16_t)band_mode; *(uint32_t *)(band_job + 0x2c) = band_cols; *(uint16_t *)(band_job + 0x30) = 600;
    band_rc[0] = stock_call(STOCK_ALLOC_JOB | 1u, (uint32_t)band_job, 0, 0, 0, 0, 0);
    note(T_UNKNOWN, 0xadc14, band_rc[0], w(MANAGER6 + 0x18));
    dbg_puts("[shim] adc14 -> "); dbg_dec(band_rc[0]); dbg_puts(" manager6 base=0x"); dbg_u32(w(MANAGER6 + 0x18)); dbg_putc('\n');
    if (!w(MANAGER6 + 0x18)) return 0xbad1;

    for (unsigned i = 0; i < sizeof band_source; ++i) band_source[i] = 0;
    for (unsigned r = 0; r < 8; ++r) band_source[40 * r + r] = (uint8_t)(0x80u >> r);
    for (unsigned i = 0; i < sizeof band_packet; ++i) band_packet[i] = 0;
    *(uint16_t *)(band_packet + 0) = 1; *(uint16_t *)(band_packet + 2) = 600;
    *(uint32_t *)(band_packet + 8) = band_plane; *(uint32_t *)(band_packet + 0xc) = 320;
    *(uint32_t *)(band_packet + 0x14) = (uint32_t)band_source;
    *(uint16_t *)(band_packet + 0x1c) = 1; *(uint16_t *)(band_packet + 0x1e) = 8;
    band_rc[1] = stock_call(STOCK_RASTER | 1u, (uint32_t)band_packet, 0, 0, 0, 0, 0);
    note(T_UNKNOWN, 0xa8422, band_rc[1], 0);
    dbg_puts("[shim] a8422 -> "); dbg_dec(band_rc[1]); dbg_putc('\n');

    /* Selection matrix: 7 output groups x 40 eight-row entries (group 6 = the
     * two BK lanes); entry value = raster slot (storage group g at +16g in each
     * tile). The builder turns entry e into lane-mask bit e-2 (entry 18 gave
     * bit 16 on the PLACED sheet; entries 0..39 gave 38 bits), so entries 0
     * and 1 drive nothing: 38 groups = 304 rows per band. The 8-row legacy
     * band keeps entry 18 -> slot 0; wider bands map entry 2+g -> slot g. */
    for (unsigned i = 0; i < 7 * 40; ++i) *(volatile int16_t *)(MATRIX + 2 * i) = -1;
    if (band_rows == 8) *(volatile int16_t *)(MATRIX + 2 * (6 * 40 + 18)) = 0;
    else for (unsigned g = 0; g < band_rows / 8; ++g) *(volatile int16_t *)(MATRIX + 2 * (6 * 40 + 2 + g)) = (int16_t)g;
    if (band_colour) {
        /* C, M, Y = output groups 0, 2, 4. The builder makes colour mask bit e-18 from entry e
         * (logical colour rows 144..303, origin 144 = 18 groups; brief 20 §4): entries 18..37
         * select the 160-row arrays, raster slot g for nozzle group g. (CR0006: entries from 2
         * reached only the groups >= 18, i.e. 28/44/92/160 rows for 160/176/224/304.) */
        uint32_t crows = band_rows < 160u ? band_rows : 160u;
        for (unsigned grp = 0; grp < 6; grp += 2) {
            uint32_t slots = manager_slots(MANAGERS + grp * 0x1cu);
            for (unsigned g = 0; g < crows / 8 && g < slots; ++g) *(volatile int16_t *)(MATRIX + 2 * (grp * 40 + 18 + g)) = (int16_t)g;
        }
    }
    *(volatile uint8_t *)0x1806c86au = 8; *(volatile uint16_t *)0x1806cad4u = 600;
    /* Brief 20 §2: with the snapshot's previous-state byte 0x1802ef84 = 1 the builder's
     * transition policy (b2aa0 -> b29f2) shifts both black masks right by two and adds a
     * paired 32 Ulf feed carry that our manually positioned page path never applies. The
     * page positions paper itself, so start the first build in the steady state. */
    if (fresh_run) *(volatile uint8_t *)0x1802ef84u = 0;
    mailbox_count[0x2d] = 0;
    band_rc[2] = stock_call(STOCK_Q_BUILD | 1u, 0, 0, 0, 0, 0, 0);
    band_q = mailbox_count[0x2d] ? (uint32_t)mailbox[0x2d][0] : 0;
    note(T_UNKNOWN, 0xb12a2, band_rc[2], band_q);
    dbg_puts("[shim] b12a2 -> "); dbg_dec(band_rc[2]); dbg_puts(" Q=0x"); dbg_u32(band_q); dbg_putc('\n');
    band_mark = arena_used; band_live = band_q != 0;
    if (band_mark > prof_arena_peak) prof_arena_peak = band_mark;
    dbg_puts("[shim] band arena "); dbg_dec(band_mark); dbg_puts(" bytes live\n");
    return band_q ? 1u : 0xbad3;
}

/* Band run: brief 11's recipe on the prepared Q (extents, addresses, explicit
 * profile 3, forward, placement 5000), scheduler state, the CR profile table
 * from ROM, lane preparation, then the stock band executor. With the carriage
 * adapter dry nothing moves, so the hardware position windows never trigger
 * and nothing fires; the executor's register programming and waits run for
 * real. Live mode drives our carriage to the stock target minus cr_offset. */
/* Stock's band task after the executor returns (0xe6dc6..0xe6e24): result
 * notification, CR wait + release pair (0xe7052: d227e, d1d9e), the band-task
 * flags at 0x180d0320 cleared, two registration bytes reset (0xe2a3e, 0xe2c40),
 * 0xe753c, then the descriptor handed back (0xe28a6(Q,1) -> 0xea73e) or the
 * error path 0xe28ec. Without it the sixth prepare's Q builder waits forever
 * for a queue slot (sheet PG2039: bands 0..4 printed, then stalled) and every
 * later pass waits 2 x 15 s for the "previous head work done" event. */
static void band_finish(uint32_t r, uint32_t q)
{
    if (finish_mask & 1u) stock_call(0xe281au | 1u, r, 0, 0, 0, 0, 0);
    if (finish_mask & 2u) stock_call(0xe7052u | 1u, 0, 0, 0, 0, 0, 0);
    if (finish_mask & 4u) {
        *(volatile uint8_t *)0x180d0320u = 0; *(volatile uint8_t *)0x180d0322u = 0; *(volatile uint8_t *)0x180d0323u = 0;
        *(volatile uint16_t *)0x180d0326u = 0;
    }
    if (finish_mask & 8u) stock_call(0xe2a3eu | 1u, q, 0, 0, 0, 0, 0);
    if (finish_mask & 16u) stock_call(0xe2c40u | 1u, q, 0, 0, 0, 0, 0);
    if (finish_mask & 32u) stock_call(0xe753cu | 1u, q, 0, 0, 0, 0, 0);
    if (finish_mask & 64u) {
        if (r == 1) {
            if (*(volatile uint8_t *)0x180d0321u == 1) stock_call(0xe295eu | 1u, q, 0, 0, 0, 0, 0);
            else stock_call(0xe28a6u | 1u, q, 1, 0, 0, 0, 0);
        } else stock_call(0xe28ecu | 1u, r, q, 0, 0, 0, 0);
    }
    note(T_UNKNOWN, 0xe6dc6, r, *(volatile uint8_t *)0x180d0320u);
    dbg_puts("[shim] band finish done, errors="); dbg_dec(errors); dbg_putc('\n');
}

static uint32_t band_run(void)
{
    uint32_t q = band_q, N = band_cols;
    *(volatile int16_t *)(q + 0x28) = 0; *(volatile int16_t *)(q + 0x3a) = band_place; *(volatile int16_t *)(q + 0x3c) = 0;
    for (unsigned i = 0; i < 10; ++i) *(volatile int16_t *)(q + 0x3e + 2 * i) = 0;
    *(volatile uint16_t *)(q + 0x52) = 0x83; *(volatile uint8_t *)(q + 0x54) = 1;
    for (unsigned i = 0; i < 10; ++i) {
        uint32_t m = manager_of_lane(i);
        bool on = m && manager_slots(m) && (i < 2 || band_colour);
        uint32_t stride = on ? manager_stride(m) : 0, B = on ? manager_raster(m) : 0xffffffffu, END = on ? B + (N / 16 - 1) * stride : 0xffffffffu;
        *(volatile int16_t *)(q + 0x56 + 2 * i) = on ? 0 : -1; *(volatile int16_t *)(q + 0x6a + 2 * i) = on ? (int16_t)(4 * N) : -1;
        ww(q + 0x11c + 4 * i, B); ww(q + 0x144 + 4 * i, END);
        ww(q + 0x16c + 4 * i, stride); ww(q + 0x194 + 4 * i, B); ww(q + 0x1bc + 4 * i, END);
        ww(q + 0x1e4 + 4 * i, on ? manager_slots(m) - 1u : 0); ww(q + 0x20c + 4 * i, 0); ww(q + 0x234 + 4 * i, 0);
        if (!on) { ww(q + 0x2ac + 12 * i, 0); ww(q + 0x2b0 + 12 * i, 0); ww(q + 0x2b4 + 12 * i, 0); }   /* active lanes keep the builder's selection masks */
    }
    ww(q + 0x350, 1u | (band_colour ? 4u : 0)); ww(q + 0x354, 3u | (band_colour ? 4u : 0));   /* bit 2: colour lanes in this pass (brief 11 §6) */
    for (unsigned i = 0; i < 5; ++i) ww(q + 0x394 + 4 * i, 0x009f0000);
    ww(q + 0x3a8, 9);
    /* Cached common horizontal offset (e4380): zero + valid only on a fresh
     * snapshot (brief 11 recipe); later passes keep what lane preparation
     * computed, as stock's own band loop does, so the placement stays put. */
    if (fresh_run) { ww(0x180cda80u, 0); ww(0x180cda84u, 0); *(volatile uint8_t *)0x180cda88u = 1; }
    *(volatile uint8_t *)0x180cff5du = 0; ww(0x180cd590u, 0);
    note(T_UNKNOWN, 0x2ac, w(q + 0x2ac), w(q + 0x2b0)); note(T_UNKNOWN, 0x2b8, w(q + 0x2b8), w(q + 0x2bc));   /* BK lane masks from the builder */
    dbg_puts("[shim] band N="); dbg_dec(N); dbg_puts(" rows="); dbg_dec(band_rows); dbg_puts(" place="); dbg_dec((uint32_t)(int32_t)band_place);
    dbg_puts(" masks 0x"); dbg_u32(w(q + 0x2ac)); dbg_putc(' '); dbg_u32(w(q + 0x2b0)); dbg_putc(' '); dbg_u32(w(q + 0x2b4)); dbg_putc('\n');
    uint32_t r = stock_call(STOCK_LANE_PREP | 1u, q, 0, 0, 0, 0, 0);
    uint32_t lanes0 = w(0x180cda70u), target = w(0x180cda4cu), profile = w(0x180cda54u);
    note(T_UNKNOWN, 0xe6aea, r, lanes0); note(T_UNKNOWN, 0xda74, w(0x180cda74u), w(0x180cda78u));
    dbg_puts("[shim] e6aea -> "); dbg_dec(r); dbg_puts(" lanes=0x"); dbg_u32(lanes0); dbg_putc('/'); dbg_u32(w(0x180cda74u)); dbg_putc('/'); dbg_u32(w(0x180cda78u)); dbg_puts(" target="); dbg_dec(target); dbg_puts(" profile=0x"); dbg_u32(profile); dbg_putc('\n');
    if ((lanes0 & 0xffffu) != 0x0101u) return 0xbad6;
    r = stock_call(STOCK_BAND_EXEC | 1u, q, 0, 0, 0, 0, 0);
    note(T_UNKNOWN, 0xe6b80, r, 0);
    dbg_puts("[shim] e6b80 -> "); dbg_dec(r); dbg_putc('\n');
    { uint32_t t0 = app_clock_ticks(); band_finish(r, q); prof_add(HP_FINISH, t0); }
    return r;
}

/* Band raster upload (USB 0x99 + bulk OUT): the host writes the tile-layout
 * raster (byte = j*0x500 + 16*g + 2*r, bit b = column 16j+b, row 8g+r; measured
 * on hardware, evidence/stockapp/band) into manager 6's storage after
 * band-prepare, replacing what the RB2PB conversion produced. Routine 7 then
 * runs the executor on it without re-preparing. */
static uint8_t *raster_dst; static unsigned raster_received;
bool hybrid_raster_active(void) { return raster_remaining != 0; }
bool hybrid_raster_begin(unsigned total, unsigned manager)
{
    if (manager > 6) return false;
    uint32_t m = MANAGERS + manager * 0x1cu, base = w(m + 0x18);
    uint32_t width = *(volatile uint16_t *)(m + 4u);   /* the manager's own width: colour managers are allocated at twice the job width */
    uint32_t limit = (width / 16u) * manager_stride(m), allocated = w(m + 8);   /* manager: slots u16, pad u16, width u16, stride u16, size u32, .. base at +0x18 */
    if (running || routine_pending || !band_q || !base || !manager_slots(m) || !total || total > limit || total > allocated) return false;
    raster_dst = (uint8_t *)manager_raster(m); raster_remaining = total; raster_received = 0;
    dbg_puts("[shim] raster upload "); dbg_dec(total); dbg_puts(" bytes to 0x"); dbg_u32((uint32_t)raster_dst); dbg_putc('\n');
    return true;
}
void hybrid_raster_feed(const uint8_t *data, unsigned len)
{
    if (len > raster_remaining) len = raster_remaining;
    for (unsigned i = 0; i < len; ++i) raster_dst[raster_received + i] = data[i];
    raster_received += len; raster_remaining -= len;
    if (!raster_remaining) { dbg_puts("[shim] raster upload complete: "); dbg_dec(raster_received); dbg_puts(" bytes\n"); }
}

/* ---- on-device print engine (print_engine.c) ---- */
uint32_t hybrid_last_result(void) { return last_result; }
unsigned hybrid_routines_run(void) { return routines_run; }
bool hybrid_sheet_loaded(void) { return ram_ready && *(volatile uint8_t *)0x180c6f54u == 1u; }   /* stock ASF state byte: 1 = loaded and aligned */
uint8_t *hybrid_manager_raster(unsigned group, unsigned *slots, unsigned *width, unsigned *stride, unsigned *size)
{
    if (group > 6 || running || routine_pending || !band_q) return 0;
    uint32_t m = MANAGERS + group * 0x1cu;
    if (!w(m + 0x18) || !manager_slots(m)) return 0;
    *slots = manager_slots(m); *width = *(volatile uint16_t *)(m + 4u); *stride = manager_stride(m); *size = w(m + 8);
    return (uint8_t *)manager_raster(m);
}

/* ---- USB interface (0x95 call, 0x96 result, 0x97 run, 0x98 trace, 0x99 raster) ---- */
bool hybrid_panel_available(void)
{
    /* Only the shape-B app linked in the stock hole may use these buttons.
     * A standalone app can leave old stock bytes behind in flash. */
    return (uint32_t)app_entry == 0x201a8fe0u && snapshot_present();
}

bool hybrid_panel_advance(void)
{
    if (!hybrid_panel_available() || service_busy() || head_fire_busy() || cartridge_busy() ||
        routine_pending || raster_remaining) return false;
    if (!panel_motor_ready) {
        /* Shared setup with motor outputs disabled also restores the PE
         * input after reboot. Do not home or move the carriage here. */
        if (!scanner_motor_shared_init()) return false;
        panel_motor_ready = true;
    }
    return hybrid_run(10u | 0x800u, 400u);
}

bool hybrid_run(unsigned value, unsigned index)
{
    if (running || service_busy() || raster_remaining) return false;
    unsigned routine = value & 0xfu;
    if (routine < 1 || routine > 10) return false;
    if (routine == 10 && (!index || index > 1200u)) return false;
    routine_pending = routine; routine_type = value >> 4 & 0xfu;
    live = (value & 0x100u) != 0; motion_only = (value & 0x200u) != 0; lf_live = (value & 0x800u) != 0; cr_stock = (value & 0x1000u) != 0; ambient_half = (index & 0xffu) ? (index & 0xffu) : 50u;
    cr_motion_pending = false; cr_stock_dry = (value & 0x2000u) != 0;   /* emulator: stock CR task without a homed carriage */
    fresh_run = (value & 0x400u) != 0;
    if (fresh_run) { ram_ready = false; band_q = 0; }   /* fresh: stock RAM back to the boot snapshot first */
    stage_limit = routine == 10 ? 0 : index >> 8;       /* routine 10 uses all of wIndex as distance */
    if (routine == 10) { advance_ulf = index; ambient_half = 50; }
    if (live && !carriage_referenced()) { dbg_puts("[shim] live run needs a homed carriage\n"); routine_pending = 0; return false; }
    if (live) {
        /* Stock programs the head's position windows against the raw CR counter
         * in its own frame; put the counter there (capped carriage = 2688) and
         * map stock targets through the same offset. */
        if (!carriage_set_frame(STOCK_CAP_POSITION)) { dbg_puts("[shim] carriage frame preset failed\n"); routine_pending = 0; return false; }
        cr_offset = (int32_t)STOCK_CAP_POSITION;
    }
    return true;
}

void hybrid_poll(void)
{
    if (!routine_pending) return;
    unsigned routine = routine_pending; routine_pending = 0; running = true; prof_routine = routine;
    if (!ram_ready) { uint32_t t0 = app_clock_ticks(); stock_ram_init(); prof_add(HP_RAMINIT, t0); }
    if (stage_limit == 1) { ++routines_run; running = false; dbg_puts("[shim] stage 1 reached\n"); return; }
    if (!svc_ready && !register_stock_svc()) { ++errors; running = false; return; }
    if (stage_limit == 2) { ++routines_run; running = false; dbg_puts("[shim] stage 2 reached\n"); return; }
    presets(routine == 2 || routine == 3);              /* uncap and wipe start capped */
    if (stage_limit == 3) { ++routines_run; running = false; dbg_puts("[shim] stage 3 reached\n"); return; }
    dbg_puts("[shim] run routine "); dbg_dec(routine); dbg_puts(live ? " live\n" : " dry\n");
    uint32_t r = 0;
    switch (routine) {
    case 1: r = stock_call(STOCK_CAP | 1u, 2, 1, 1, 0, 0, 0); break;
    case 2: r = stock_call(STOCK_UNCAP | 1u, 0, 1, 0, 0, 0, 0); break;
    case 3: r = stock_call(STOCK_WIPE | 1u, 0, 1, 0, 0, 0, 0); break;
    case 4: r = stock_call(STOCK_CLEAN | 1u, routine_type ? routine_type : 1u, 0, 0, 1, 0, 0); break;
    case 5: { uint32_t t0 = app_clock_ticks(); r = band_prepare(); prof_add(HP_PREPARE, t0); } break;
    case 6: { uint32_t t0 = app_clock_ticks(); r = band_prepare(); prof_add(HP_PREPARE, t0); } if (r == 1) r = band_run(); break;
    case 7: r = band_q ? band_run() : 0xbad7; break;   /* executor only: the prepared (or uploaded) raster */
    case 8: r = feed_load(); break;                     /* stock paper load (LF motion real only with 0x800) */
    case 9: r = feed_eject(); break;                    /* stock LF eject requests until the sensor clears */
    case 10: r = feed_advance(); break;                 /* one bounded stock LF step, no firing */
    }
    /* Drain pending stock motions started inside the routine (their ticks and
     * completion run from the hardware-event poll). */
    { uint32_t t0 = app_clock_ticks();
      while ((cr_stock && (cr_running() || cr_motion_pending)) || (lf_live && (lf_motion_pending || *(volatile uint16_t *)(LF_TIMER_STATE + 2u) == 1u))) {
          poll_hardware_events(); shim_poll();
          if ((app_clock_ticks() - t0) / CLOCK_TICKS_PER_MS > 15000u) { ++errors; dbg_puts("[shim] motion drain timeout\n"); break; }
      }
      prof_add(HP_DRAIN, t0); }
    last_result = r; ++routines_run; running = false; prof_routine = 0;
    note(T_ROUTINE, routine, r, errors);
    if (r == 0xdead) { dbg_puts("[shim] stock call aborted by exception kind "); dbg_dec(exc_kind); dbg_puts(" lr=0x"); dbg_u32(exc_lr); dbg_puts(" spsr=0x"); dbg_u32(exc_spsr); dbg_putc('\n'); }
    dbg_puts("[shim] routine "); dbg_dec(routine); dbg_puts(" returned "); dbg_dec(r); dbg_puts(" errors="); dbg_dec(errors); dbg_putc('\n');
}

bool hybrid_busy(void) { return running || routine_pending != 0; }

unsigned hybrid_trace(uint8_t *out, unsigned index)
{
    unsigned count = trace_n < TRACE_RECORDS ? trace_n : TRACE_RECORDS;
    if (index >= count) return 0;
    unsigned first = trace_n < TRACE_RECORDS ? 0 : trace_n % TRACE_RECORDS;
    const struct trace *t = &trace[(first + index) % TRACE_RECORDS];
    put32(out, t->kind); put32(out + 4, t->a); put32(out + 8, t->b); put32(out + 12, t->c);
    return 16;
}

unsigned hybrid_run_status(uint8_t *out)
{
    put32(out, 0x4e555248u); put32(out + 4, last_result); put32(out + 8, routines_run); put32(out + 12, errors);
    put32(out + 16, trace_n); put32(out + 20, (running ? 1u : 0) | (live ? 2u : 0) | (ram_ready ? 4u : 0));
    put32(out + 24, cr_position); put32(out + 28, snapshot_pages);   /* was cr_offset (always 0 so far) */
    put32(out + 32, exc_kind); put32(out + 36, exc_lr); put32(out + 40, exc_spsr); put32(out + 44, exc_count);
    put32(out + 48, band_q); put32(out + 52, w(MANAGER6 + 0x18)); put32(out + 56, band_rc[0]); put32(out + 60, band_rc[1]);
    return 64;                                          /* the control buffer holds 64 bytes */
}
