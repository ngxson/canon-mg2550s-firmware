/* Print mechanism (CR carriage / LF paper feed) register observation.
 *
 * Stage A of app/PRINT_TEST_PLAN.md: a read-only dump of the two print motor
 * blocks so the encoder counters can be watched while the carriage or feed
 * roller is moved by hand. Nothing here writes to the motor blocks. Block
 * roles follow fw_dump/PROGRESS.md section 1i (CR companion 0xc0c50000,
 * primary 0xc0c51000; LF companion 0xc0c52000, primary 0xc0c53000). */
#include "app.h"

static const uint32_t print_regs[] = {
    0xc0c50000u, 0xc0c50004u, 0xc0c50008u,                /* CR drive: enable, period, command */
    0xc0c51000u, 0xc0c51004u, 0xc0c51008u,                /* CR primary control words */
    0xc0c51100u, 0xc0c51118u, 0xc0c51400u, 0xc0c51404u,   /* CR position, head staging position, event mask/status */
    0xc0c52000u, 0xc0c52004u, 0xc0c52008u,                /* LF drive: enable, period, command */
    0xc0c53000u, 0xc0c53004u, 0xc0c53008u,                /* LF primary control words */
    0xc0c53100u, 0xc0c53108u, 0xc0c53120u, 0xc0c53220u,   /* LF position and event-cleared words */
    0xc0c53200u, 0xc0c53204u, 0xc0c53208u, 0xc0c5320cu,   /* LF period captures */
    0xc0c53400u, 0xc0c53404u, 0xc0c52100u, 0xc0c52104u,   /* LF primary and companion event mask/status */
    0xc0c56000u, 0xc0c57100u,                             /* scanner motor enable and position, for reference */
    0xc0c54000u, 0xc0c54004u, 0xc0c54008u,                /* fourth channel candidate (feeding motor?): drive */
    0xc0c55000u, 0xc0c55004u, 0xc0c55008u, 0xc0c55100u,   /* fourth channel primary control and position */
    0xc0c55400u, 0xc0c55404u, 0xc0c54100u, 0xc0c54104u,   /* fourth channel event mask/status */
};

/* Reply: MGPR, version 1, count, then one word per register in table order. */
unsigned print_mech_dump(uint8_t *out)
{
    put32(out, 0x5250474du);
    put32(out + 4, 1);
    put32(out + 8, ARRAY_SIZE(print_regs));
    for (unsigned i = 0; i < ARRAY_SIZE(print_regs); ++i) put32(out + 12 + 4 * i, rd(print_regs[i]));
    return 12 + 4 * ARRAY_SIZE(print_regs);
}

/* Stage A2: prepare the CR (and optionally LF) primary block for counting with
 * every motor driver left disabled. Variant bits (USB 0x65 wValue):
 *   1: CR primary init words: control 0x0d, P+4=0, P+8=1200, position 0xf0000,
 *      period/feedback 0xfffff, event mask 0 (section 1i), plus count enables
 *      P+0x148=1 and P+0x308=1 *by analogy with the scanner block* (unverified
 *      for CR).
 *   2: shared signal routing as the scanner driver does before enabling its
 *      block (0xc0c4100x, 0xc0c414xx motor route, 0xc0c85604, 0xc0c81090),
 *      without the motor-driver serial commands.
 *   4: the same primary init for the LF block (0xc0c53000), analogy only.
 *  16: the same primary init for the fourth channel candidate (0xc0c55000).
 *   8: run the scanner driver's complete motor-off shared setup first (signal
 *      routing, motor-driver IC serial wake, scanner block init), the path every
 *      physical scanner move already uses.
 * No enable register (0xc0c50000/52000/56000) is ever written non-zero. */
static void prepare_primary(uint32_t p, uint32_t position)
{
    wr(p, 0x0du); wr(p + 4u, 0u); wr(p + 8u, 1200u);
    wr(p + 0x100u, position); wr(p + 0x200u, 0xfffffu);
    wr(p + 0x400u, 0u); wr(p + 0x404u, 0u);
    wr(p + 0x148u, 1u); wr(p + 0x308u, 1u);
}

