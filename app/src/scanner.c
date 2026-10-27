/* Scanner (CIS) bring-up — reverse-engineering support.
 *
 * Stationary optical/diagnostic capture, bursts, moving line acquisition and lamp
 * control (the 2026-10-23 bring-up probes USB 0x51..0x58 were removed on 2026-10-27). Motor control and the page sequence live in scanner_motor.c and
 * scan_page.c; mechanical homing is physically verified. Stationary captures
 * use a bounded drain wait with motor drive off. See evidence/PAGE_SCAN.md.
 *
 * Register map (from stock-firmware RE, see fw_dump/gpt/ANALYSIS.md and the
 * low-level serial routines 0xf3732/0xf31d2/0xf31e8):
 *   0xc0120000  scanner serial controller base (global ptr at stock 0x180d7f0c)
 *   0xc0120000 + ch*0x100   per-channel block; +0x10 status/ack (bit0 = done),
 *                           +0x1c / +0x24 received data
 *   0xc0c840c0  strobe/clock block pulsed during a transaction (+0x04)
 *   0xc0c81200  GPIO block; +0x1c bits 8..9 toggled as chip-select-like lines
 * The chip-type probe sends 0x80 then 0x44 and decodes the reply's high nibble
 * (0x40 -> type 0, 0x90 -> type 1). That active transaction is a later stage. */
#include "app.h"
#include "scnio_seq.h"

#define SCAN 0xc0120000u
#define SCAN_CH(ch) (SCAN + (ch) * 0x100u)

/* DMA target for a capture. The engine would write pixels here; we only ever
 * start a capture once the dest register has latched to THIS buffer (see the
 * guard in scanner_capture), so an errant DMA cannot land anywhere else. */
static uint8_t cap_buf[0x4000] ALIGNED;   /* covers the profile-7 R+0x3000 window */

/* Program one LDMAC channel's profile-7 descriptor (verified field map). Sets up
 * the DMA (dest/base/limit/dims) but does NOT arm (+0x08[1:0]=1) or trigger, and
 * touches no motor register. Values for width 0xc00 from scanner_dma_codex.md. */
static void ldmac_descriptor(unsigned ch, uint32_t dest, uint32_t base, uint32_t limit, uint32_t width)
{
    uint32_t b = 0xc0d50000u + ch * 0x100u;
    wr(b + 0x08, 0u); wr(b + 0x00, 0u); wr(b + 0x00, 1u);   /* channel enable */
    modify(b + 0x04, 0x7u, 0x6u);
    modify(b + 0x08, 0x3u, 0u);
    modify(b + 0x08, 0xc0u, 0x80u);
    modify(b + 0x08, 0x100u | 0x200u | 0x80000000u | 0x10u, 0u);
    wr(b + 0x14, dest); wr(b + 0x18, base); wr(b + 0x1c, limit);
    wr(b + 0x2c, width); wr(b + 0x30, 0u); wr(b + 0x34, width); wr(b + 0x38, 0u); wr(b + 0x3c, 0u);
    modify(b + 0x20, 0x0000ffffu, width);
    modify(b + 0x20, 0xffff0000u, 0x00010000u);
    wr(b + 0x24, 0u);
    modify(b + 0x28, 0x0000ffffu, 0x1u);
    modify(b + 0x28, 0xffff0000u, 0x00010000u);
}

static bool capture2_pending;

/* Scanner analog power (0xc0c85614). Hardware rule, 2026-10-24/27: the FIRST
 * acquisition setup after this rail comes up outputs exact zeros for every
 * line (stock programs its P5 staging profile first); a second setup with
 * the rail kept up outputs data. Every power-off therefore clears the
 * "primed" state and the next setup is repeated once as a throwaway. */
static bool line_primed;
static void scanner_power(bool on)
{
    wr(0xc0c85614u, on ? 1u : 0u);
    if (!on) line_primed = false;
}

static void apply_seq(const struct rw *t, unsigned n)
{
    for (unsigned i = 0; i < n; ++i)
        wr(t[i].addr, (rd(t[i].addr) & ~t[i].clear) | t[i].set);
}

/* AFE serial, polling variant (stock f35f8); bounded. packed=(reg<<12)|byte. */
static bool afe_word(uint32_t packed)
{
    wr(0xc0120108u, 1u); wr(0xc012010cu, 0x80024110u); wr(0xc0120114u, 0x20u);
    modify(0xc0c840c4u, 0u, 1u); modify(0xc0c840c4u, 1u, 0u); modify(0xc0c8121cu, 0x300u, 0u);
    wr(0xc0120118u, packed); wr(0xc0120120u, 0u); modify(0xc0120104u, 0u, 1u);
    bool done = false;
    for (uint32_t n = 0; n < 200000u; ++n)
        if (rd(0xc0120110u) & 1u) { wr(0xc0120110u, 0u); done = true; break; }
    modify(0xc0c8121cu, 0u, 0x300u); modify(0xc0c840c4u, 0u, 1u);
    delay_ticks(2000);
    return done;
}
static void afe_setup(void)
{
    wr(0xc0120108u, 1u); wr(0xc012010cu, 0x90024100u); wr(0xc0120114u, 0x20u);
    modify(0xc0c840c4u, 0u, 1u); wr(0xc0120118u, 0u); wr(0xc0120120u, 0u);
}
/* P.3 operating AFE registers, chip type 0 (literal packed words). */
static void afe_operating(void)
{
    static const uint32_t p3[] = { 0x0080u, 0x0043u, 0x10ffu, 0x200du, 0x300cu,
                                   0x40ffu, 0x500du, 0x60bbu, 0x70b1u };
    afe_setup();
    for (unsigned i = 0; i < ARRAY_SIZE(p3); ++i) afe_word(p3[i]);
    delay_ticks(CLOCK_TICKS_PER_MS * 2u);
}

static unsigned capture2_variant;
static bool last_capture_ok;

/* Stationary diagnostic P7 or optical P0 capture (source-derived recipe).
 * variant bits: 0x01 = A.2 clock switch (RISKY: writes clock controller),
 * 0x02 = AFE serial config, 0x04 = lamp/scanner power on (0xc0c85614),
 * 0x08 = exposure boost, 0x40 = optical profile 0, 0x30 = wait strategy
 * (0 fixed, 1 poll read-count, 2 poll IRQ19). NEVER writes the motor
 * (0xc0c56000/57000). Bounded throughout. */
#define CAPTURE_KEEP_POWER 0x100u  /* leave scanner power / lamp on afterwards */
#define CAPTURE_AFE_RUN 0x200u     /* AFE already configured: operating step only */
#define CAPTURE_QUIET 0x400u       /* page rows: log failures only */

static void scanner_full_capture(unsigned v)
{
    const uint32_t R = (uint32_t)(uintptr_t)cap_buf;
    bool optical = (v & 0x40u) != 0;
    uint32_t samples = optical ? 0xa20u : 0xc00u;
    uint32_t window = optical ? 0x2880u : 0x3000u;
    bool quiet = (v & CAPTURE_QUIET) != 0;
    last_capture_ok = false;
    if (!quiet) { dbg_puts("[scan] FULL capture v=0x"); dbg_u32(v); dbg_puts(" R=0x"); dbg_u32(R); dbg_putc('\n'); }
    for (unsigned i = 0; i < sizeof(cap_buf) / 4u; ++i)       /* sentinel, word stores */
        ((uint32_t *)(uintptr_t)cap_buf)[i] = 0xa5a5a5a5u;

    modify(0xc0133100u, 0u, 0x10u); wr(0xc0133100u, 8u);     /* A.1 IRQ18 src */
    modify(0xc0133104u, 0u, 0x10u); wr(0xc0133104u, 8u);     /* A.1 IRQ19 src */

    if (v & 0x02u) {
        if (!quiet) dbg_puts("  AFE serial config (type0)\n");
        afe_setup(); afe_word(0x0004u); afe_word(0x2000u);
        delay_ticks(CLOCK_TICKS_PER_MS * 5u);
        modify(0xc0c8121cu, 0u, 0x300u);
    }
    if (v & 0x04u) {
        if (!quiet) dbg_puts("  lamp / scanner power ON\n");
        scanner_power(true);
        delay_ticks(CLOCK_TICKS_PER_MS * 20u);               /* let the LED settle */
    }
    if (v & 0x01u) {
        dbg_puts("  A.2 clock switch (RISKY)\n");
        wr(0xc0d60000u, 0u);
        modify(0xc010112cu, 0x600u, 0u); modify(0xc010102cu, 0xcu, 0u); modify(0xc010112cu, 0u, 0x703u);
    }

    for (unsigned ch = 0; ch < 4; ++ch) { uint32_t b = 0xc0d50000u + ch * 0x100u; wr(b + 8, 0u); wr(b, 0u); wr(b, 1u); }
    wr(0xc0d70000u, 1u); wr(0xc0d70000u, 0u); wr(0xc0d7001cu, 0u); wr(0xc0d70018u, 0u); wr(0xc0d70004u, 0u);
    modify(0xc0d60008u, 0u, 0x80000000u);
    ldmac_descriptor(0, R,           R, R + window, samples);
    ldmac_descriptor(1, R + samples, R, R + window, samples);
    wr(0xc0d50208u, 0u); wr(0xc0d50200u, 0u); wr(0xc0d50308u, 0u); wr(0xc0d50300u, 0u);

    if (optical) apply_seq(scnio_p0, scnio_p0_N);
    else apply_seq(scnio_p7, scnio_p7_N);                          /* A.4 SCNIO config */
    if (v & 0x08u) {                                          /* longer CIS integration */
        dbg_puts("  exposure boost (4x LEDTIME)\n");
        modify(0xc0d60264u, 0xfffffu, 0x0400u); modify(0xc0d60268u, 0xfffffu, 0x0800u);
        modify(0xc0d6026cu, 0xfffffu, 0x0c00u); modify(0xc0d60270u, 0xfffffu, 0x1000u);
        modify(0xc0d60274u, 0xfffffu, 0x1400u); modify(0xc0d60278u, 0xfffffu, 0x1800u);
    }
    delay_ticks(CLOCK_TICKS_PER_MS * 2u);                     /* ~IRQ18 settle */
    if (v & (0x02u | CAPTURE_AFE_RUN)) afe_operating();       /* A.4 operating AFE */
    /* Actual DMA registers, not the legacy scalar at SCNIO+0x1dc. */
    if (rd(0xc0d50014u) != R || rd(0xc0d50114u) != R + samples ||
        rd(0xc0d5001cu) != R + window || rd(0xc0d5011cu) != R + window) {
        apply_seq(scnio_stop, scnio_stop_N); scanner_power(false);
        dbg_puts("[scan] DMA destination mismatch; capture refused\n"); return;
    }
    apply_seq(scnio_arm_run, scnio_arm_run_N);                /* A.5 arm/run/stationary */
    if (optical) modify(0xc0d60138u, 0xffffffu, 0x1a747u);
    uint32_t rc0 = rd(0xc0d60010u);
    wr(0xc0d6000cu, 1u);                                      /* A.6 direct request */

    uint32_t t0 = time_ms();
    unsigned ws = (v >> 4) & 3u, ticks = 0;
    while ((uint32_t)(time_ms() - t0) < 200u) {
        if (ws == 0) {
            if ((uint32_t)(time_ms() - t0) >= 80u) break;
        } else if (ws == 1) {
            if (rd(0xc0d60010u) != rc0) break;
        } else {
            if (rd(0xc0133104u) & 0x10u) {
                modify(0xc0d60158u, 0u, 1u);
                wr(0xc0133104u, 0x10u);
                if (++ticks == 2) wr(0xc0d6000cu, 1u);
                if (ticks >= 5) break;
            }
        }
    }

    uint32_t rc = rd(0xc0d60010u), ev0 = rd(0xc0d50010u), ev1 = rd(0xc0d50110u), st = rd(0xc0d60008u);
    apply_seq(scnio_stop, scnio_stop_N);                      /* A.7 stop */
    if ((v & 0x04u) && !(v & CAPTURE_KEEP_POWER)) scanner_power(false);   /* lamp off */

    const uint16_t *s = (const uint16_t *)(uintptr_t)cap_buf;
    unsigned nz = 0, bad = 0, changed[2] = {0, 0};
    for (unsigned i = 0; i < samples; ++i) {
        if (s[i] != 0xa5a5u) { ++nz; ++changed[i < samples / 2u ? 0 : 1]; }
        if ((i & 0x100u) ? (s[i] < 0xb000u) : (s[i] > 0x4000u)) ++bad;
    }
    bool guard = true;
    for (unsigned i = 0x3000u / 4u; i < sizeof(cap_buf) / 4u; ++i)   /* word compare */
        if (((const uint32_t *)(uintptr_t)cap_buf)[i] != 0xa5a5a5a5u) guard = false;
    /* Fixed 80ms drain is the physically tested contract; progress alone is
     * not a DMA fence. Require new data in BOTH segments and intact guard. */
    last_capture_ok = rc != rc0 && (ev0 & 4u) && (ev1 & 4u) &&
                      changed[0] >= samples / 2u - 6u && changed[1] >= samples / 2u - 6u && guard;
    if (quiet && last_capture_ok) return;
    dbg_puts("  readcnt 0x"); dbg_u32(rc0); dbg_puts("->0x"); dbg_u32(rc);
    dbg_puts(" ldmac ev0=0x"); dbg_u32(ev0); dbg_puts(" ev1=0x"); dbg_u32(ev1);
    dbg_puts(" scnio08=0x"); dbg_u32(st); dbg_putc('\n');
    dbg_puts("  buf non-sentinel="); dbg_dec(nz); dbg_putc('/'); dbg_dec(samples); dbg_puts("  pattern-mismatch="); dbg_dec(bad); dbg_putc('\n');
    dbg_puts("  buf[0..7]:");
    for (unsigned i = 0; i < 8; ++i) { dbg_putc(' '); dbg_u32(s[i]); }
    dbg_putc('\n');
    dbg_puts("[scan] FULL capture done\n");
}

void scanner_fullcap_request(unsigned v) { capture2_variant = v; capture2_pending = true; }

/* Diagnostic: N lines with ONE SCNIO/LDMAC setup, as the stock finite path does
 * (stock finite acquisition path, analysed statically): two LDMAC channels advance
 * by a 0x1440-byte row stride inside a window of (N+1) rows; one request per
 * IRQ19 event after a first preamble event; return after N+4 events. Polled
 * with the motor off and a 400 ms deadline. Exposure (eighths of the P0 window
 * widths, starts fixed) and AFE gain (R2/R5) are optional knobs. Every event's
 * time and the per-row statistics go to the debug log; rows via 0x5c. */