bool print_mech_prepare(unsigned variant)
{
    if (!variant || variant > 31u) return false;
    if (rd(0xc0c50000u) || rd(0xc0c52000u) || rd(0xc0c56000u)) {
        dbg_puts("[pmech] refused: a motor driver is enabled\n"); return false;
    }
    dbg_puts("[pmech] counter prep variant="); dbg_dec(variant); dbg_putc('\n');
    if ((variant & 8u) && !scanner_motor_shared_init()) {
        dbg_puts("[pmech] shared setup failed\n"); return false;
    }
    if (variant & 2u) {
        static const uint32_t motor_route[7] = {0, 4, 5, 6, 7, 11, 10};
        wr(0xc0c41008u, 0u); wr(0xc0c41004u, 0u);
        for (unsigned i = 0; i < 7u; ++i) wr(0xc0c41400u + 4u * i, motor_route[i]);
        wr(0xc0c85604u, 1u); wr(0xc0c41008u, 1u);
        delay_ticks(CLOCK_TICKS_PER_MS);
        wr(0xc0c81090u, 0x3fu);
    }
    if (variant & 1u) prepare_primary(0xc0c51000u, 0xf0000u);
    if (variant & 4u) prepare_primary(0xc0c53000u, 0xf0000u);
    if (variant & 16u) prepare_primary(0xc0c55000u, 0xf0000u);
    dbg_puts("[pmech] CR pos="); dbg_u32(rd(0xc0c51100u));
    dbg_puts(" LF pos="); dbg_u32(rd(0xc0c53100u)); dbg_putc('\n');
    return true;
}

/* GPIO snapshot (USB 0x66 IN, wValue = 64-word chunk): read slots of every GPIO
 * alias group (0xc0c84000 + group*0x200 + slot*0x10 + 8, 18 groups x 32 slots)
 * followed by the bank words 0xc0c81200..0xc0c816fc. Read-only; a host diff of
 * two snapshots (sensor state changed by hand) locates an input bit without RE. */
#define SNAP_ALIAS_WORDS (18u * 32u)
#define SNAP_BANK_WORDS 320u
#define SNAP_WORDS (SNAP_ALIAS_WORDS + SNAP_BANK_WORDS)

static uint32_t snapshot_address(unsigned i)
{
    if (i < SNAP_ALIAS_WORDS) return 0xc0c84008u + (i / 32u) * 0x200u + (i % 32u) * 0x10u;
    return 0xc0c81200u + (i - SNAP_ALIAS_WORDS) * 4u;
}

unsigned print_mech_snapshot(uint8_t *out, unsigned chunk)
{
    unsigned first = chunk * 64u;
    if (first >= SNAP_WORDS) return 0;
    unsigned n = SNAP_WORDS - first < 64u ? SNAP_WORDS - first : 64u;
    for (unsigned i = 0; i < n; ++i) put32(out + 4 * i, rd(snapshot_address(first + i)));
    return 4 * n;
}

/* Bounded open-loop LF (paper feed) pulse, Stage D of PRINT_TEST_PLAN.md.
 * USB 0x67 OUT: wValue = raw drive command (register units, 2000 = stock neutral,
 * stock low commands 1880..2120), wIndex = duration in ms (1..200). Refused while
 * any motor enable is set or the scanner is busy. Sequence follows the stock LF
 * init (drive off, period 4000, neutral) and the scanner jog: enable at neutral,
 * apply the command, sample the position every millisecond, neutral, settle,
 * disable. Hard cap 2 s regardless of host input; USB is polled every millisecond. Polarity is unknown until
 * observed; see the plan's purge-clutch caution. */
#define LF_C 0xc0c52000u
#define LF_P 0xc0c53000u
static uint32_t jog_c = LF_C, jog_p = LF_P;   /* selected channel for the bounded pulse */
#define LF_NEUTRAL 2000u
#define LF_SAMPLES 32u
static uint32_t lf_result[8 + LF_SAMPLES];   /* magic, version, done, command, ms, start, end, count, samples */
static unsigned lf_pending_command, lf_pending_ms;
static bool lf_pending;

bool print_mech_lf_request(unsigned value, unsigned ms)
{
    unsigned command = value & 0xfffu, channel = (value >> 12) & 3u;
    /* Channel 0 = paper feed, 1 = the (absent) 0xc0c54000 block, 2 = print carriage.
     * The carriage has end stops: its pulses are capped at 1000 ms. */
    static const uint32_t bases[3][2] = { {LF_C, LF_P}, {0xc0c54000u, 0xc0c55000u}, {0xc0c50000u, 0xc0c51000u} };
    if (channel > 2u || !ms || ms > (channel == 2u ? 1000u : 2000u) || lf_pending) return false;
    jog_c = bases[channel][0]; jog_p = bases[channel][1];
    if (command > 2600u || command < 1400u) return false;      /* at most +-30 % of neutral */
    if (scanner_motor_busy() || scan_page_busy() || paper_feed_busy()) return false;
    if (rd(0xc0c50000u) || rd(LF_C) || rd(0xc0c54000u) || rd(0xc0c56000u)) return false;
    lf_pending_command = command; lf_pending_ms = ms; lf_pending = true;
    lf_result[0] = 0x4a464c4du; lf_result[1] = 1; lf_result[2] = 0;   /* result pending until the pulse ends */
    return true;
}