/* Scanner-owned DRAM (the scan ring): from the app's RAM bound (app.ld
 * APP_RAM_END = 0x20420000 for the hybrid build, LAYOUT.md) up to 0x20760000,
 * below the stock snapshot pages (alias 0x18771000+) and the stack at
 * 0x207ff000. Stock's scatter zero-fill covers it at RAM init only; no stock
 * routine or snapshot page touches it (BRIEF_hybrid_scanner.md asked for an
 * explicit reservation: this is it). DMA rows, probes and bursts land here. */
#define LINE_BUF 0x20420000u
#define LINE_BUF_BYTES 0x340000u
#define BURST_MAX 8u
#define BURST_ROW 0x1440u
#define BURST_SEG 0x0a20u
#define BURST_GUARD 256u
/* Burst rows live at the start of the scan ring (LINE_BUF, below): a burst and
 * a page never run together, and the ring is the one scanner-owned DRAM area. */
static unsigned burst_n, burst_exposure, burst_gain, burst_skip, burst_offset;
/* USB 0x5e options: component count 1 (grey) or 3 (colour, the stock C=3
 * configuration recorded by emu/stockapp/scanner_fixture.py), LDMAC repeat
 * count (+0x28 high16) and the sub-period T (S+0x138 = T-1, default P0). */
static unsigned burst_components = 1u, burst_repeat = 1u, burst_period = 0x1a748u, burst_rows, burst_window;
static bool burst_immediate;   /* first request right after arming (as scanner_full_capture does), not on an IRQ19 event */
static bool burst_no_ack158;   /* do not write S+0x158 bit 0 on each event */
static bool burst_full_arm;    /* arm with the complete stock table (enables inside) and the period after it, as scanner_full_capture */
static bool burst_keep_power, burst_ok;   /* calibration passes: rail stays up between passes; last burst completed with data */
static bool burst_pending;

static bool line_setup(unsigned components, unsigned lines, unsigned pitch, unsigned period);
static bool afe_operating_gain(unsigned gain, unsigned offset)
{
    static const uint8_t operating[8] = {0x43u, 0xffu, 0x0du, 0x0cu, 0xffu, 0x0du, 0xbbu, 0xb1u};
    afe_setup();
    if (!afe_word(0x0080u)) return false;
    for (unsigned reg = 0; reg < 8u; ++reg) {
        uint32_t byte = operating[reg];
        if (reg == 2u || reg == 5u) byte = (byte & ~0x3fu) | gain;
        if (reg == 1u || reg == 4u) byte = offset;    /* stock's calibrated offset slot */
        if (!afe_word((reg << 12) | byte)) return false;
    }
    delay_ticks(CLOCK_TICKS_PER_MS * 2u);
    return true;
}


static void burst_stop(void)
{
    modify(0xc013002cu, 0u, 2u);
    wr(0xc0d6000cu, 0u);
    modify(0xc0d60008u, 0u, 0x80000000u);
    for (unsigned c = 0; c < 4u; ++c) { uint32_t b = 0xc0d50000u + c * 0x100u; wr(b + 8u, 0u); wr(b, 0u); }
    wr(0xc0d70000u, 1u); wr(0xc0d70000u, 0u); wr(0xc0d7001cu, 0u); wr(0xc0d70018u, 0u);
}

/* Three-component (colour) SCNIO configuration on top of the P0 table: the
 * exact register deltas stock writes for component count 3 (fixture case
 * p0-three-components versus p0-gray-4). Each of the three illumination
 * windows spans the whole 0x99..0x7e0 range; `eighths` scales that width. */
static void scnio_colour_config(unsigned eighths)
{
    wr(0xc0d600e0u, 7u);
    modify(0xc0d60130u, 0x1fu, 2u);
    modify(0xc0d601a8u, 0x7u, 2u);
    wr(0xc0d601c0u, 1u); wr(0xc0d601c4u, 2u); wr(0xc0d601c8u, 4u);
    for (unsigned c = 0; c < 3u; ++c) {
        modify(0xc0d60264u + 8u * c, 0xfffffu, 0x99u);
        modify(0xc0d60268u + 8u * c, 0xfffffu, 0x99u + (0x7e0u - 0x99u) * eighths / 8u);
    }
}

/* Arm table without its final three timing enables; period and divider as
 * given, colour routing when three components are configured. */
static void scnio_arm(unsigned components, uint32_t period)
{
    static const uint32_t routing[3] = {0x24924924u, 0x49249249u, 0x92492492u};
    for (unsigned i = 0; i + 3u < scnio_arm_run_N; ++i) {
        uint32_t a = scnio_arm_run[i].addr, set = scnio_arm_run[i].set;
        if (a == 0xc0d60138u) set = period - 1u;
        if (a == 0xc0d60130u) set = components == 3u ? 2u : 0u;         /* d - 1 */
        if (components == 3u && a == 0xc0d601b8u) set = 2u;
        if (components == 3u && a >= 0xc0d601c0u && a <= 0xc0d601c8u) set = routing[(a - 0xc0d601c0u) / 4u];
        modify(a, scnio_arm_run[i].clear, set);
    }
}

static void scanner_burst(void)
{
    static const uint32_t start[3] = {0x99u, 0x2dbu, 0x67eu}, end[3] = {0x2abu, 0x64eu, 0x7dfu};
    const uint32_t R = LINE_BUF;
    const unsigned n = burst_n, C = burst_components, rep = burst_repeat;
    const unsigned lines = n * C;                       /* DMA transfers per channel per repeat */
    const unsigned rows = burst_window ? burst_window : C == 3u ? 3u * n + 1u : n + 1u; /* stock window rows, or the ring-wrap test size */
    const uint32_t window = rows * BURST_ROW;
    uint32_t times[32]; unsigned raw = 0, q = 0, requests = 0, wanted = n * rep;
    bool done = false, prepared = false;
    burst_rows = rows; burst_ok = false;
    dbg_puts("[burst] n="); dbg_dec(n); dbg_puts(" comp="); dbg_dec(C); dbg_puts(" rep="); dbg_dec(rep);
    dbg_puts(" T="); dbg_dec(burst_period); dbg_puts(" exposure="); dbg_dec(burst_exposure);
    dbg_puts("/8 gain="); dbg_dec(burst_gain); dbg_puts(" offset=0x"); dbg_u32(burst_offset);
    dbg_puts(" skip="); dbg_dec(burst_skip); dbg_putc('\n');
    if (!line_primed) {
        dbg_puts("[burst] priming setup\n");
        bool ok = line_setup(C, 1u, 2u, 0x1a748u);        /* throwaway first setup, rail stays up */
        modify(0xc0d6011cu, 0x11111113u, 0u); burst_stop(); line_primed = ok;
        delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    }
    for (unsigned i = 0; i < (window + BURST_GUARD) / 4u; ++i) ((uint32_t *)(uintptr_t)R)[i] = 0xa5a5a5a5u;
    burst_stop();
    modify(0xc0133100u, 0u, 0x10u); wr(0xc0133100u, 8u);
    modify(0xc0133104u, 0u, 0x10u); wr(0xc0133104u, 8u);
    scanner_power(true); delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    afe_setup();
    if (!afe_word(0x0004u) || !afe_word(0x2000u)) goto out;
    delay_ticks(CLOCK_TICKS_PER_MS * 5u);
    modify(0xc0c8121cu, 0u, 0x300u);
    for (unsigned c = 0; c < 4u; ++c) { uint32_t b = 0xc0d50000u + c * 0x100u; wr(b + 8u, 0u); wr(b, 0u); wr(b, 1u); }
    wr(0xc0d70000u, 1u); wr(0xc0d70000u, 0u); wr(0xc0d7001cu, 0u); wr(0xc0d70018u, 0u); wr(0xc0d70004u, 0u);
    modify(0xc0d60008u, 0u, 0x80000000u);
    for (unsigned c = 0; c < 2u; ++c) {
        uint32_t b = 0xc0d50000u + c * 0x100u;
        /* stock C=3: initial pointer two N-row planes in, second skip 2N rows + one segment */
        uint32_t plane = C == 3u ? (2u * n % rows) * BURST_ROW : 0u;
        ldmac_descriptor(c, R + plane + c * BURST_SEG, R, R + window, BURST_SEG);
        wr(b + 0x34u, plane + BURST_SEG);
        modify(b + 0x28u, 0xffffu, lines);
        modify(b + 0x28u, 0xffff0000u, rep << 16);
    }
    for (unsigned c = 2; c < 4u; ++c) { uint32_t b = 0xc0d50000u + c * 0x100u; wr(b + 8u, 0u); wr(b, 0u); }
    apply_seq(scnio_p0, scnio_p0_N);
    /* Stationary: the P0 table clears only bits 0/1 of S+0x11c; the moving-mode
     * bits 12/16 a previous page set made every later stationary capture read
     * exact zeros (2026-10-27, bursts after a page scan). */
    modify(0xc0d6011cu, 0x11111113u, 0u);
    if (C == 3u) scnio_colour_config(burst_exposure);
    else if (burst_exposure < 8u)
        for (unsigned c = 0; c < 3u; ++c) {
            uint32_t width = (end[c] - start[c]) * burst_exposure / 8u;
            modify(0xc0d60264u + 8u * c, 0xfffffu, start[c]);
            modify(0xc0d60268u + 8u * c, 0xfffffu, start[c] + width);
        }
    delay_ticks(CLOCK_TICKS_PER_MS * 2u);
    if (!afe_operating_gain(burst_gain, burst_offset)) goto out;
    for (unsigned c = 0; c < 2u; ++c) {
        uint32_t b = 0xc0d50000u + c * 0x100u, plane = C == 3u ? (2u * n % rows) * BURST_ROW : 0u;
        if (rd(b + 0x14u) != R + plane + c * BURST_SEG || rd(b + 0x18u) != R || rd(b + 0x1cu) != R + window ||
            rd(b + 0x20u) != 0x00010a20u || rd(b + 0x28u) != ((rep << 16) | lines)) goto out;
    }
    if (burst_full_arm) { apply_seq(scnio_arm_run, scnio_arm_run_N); modify(0xc0d60138u, 0xffffffu, burst_period - 1u); if (C == 3u) modify(0xc0d60130u, 0x1fu, 2u); }
    else scnio_arm(C, burst_period);
    prepared = true;
    uint32_t rc0 = rd(0xc0d60010u), t0 = app_clock_ticks();
    if (!burst_full_arm) { modify(0xc0133104u, 0u, 0x10u); modify(0xc0d60158u, 1u, 1u); modify(0xc0130028u, 0u, 2u); }
    if (burst_immediate) { wr(0xc0d6000cu, C == 3u ? 7u : 1u); ++requests; ++q; }
    uint32_t limit = (400u + 2u * burst_skip + (C == 3u ? 400u : 0u) + rep * 100u) * CLOCK_TICKS_PER_MS;
    unsigned logged = 0;
    while (!done && app_clock_ticks() - t0 < limit) {
        if (!(rd(0xc0130020u) & 2u)) continue;
        /* Event times: the first few, then those around the first request. */
        if (logged < ARRAY_SIZE(times) && (raw < 4u || raw + 2u >= burst_skip))
            times[logged++] = app_clock_ticks() - t0;
        if (!burst_no_ack158) modify(0xc0d60158u, 1u, 1u);
        if (raw > burst_skip) {
            if (q < wanted) { wr(0xc0d6000cu, C == 3u ? 7u : 1u); ++requests; }
            else if (q >= wanted + 2u) done = true;
            if (!done) ++q;
        }
        ++raw;
        modify(0xc0133104u, 0u, 0x10u);
    }
    uint32_t rc = rd(0xc0d60010u), ev0 = rd(0xc0d50010u), ev1 = rd(0xc0d50110u), st = rd(0xc0d60008u);
    uint32_t p0 = rd(0xc0d50014u), p1 = rd(0xc0d50114u);
    burst_stop();
    dbg_puts("[burst] done="); dbg_dec(done); dbg_puts(" raw="); dbg_dec(raw); dbg_puts(" requests="); dbg_dec(requests);
    dbg_puts(" readcnt "); dbg_u32(rc0); dbg_puts("->"); dbg_u32(rc);
    dbg_puts(" ev0="); dbg_u32(ev0); dbg_puts(" ev1="); dbg_u32(ev1); dbg_puts(" st="); dbg_u32(st);
    dbg_puts(" ptr0="); dbg_u32(p0 - R); dbg_puts(" ptr1="); dbg_u32(p1 - R); dbg_putc('\n');
    dbg_puts("[burst] event us:");
    for (unsigned i = 0; i < logged; ++i) { dbg_putc(' '); dbg_dec(times[i] / 96u); }
    dbg_putc('\n');
    for (unsigned k = 0; k < rows; ++k)
        for (unsigned c = 0; c < 2u; ++c) {
            const uint16_t *s = (const uint16_t *)(uintptr_t)(R + k * BURST_ROW + c * BURST_SEG);
            unsigned sentinel = 0, clipped = 0, lo = 0xffffu, hi = 0; uint32_t sum = 0;
            for (unsigned j = 0; j < BURST_SEG / 2u; ++j) {
                unsigned v = s[j];
                if (v == 0xa5a5u) { ++sentinel; continue; }
                if (v == 0xffffu) ++clipped;
                if (v < lo) lo = v;
                if (v > hi) hi = v;
                sum += v;
            }
            unsigned valid = BURST_SEG / 2u - sentinel;
            dbg_puts("[burst] row "); dbg_dec(k); dbg_puts(" ch"); dbg_dec(c);
            dbg_puts(" sentinel="); dbg_dec(sentinel); dbg_puts(" clipped="); dbg_dec(clipped);
            dbg_puts(" min="); dbg_dec(valid ? lo : 0); dbg_puts(" max="); dbg_dec(hi);
            dbg_puts(" mean="); dbg_dec(valid ? sum / valid : 0); dbg_putc('\n');
        }
    line_primed = true;                         /* a complete burst primes the front end too */
    bool guard = true;
    for (unsigned i = window; i < window + BURST_GUARD; ++i) if (((const uint8_t *)(uintptr_t)R)[i] != 0xa5u) guard = false;
    dbg_puts("[burst] guard="); dbg_dec(guard); dbg_putc('\n');
    burst_ok = done && guard;
out:
    if (!prepared) { burst_stop(); dbg_puts("[burst] setup failed\n"); }
    if (!burst_keep_power) scanner_power(false);
}