bool print_mech_lf_busy(void) { return lf_pending; }

void print_mech_poll(void)
{
    if (!lf_pending) return;
    unsigned ms = lf_pending_ms, command = lf_pending_command;
    dbg_puts("[lf] jog ch="); dbg_dec(jog_c == LF_C ? 0u : jog_c == 0xc0c50000u ? 2u : 1u); dbg_puts(" command="); dbg_dec(command); dbg_puts(" ms="); dbg_dec(ms); dbg_putc('\n');
    wr(jog_c, 0); wr(jog_c + 4u, 4000u); wr(jog_c + 8u, LF_NEUTRAL);
    wr(jog_p + 0x400u, 0); wr(jog_p + 0x404u, 0); wr(jog_c + 0x100u, 0); wr(jog_c + 0x104u, 0);
    uint32_t start = rd(jog_p + 0x100u), count = 0;
    wr(jog_c, 0x102u);
    delay_ticks(CLOCK_TICKS_PER_MS);
    wr(jog_c + 8u, command);
    uint32_t t0 = app_clock_ticks();
    for (unsigned i = 0; i < ms; ++i) {
        delay_ticks(CLOCK_TICKS_PER_MS); usb_poll();
        if (count < LF_SAMPLES && (i % ((ms + LF_SAMPLES - 1u) / LF_SAMPLES)) == 0)
            lf_result[8 + count++] = rd(jog_p + 0x100u);
    }
    uint32_t span = app_clock_ticks() - t0;
    wr(jog_c + 8u, LF_NEUTRAL);
    delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    wr(jog_c, 0);
    uint32_t end = rd(jog_p + 0x100u);
    lf_result[0] = 0x4a464c4du;  /* MLFJ */
    lf_result[1] = 1; lf_result[2] = 1; lf_result[3] = command | (jog_c == LF_C ? 0u : jog_c == 0xc0c50000u ? 0x2000u : 0x1000u); lf_result[4] = span / CLOCK_TICKS_PER_MS;
    lf_result[5] = start; lf_result[6] = end; lf_result[7] = count;
    dbg_puts("[lf] start="); dbg_u32(start); dbg_puts(" end="); dbg_u32(end);
    dbg_puts(" evt="); dbg_u32(rd(jog_p + 0x404u)); dbg_putc('/'); dbg_u32(rd(jog_c + 0x104u)); dbg_putc('\n');
    lf_pending = false;
}

unsigned print_mech_lf_status(uint8_t *out)
{
    if (!lf_result[0]) { put32(out, 0x4a464c4du); put32(out + 4, 1); put32(out + 8, 0); return 12; }
    for (unsigned i = 0; i < ARRAY_SIZE(lf_result); ++i) put32(out + 4 * i, lf_result[i]);
    return 4 * ARRAY_SIZE(lf_result);
}

/* Panel keys (owner's bench only; the emulator uses USB 0x67): Copy B/W = carriage
 * away from the park wall (command 2600, counting up), Copy Colour = carriage back
 * toward the wall (1400), each one bounded 500 ms pulse. The first press since boot
 * runs the shared driver-IC serial wake and the CR primary init with every drive
 * disabled. */
static bool cr_button_ready;

bool print_mech_lf_button(unsigned dir)
{
    if (lf_pending || scanner_motor_busy() || scan_page_busy()) return false;
    if (!cr_button_ready) {
        if (rd(0xc0c50000u) || rd(LF_C) || rd(0xc0c56000u)) return false;
        if (!scanner_motor_shared_init()) { dbg_puts("[cr] button: shared setup failed\n"); return false; }
        prepare_primary(0xc0c51000u, 0xf0000u);
        cr_button_ready = true;
    }
    dbg_puts(dir ? "[cr] button: colour -> command 1400 (toward wall)\n" : "[cr] button: b/w -> command 2600 (away from wall)\n");
    return print_mech_lf_request((dir ? 1400u : 2600u) | 0x2000u, 500u);
}