static void line_exposure(unsigned eighths)
{
    static const uint32_t start[3] = {0x99u, 0x2dbu, 0x67eu}, end[3] = {0x2abu, 0x64eu, 0x7dfu};
    for (unsigned c = 0; c < 3u; ++c) {
        uint32_t width = (end[c] - start[c]) * eighths / 8u;
        modify(0xc0d60264u + 8u * c, 0xfffffu, start[c]);
        modify(0xc0d60268u + 8u * c, 0xfffffu, start[c] + width);
    }
}

/* Moving line acquisition into a hardware ring. One setup per page: AFE, P0
 * optics (one or three components), the LDMAC window as a ring whose rows the
 * engine fills and the page consumer releases, then SCNIO moving mode (line
 * period T per sub-period, spatial pitch, positive direction). The carriage
 * travels at constant speed (scanner_motor_scan_move); from the first scan
 * position on, every IRQ19 event requests one line (mask 1, or 7 for three
 * components) and a line counts as complete three events after its request
 * (the stock finite-path drain). Register sequence and formulas: static
 * analysis of the stock firmware plus the fixture's recorded C=3 deltas
 * (emu/stockapp/scanner_fixture.py); the ring/wrap behaviour is measured on
 * hardware with the burst window override (USB 0x5e).
 *
 * Ring geometry: every transfer advances by one component row, as in the
 * verified grey path. Colour stores consecutive R/G/B component rows for
 * each line; the window is a multiple of three rows. The finite stock
 * capture's large planar jumps remain confined to the short burst/calibration
 * path. Small burst tests did not prove those jumps safe for a whole page;
 * long colour pages using them corrupted application RAM on the board.
 * The DMA transfer count is the whole page (16-bit, up to
 * 65535 segments); the page consumer must release line i before line
 * i + capacity - 2 is requested, which scan_page.c enforces by throttling the
 * carriage (lines are position-gated). */
#define LINE_RING_ROWS ((LINE_BUF_BYTES - BURST_GUARD) / BURST_ROW)
#define LINE_ATTEMPTS 3u
#define LINE_WARMUP 128u                    /* stationary P0 periods before any request */
#define LINE_AFE_GAIN 13u
#define LINE_AFE_OFFSET 0x00u
#define LINE_EXPOSURE 6u                    /* eighths of the P0 window widths (grey) */
#define LINE_COLOUR_EXPOSURE 8u             /* eighths of the full-window colour exposure */
static struct {
    bool running, done, failed, gate_pending, validated, overrun;
    unsigned components, lines, requested, q, raw, ready, taken;
    unsigned ring_lines, window_rows;
    uint32_t first, started_ms, budget_ms;
} line;
/* Request event index of each outstanding line, by line mod ring capacity. */
static uint16_t line_request_event[LINE_RING_ROWS];
/* Optics experiment (vendor 0x7f): P0 exposure in eighths and AFE gain code. */
static unsigned line_exposure_eighths = LINE_EXPOSURE, line_colour_exposure = LINE_COLOUR_EXPOSURE, line_gain = LINE_AFE_GAIN;

bool scanner_line_set_afe(unsigned exposure, unsigned gain)
{
    if (line.running || line.gate_pending || exposure < 1u || exposure > 8u || gain < 1u || gain > 13u) return false;
    line_exposure_eighths = exposure; line_gain = gain;
    dbg_puts("[line] afe exposure="); dbg_dec(exposure); dbg_puts("/8 gain="); dbg_dec(gain); dbg_putc('\n');
    return true;
}

unsigned scanner_line_capacity(unsigned components)
{
    return components == 3u ? LINE_RING_ROWS / 3u : LINE_RING_ROWS;
}

/* Buffer row of component `comp` of line `k` (see the ring geometry above). */
static uint32_t line_row_address(unsigned k, unsigned comp)
{
    unsigned row = (k % line.ring_lines) * line.components + comp;
    return LINE_BUF + row * BURST_ROW;
}

void scanner_line_stop(void)
{
    /* Prepare enables timing before running is set; completion may already
     * have cleared running. Always quiesce the hardware, preserving the rows
     * and counters so the host can drain a finished page. */
    modify(0xc0d6011cu, 0x11111113u, 0u);
    modify(0xc0c57400u, 2u, 0u); wr(0xc0c57108u, 0u); modify(0xc0c57404u, 2u, 0u);
    wr(0xc0c57140u, 0u);
    burst_stop();
    line.running = line.gate_pending = false;
    scanner_power(false);
}

static bool row_all_zero(uint32_t address);

/* One setup attempt; on success timing runs at the scan period. */
static bool line_setup(unsigned components, unsigned lines, unsigned pitch, unsigned period)
{
    const uint32_t R = LINE_BUF;
    const unsigned C = components;
    unsigned rows = scanner_line_capacity(C) * C;
    uint32_t window = rows * BURST_ROW;
    unsigned transfers = lines * C;
    line.components = C; line.lines = lines; line.window_rows = rows;
    line.ring_lines = scanner_line_capacity(C);
    if (transfers > 65535u) return false;
    /* Sentinel over the whole window (~40 ms with the instruction cache): a
     * row the DMA never touched stays recognisable in the ring dump. */
    for (uint32_t i = 0; i < window / 4u; ++i) ((uint32_t *)(uintptr_t)R)[i] = 0xa5a5a5a5u;
    burst_stop();
    modify(0xc0133100u, 0u, 0x10u); wr(0xc0133100u, 8u);
    modify(0xc0133104u, 0u, 0x10u); wr(0xc0133104u, 8u);
    scanner_power(true); delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    afe_setup();
    if (!afe_word(0x0004u) || !afe_word(0x2000u)) goto failed;
    delay_ticks(CLOCK_TICKS_PER_MS * 5u);
    modify(0xc0c8121cu, 0u, 0x300u);
    for (unsigned c = 0; c < 4u; ++c) { uint32_t b = 0xc0d50000u + c * 0x100u; wr(b + 8u, 0u); wr(b, 0u); wr(b, 1u); }
    wr(0xc0d70000u, 1u); wr(0xc0d70000u, 0u); wr(0xc0d7001cu, 0u); wr(0xc0d70018u, 0u); wr(0xc0d70004u, 0u);
    modify(0xc0d60008u, 0u, 0x80000000u);
    for (unsigned c = 0; c < 2u; ++c) {
        uint32_t b = 0xc0d50000u + c * 0x100u;
        ldmac_descriptor(c, R + c * BURST_SEG, R, R + window, BURST_SEG);
        modify(b + 0x28u, 0xffffu, transfers);
    }
    for (unsigned c = 2; c < 4u; ++c) { uint32_t b = 0xc0d50000u + c * 0x100u; wr(b + 8u, 0u); wr(b, 0u); }
    apply_seq(scnio_p0, scnio_p0_N);
    modify(0xc0d6011cu, 0x11111113u, 0u);
    if (C == 3u) scnio_colour_config(line_colour_exposure);
    else line_exposure(line_exposure_eighths);
    delay_ticks(CLOCK_TICKS_PER_MS * 2u);
    if (!afe_operating_gain(line_gain, LINE_AFE_OFFSET)) goto failed;
    for (unsigned c = 0; c < 2u; ++c) {
        uint32_t b = 0xc0d50000u + c * 0x100u;
        if (rd(b + 0x14u) != R + c * BURST_SEG || rd(b + 0x18u) != R || rd(b + 0x1cu) != R + window ||
            rd(b + 0x20u) != 0x00010a20u || rd(b + 0x28u) != (0x10000u | transfers)) goto failed;
    }
    scnio_arm(C, 0x1a748u);                  /* P0 sub-period for the warm-up */
    /* Warm-up, stationary: the front end outputs zeros for its first tens of
     * line periods (burst measurements). Run LINE_WARMUP periods, acknowledging
     * events without requests, then switch to the scan period and leave line
     * timing running into the travel. */
    modify(0xc0133104u, 0u, 0x10u); modify(0xc0d60158u, 1u, 1u); modify(0xc0130028u, 0u, 2u);
    uint32_t t0 = app_clock_ticks();
    unsigned events = 0;
    while (events < LINE_WARMUP && app_clock_ticks() - t0 < 1000u * CLOCK_TICKS_PER_MS) {
        if (!(rd(0xc0130020u) & 2u)) continue;
        modify(0xc0d60158u, 1u, 1u);
        modify(0xc0133104u, 0u, 0x10u); ++events;
    }
    if (events < LINE_WARMUP) { dbg_puts("[line] warm-up timeout\n"); goto failed; }
    /* Moving mode (stock f6bda/f6c7a), only after the stationary phase: with
     * the mode bits set and S+0x11c bit0 (set by the position gate) clear,
     * the engine outputs blank lines. Then the scan period and, for three
     * components, the divider d = 3 in S+0x164 as the moving path writes it. */
    modify(0xc0d6011cu, 0x11111113u, 0x11000u);
    modify(0xc0d60120u, 1u, 0u); modify(0xc0d60124u, 1u, 0u);
    modify(0xc0d60128u, 0xffu, 0u); modify(0xc0d6012cu, 1u, 0u);
    modify(0xc0d60134u, 3u, 0u); modify(0xc0d60154u, 0xffffffu, 0u);
    modify(0xc0d60160u, 0xffffu, pitch - 1u);
    modify(0xc0d60164u, 0xffu, C == 3u ? 2u : 0u);
    modify(0xc0d6015cu, 3u, 0u);
    modify(0xc0d60138u, 0xffffffu, period - 1u);
    return true;
failed:
    burst_stop();                            /* rail stays up: the next attempt is a second setup */
    return false;
}

/* Blocking setup (~0.3 s per attempt) with the motor off. `period` is the
 * sub-period T (S+0x138); the line period is T for grey and 3T for colour. */
bool scanner_line_prepare(unsigned components, unsigned lines, unsigned pitch, unsigned period)
{
    if (scanner_motor_busy() || !lines || (components != 1u && components != 3u) || lines * components > 65535u ||
        pitch < 2u || pitch > 65536u || period < 2u || period > 0x1000000u) return false;
    if (!line_primed) {
        memset(&line, 0, sizeof(line));
        dbg_puts("[line] priming setup\n");
        bool ok = line_setup(components, lines, pitch, period);
        modify(0xc0d6011cu, 0x11111113u, 0u); burst_stop();   /* rail stays up */
        line_primed = ok;
        delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    }
    for (unsigned attempt = 1; attempt <= LINE_ATTEMPTS; ++attempt) {
        memset(&line, 0, sizeof(line));
        if (line_setup(components, lines, pitch, period)) return true;
        dbg_puts("[line] setup attempt "); dbg_dec(attempt); dbg_puts(" failed\n");
        delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    }
    return false;
}

/* Arm the position gate and the timing enables; call right before travel.
 * `first` is the absolute position of line 0, `lead` the gate lead distance. */
bool scanner_line_start(uint32_t first, uint32_t lead, uint32_t budget_ms)
{
    const uint32_t P0 = 0xc0c57000u;
    uint32_t x = rd(P0 + 0x100u);
    if (first > 0xffffffu || lead > 0xffffu || first < lead || x > 0xffffffu - lead - 20u) return false;
    line.first = first; line.budget_ms = budget_ms;
    wr(P0 + 0x14cu, x);                                  /* acquisition position seed */
    if (x + lead + 20u > first) {
        modify(0xc0d6011cu, 1u, 1u); wr(P0 + 0x138u, x + lead + 20u);
    } else {                                             /* one-shot lead comparator */
        modify(P0 + 0x400u, 2u, 0u); modify(P0 + 0x404u, 2u, 0u);
        wr(P0 + 0x108u, first - lead);
        modify(P0 + 0x400u, 0u, 2u);
        wr(P0 + 0x138u, first);
        line.gate_pending = true;
    }
    wr(P0 + 0x13cu, 0xffffffu); wr(P0 + 0x140u, 1u);
    line.started_ms = time_ms(); line.running = true;      /* timing runs since prepare */
    dbg_puts("[line] start lines="); dbg_dec(line.lines); dbg_puts(" comp="); dbg_dec(line.components);
    dbg_puts(" ring="); dbg_dec(line.ring_lines); dbg_puts(" first="); dbg_u32(first);
    dbg_puts(" at="); dbg_u32(x); dbg_puts(line.gate_pending ? " gate\n" : " immediate\n");
    return true;
}

/* Sparse check (every 16th word): cheap enough for the motor-polled path. */
static bool row_all_zero(uint32_t address)
{
    const uint32_t *w = (const uint32_t *)(uintptr_t)address;
    for (unsigned i = 0; i < BURST_ROW / 4u; i += 16u) if (w[i] && w[i] != 0xa5a5a5a5u) return false;
    return true;
}

/* Service the gate comparator and at most one IRQ19 event; never waits. */
void scanner_line_poll(void)
{
    const uint32_t P0 = 0xc0c57000u;
    if (!line.running) return;
    if (line.gate_pending && (rd(P0 + 0x404u) & 2u)) {
        modify(P0 + 0x400u, 2u, 0u); wr(P0 + 0x108u, 0u); modify(P0 + 0x404u, 2u, 0u);
        modify(0xc0d6011cu, 1u, 1u);
        line.gate_pending = false;
    }
    if ((uint32_t)(time_ms() - line.started_ms) >= line.budget_ms) {
        dbg_puts("[line] deadline\n"); line.failed = true; scanner_line_stop(); return;
    }
    if (!(rd(0xc0130020u) & 2u)) return;
    uint32_t latched = rd(P0 + 0x14cu);
    modify(0xc0d60158u, 1u, 1u);
    if (latched >= line.first || line.requested == line.lines) {
        if (line.q < line.lines) {
            /* The ring must keep every unreleased line intact: a request that
             * would overwrite one is a page failure, not a silent gap. */
            if (line.requested - line.taken + 2u >= line.ring_lines) {
                line.overrun = true;
            } else {
                wr(0xc0d6000cu, line.components == 3u ? 7u : 1u);
                line_request_event[line.requested % LINE_RING_ROWS] = (uint16_t)line.raw;
                ++line.requested;
                if (line.requested <= 2u || !(line.requested & 255u)) {
                    dbg_puts("[line] request "); dbg_dec(line.requested); dbg_puts(" at ");
                    dbg_u32(latched); dbg_putc('\n');
                }
            }
        } else if (line.q >= line.lines + 2u) line.done = true;
        if (!line.done) ++line.q;
    }
    ++line.raw;
    modify(0xc0133104u, 0u, 0x10u);
    if (line.overrun) {
        dbg_puts("[line] ring overrun at "); dbg_dec(line.requested); dbg_putc('\n');
        line.failed = true; scanner_line_stop(); return;
    }
    while (line.ready < line.requested &&
           (line.done || (uint16_t)(line.raw - line_request_event[line.ready % LINE_RING_ROWS]) >= 3u)) ++line.ready;
    if (line.done) { scanner_line_stop(); dbg_puts("[line] done\n"); }
    /* The front end sometimes outputs exact zeros for a whole setup; check
     * the first two lines once, before any line is handed out, so the page can
     * redo the setup without duplicating output. */
    if (!line.validated && line.ready >= 2u) {
        bool zero = true;
        for (unsigned c = 0; c < line.components; ++c)
            if (!row_all_zero(line_row_address(0, c)) || !row_all_zero(line_row_address(1, c))) zero = false;
        if (zero) {
            dbg_puts("[line] zero output\n"); line.failed = true;
            modify(0xc0d6011cu, 0x11111113u, 0u); burst_stop(); line.running = line.gate_pending = false;   /* rail stays up */
            return;
        }
        line.validated = true;
    }
}

unsigned scanner_line_ready(void) { return line.validated ? line.ready : 0u; }
/* Forget the previous page's rows: a new page must not hand out stale lines
 * while it homes (2026-10-27: an RGB page after a grey one aborted at once
 * because the stale state had one component). */
void scanner_line_reset(void)
{
    if (line.running || line.gate_pending) scanner_line_stop();
    memset(&line, 0, sizeof(line));
}
unsigned scanner_line_backlog(void) { return line.requested - line.taken; }
/* Diagnostic (2026-10-27 freezes between lines 467 and 627 of a colour page):
 * the live LDMAC words of both ring channels, so the last progress line before
 * a freeze shows where the hardware pointer is relative to the ring. */
void scanner_line_dma_report(void)
{
    for (unsigned c = 0; c < 2u; ++c) {
        uint32_t b = 0xc0d50000u + c * 0x100u;
        dbg_puts(c ? " ch1=" : " ch0="); dbg_u32(rd(b + 0x14u)); dbg_putc('/'); dbg_u32(rd(b + 0x10u));
        dbg_putc('/'); dbg_u32(rd(b + 0x0cu)); dbg_putc('/'); dbg_u32(rd(b + 0x24u)); dbg_putc('/'); dbg_u32(rd(b + 0x28u));
    }
}
uint32_t scanner_line_status_word(void) { return (line.requested & 0xfffu) | (line.ready & 0xfffu) << 12 | (line.taken & 0xffu) << 24; }
unsigned scanner_line_ring(void) { return line.ring_lines; }

/* The consumer is done with line `k` (lines are released in order). */
void scanner_line_release(unsigned k)
{
    if (k == line.taken && k < line.ready) ++line.taken;
}

/* Diagnostic read of the raw ring (vendor 0x5d), in 256-byte chunks. */
unsigned scanner_line_dump(uint8_t *out, uint32_t chunk, unsigned max)
{
    uint32_t offset = chunk * 256u, end = line.window_rows * BURST_ROW;
    if (line.running || offset >= end) return 0;
    unsigned n = end - offset < max ? end - offset : max;
    memcpy(out, (const uint8_t *)(uintptr_t)(LINE_BUF + offset), n);
    return n;
}
bool scanner_line_done(void) { return line.done && line.validated && line.ready == line.lines; }
bool scanner_line_failed(void) { return line.failed; }
const uint16_t *scanner_line_row(unsigned k, unsigned comp)
{
    return k < scanner_line_ready() && comp < line.components ? (const uint16_t *)(uintptr_t)line_row_address(k, comp) : NULL;
}


/* Dark/white reference rows for per-sample shading (readable C replacement
 * for stock's white/black arrays, brief 21 §5): with the carriage parked at
 * the mechanical home (the lid's white strip under the sensor), capture
 * CAL_LINES lines with the illumination windows closed (dark) and with the
 * page's exposure (white), and average them per sample and component. The
 * page consumer builds its correction tables from these (scan_source). */
#define CAL_LINES 8u
static uint16_t cal_dark[3][BURST_SEG], cal_white[3][BURST_SEG];
static unsigned cal_components;
static bool cal_valid;

static bool cal_average(uint16_t out[3][BURST_SEG], unsigned C, unsigned n)
{
    unsigned rows = C == 3u ? 3u * n + 1u : n + 1u;
    for (unsigned k = 0; k < C; ++k) {
        for (unsigned x = 0; x < BURST_SEG; ++x) {
            uint32_t sum = 0;
            for (unsigned i = 0; i < n; ++i) {
                unsigned row = C == 3u ? (2u * n + i + k * (2u * n + 1u)) % rows : i;
                const uint16_t *r = (const uint16_t *)(uintptr_t)(LINE_BUF + row * BURST_ROW);
                /* A row the DMA never wrote still holds the sentinel in every
                 * sample; a single sample equal to 0xa5a5 is ordinary data. */
                if (x == 0 && r[0] == 0xa5a5u && r[1] == 0xa5a5u && r[2] == 0xa5a5u && r[3] == 0xa5a5u && r[1295] == 0xa5a5u) {
                    dbg_puts("[cal] row "); dbg_dec(row); dbg_puts(" never written\n");
                    return false;
                }
                sum += r[x];
            }
            out[k][x] = (uint16_t)(sum / n);
            if (!(x & 255u)) app_yield();
        }
    }
    return true;
}

bool scanner_calibrate(unsigned components)
{
    if (scanner_motor_busy() || (components != 1u && components != 3u)) return false;
    cal_valid = false; cal_components = components;
    unsigned saved_n = burst_n, saved_e = burst_exposure, saved_g = burst_gain, saved_s = burst_skip, saved_o = burst_offset;
    unsigned saved_c = burst_components, saved_r = burst_repeat, saved_w = burst_window; uint32_t saved_p = burst_period;
    burst_n = CAL_LINES; burst_gain = line_gain; burst_skip = 0; burst_offset = LINE_AFE_OFFSET;
    burst_components = components; burst_repeat = 1; burst_window = 0; burst_period = 0x1a748u; burst_keep_power = true;
    bool ok = true;
    burst_exposure = 0;                                   /* windows closed: dark reference */
    scanner_burst(); if (!burst_ok) dbg_puts("[cal] dark burst incomplete\n");
    ok = burst_ok && cal_average(cal_dark, components, CAL_LINES);
    burst_exposure = components == 3u ? line_colour_exposure : line_exposure_eighths;
    scanner_burst(); if (!burst_ok) dbg_puts("[cal] white burst incomplete\n");
    ok = ok && burst_ok && cal_average(cal_white, components, CAL_LINES);
    burst_keep_power = false; scanner_power(false);
    burst_n = saved_n; burst_exposure = saved_e; burst_gain = saved_g; burst_skip = saved_s; burst_offset = saved_o;
    burst_components = saved_c; burst_repeat = saved_r; burst_window = saved_w; burst_period = saved_p;
    if (ok) {
        /* The mechanical home is not a verified white reference (helper
         * handoff, 2026-10-27): accept the white rows only when the central
         * 80 % of the line is uniform (max <= 1.5 x min) and well above dark. */
        unsigned lo = 0xffffu, hi = 0, dlo = 0xffffu, dhi = 0;
        for (unsigned x = BURST_SEG / 10u; x < BURST_SEG - BURST_SEG / 10u; ++x) {
            for (unsigned c = 0; c < components; ++c) {
                if (cal_white[c][x] < lo) lo = cal_white[c][x];
                if (cal_white[c][x] > hi) hi = cal_white[c][x];
                if (cal_dark[c][x] < dlo) dlo = cal_dark[c][x];
                if (cal_dark[c][x] > dhi) dhi = cal_dark[c][x];
            }
        }
        bool uniform = hi <= lo + lo / 2u && lo > dhi + 4096u;
        dbg_puts("[cal] comp="); dbg_dec(components); dbg_puts(" white "); dbg_dec(lo); dbg_putc('-'); dbg_dec(hi);
        dbg_puts(" dark "); dbg_dec(dlo); dbg_putc('-'); dbg_dec(dhi); dbg_puts(uniform ? " ok\n" : " not a uniform reference: shading off\n");
        ok = uniform;
    } else dbg_puts("[cal] failed\n");
    cal_valid = ok;
    return ok;
}
bool scanner_cal_valid(unsigned components) { return cal_valid && cal_components == components; }
const uint16_t *scanner_cal_row(unsigned white, unsigned comp) { return comp < 3u ? (white ? cal_white : cal_dark)[comp] : NULL; }

void scanner_burst_request(unsigned n, unsigned exposure, unsigned gain, unsigned skip, unsigned offset)
{
    burst_n = n; burst_exposure = exposure; burst_gain = gain; burst_skip = skip;
    burst_offset = offset; burst_pending = true;
}

bool scanner_burst_options(unsigned components, unsigned repeat, uint32_t period, unsigned window, unsigned flags)
{
    if ((components != 1u && components != 3u) || repeat < 1u || repeat > 4u || (period && (period < 0x1000u || period > 0x1000000u)) || window > 3u * BURST_MAX + 1u) return false;
    burst_components = components; burst_repeat = repeat; burst_period = period ? period : 0x1a748u; burst_window = window;
    burst_immediate = (flags & 4u) != 0; burst_no_ack158 = (flags & 2u) != 0; burst_full_arm = (flags & 1u) != 0;
    return true;
}

unsigned scanner_burst_dump(uint8_t *out, unsigned chunk, unsigned max)
{
    unsigned offset = chunk * 256u, size = burst_rows * BURST_ROW + BURST_GUARD;
    if (burst_pending || offset >= size) return 0;
    unsigned n = size - offset;
    if (n > max) n = max;
    memcpy(out, (const uint8_t *)(uintptr_t)(LINE_BUF + offset), n);
    return n;
}

/* Continuous CIS illumination (motor-free). Reuses the stationary-capture light
 * path (scanner power + AFE + SCNIO arm/run with a long integration) but leaves
 * the engine running and re-triggers integration each poll so the LEDs stay
 * visibly lit. Toggled by the Cancel key. NEVER writes the motor
 * (0xc0c56000/0xc0c57000). */
static bool illum_on;
static uint32_t illum_next;

static void illum_start(void)
{
    const uint32_t R = (uint32_t)(uintptr_t)cap_buf;
    dbg_puts("[scan] illumination ON\n");
    scanner_power(true);                                     /* scanner power */
    delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    afe_setup(); afe_word(0x0004u); afe_word(0x2000u);
    delay_ticks(CLOCK_TICKS_PER_MS * 5u);
    modify(0xc0c8121cu, 0u, 0x300u);
    for (unsigned ch = 0; ch < 4; ++ch) { uint32_t b = 0xc0d50000u + ch * 0x100u; wr(b + 8, 0u); wr(b, 0u); wr(b, 1u); }
    wr(0xc0d70000u, 1u); wr(0xc0d70000u, 0u); wr(0xc0d7001cu, 0u); wr(0xc0d70018u, 0u); wr(0xc0d70004u, 0u);
    modify(0xc0d60008u, 0u, 0x80000000u);
    ldmac_descriptor(0, R,          R, R + 0x3000u, 0xc00u);
    ldmac_descriptor(1, R + 0xc00u, R, R + 0x3000u, 0xc00u);
    wr(0xc0d50208u, 0u); wr(0xc0d50200u, 0u); wr(0xc0d50308u, 0u); wr(0xc0d50300u, 0u);
    apply_seq(scnio_p7, scnio_p7_N);
    modify(0xc0d60264u, 0xfffffu, 0x0400u); modify(0xc0d60268u, 0xfffffu, 0x0800u);  /* long integration */
    modify(0xc0d6026cu, 0xfffffu, 0x0c00u); modify(0xc0d60270u, 0xfffffu, 0x1000u);
    modify(0xc0d60274u, 0xfffffu, 0x1400u); modify(0xc0d60278u, 0xfffffu, 0x1800u);
    delay_ticks(CLOCK_TICKS_PER_MS * 2u);
    afe_operating();
    apply_seq(scnio_arm_run, scnio_arm_run_N);
    wr(0xc0d6000cu, 1u);                                     /* first integration */
    illum_on = true; illum_next = time_ms() + 30u;
}

static void illum_stop(void)
{
    dbg_puts("[scan] illumination OFF\n");
    apply_seq(scnio_stop, scnio_stop_N);
    scanner_power(false);
    illum_on = false;
}

void scanner_lamp_off(void)
{
    if (illum_on) illum_stop();
    else { apply_seq(scnio_stop, scnio_stop_N); scanner_power(false); }
}

/* Page row: optical P0 with stock exposure. Scanner power, AFE set-up and the
 * 20 ms lamp settle happen on a page's first row only; power stays on until
 * the page ends (scanner_lamp_off). Every row re-arms SCNIO/DMA, runs the AFE
 * operating step and keeps the physically tested 80 ms drain and checks. */
bool scanner_capture_row(bool first)
{
    if (scanner_motor_busy()) return false;
    if (first) scanner_lamp_off();
    scanner_full_capture(first ? 0x46u | CAPTURE_KEEP_POWER | CAPTURE_QUIET
                               : 0x40u | CAPTURE_AFE_RUN | CAPTURE_QUIET);
    return last_capture_ok;
}

void scanner_lamp_toggle(void) { if (illum_on) illum_stop(); else illum_start(); }

void scanner_lamp_poll(void)
{
    if (illum_on && (int32_t)(time_ms() - illum_next) >= 0) {
        wr(0xc0d6000cu, 1u);                                 /* sustain -> LED stays lit */
        illum_next = time_ms() + 30u;
    }
}

const uint16_t *scanner_row(void) { return (const uint16_t *)(uintptr_t)cap_buf; }

unsigned scanner_dump(uint8_t *out, unsigned chunk, unsigned max)
{
    unsigned offset = chunk * 256u;
    if (offset >= sizeof(cap_buf)) return 0;
    unsigned n = sizeof(cap_buf) - offset;
    if (n > max) n = max;
    memcpy(out, cap_buf + offset, n);
    return n;
}

void scanner_poll(void)
{
    scanner_lamp_poll();   /* sustain continuous illumination if the lamp is on */
    if (capture2_pending) { capture2_pending = false; scanner_full_capture(capture2_variant); }
    if (burst_pending) { burst_pending = false; scanner_burst(); }
}
