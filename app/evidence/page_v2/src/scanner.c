/* Scanner (CIS) bring-up — reverse-engineering support.
 *
 * Includes register probes, stationary optical/diagnostic capture, and lamp
 * control. Motor control and the page sequence live in scanner_motor.c and
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

static const uint32_t peek_regs[] = {
    /* controller + channel 0 block */
    0xc0120000u, 0xc0120004u, 0xc0120008u, 0xc012000cu, 0xc0120010u,
    0xc012001cu, 0xc0120024u,
    /* channel 1 block (the chip-type transaction uses this one) */
    0xc0120100u, 0xc0120104u, 0xc0120108u, 0xc012010cu, 0xc0120110u,
    0xc0120114u, 0xc012011cu, 0xc0120124u,
    /* strobe/clock and GPIO blocks the stock transaction touches */
    0xc0c840c0u, 0xc0c840c4u,
    0xc0c81200u, 0xc0c81204u, 0xc0c8121cu,
};

/* The real pixel-capture engine family at 0xc0d60000 (RE: scanner_re_pi.md).
 * All reads; config/state registers only (no clear-on-read FIFO touched). */
static const uint32_t cap_regs[] = {
    0xc0d60000u, 0xc0d60008u, 0xc0d60080u, 0xc0d60084u,  /* enable/ack/clear */
    0xc0d60130u,                                         /* +0x30: dpi index bits5-8 */
    0xc0d6015cu, 0xc0d60164u,                            /* 0x140 blk: size, line count-1 */
    0xc0d601c0u, 0xc0d601d4u, 0xc0d601d8u, 0xc0d601dcu,  /* engine: base,start,status,dest */
    0xc0d601e0u, 0xc0d601e4u, 0xc0d601e8u,               /* count, progress, mode */
    0xc0d601ecu, 0xc0d601f0u, 0xc0d601f4u,               /* three 16:16 timing pairs */
    0xc0d60294u, 0xc0d602c0u,                            /* CIS clock/timing mux */
    0xc0c85614u,                                         /* scanner power (+0x14) */
};

static bool peek_pending, probe_pending, bringup_pending, capture_pending, enable_pending, field_pending, ldmac_pending, dmasetup_pending, capture2_pending;

/* DMA target for a capture. The engine would write pixels here; we only ever
 * start a capture once the dest register has latched to THIS buffer (see the
 * guard in scanner_capture), so an errant DMA cannot land anywhere else. */
static uint8_t cap_buf[0x4000] ALIGNED;   /* covers the profile-7 R+0x3000 window */

void scanner_request(void) { peek_pending = true; }
void scanner_probe_request(void) { probe_pending = true; }
void scanner_bringup_request(void) { bringup_pending = true; }
void scanner_capture_request(void) { capture_pending = true; }
void scanner_enable_request(void) { enable_pending = true; }
void scanner_field_request(void) { field_pending = true; }
void scanner_ldmac_request(void) { ldmac_pending = true; }
void scanner_dmasetup_request(void) { dmasetup_pending = true; }

/* Write a config value then read it back, logging both. Decides whether the
 * scanner block is clock-gated (writes ignored, reads 0) or merely
 * unconfigured (the value sticks). These are the same writes the stock
 * transaction routine 0xf3732 performs on the channel-1 block, so they are
 * safe to issue; we do not start a transfer or poll any unbounded status. */
static void wrb(uint32_t addr, uint32_t value)
{
    wr(addr, value);
    dbg_puts("  wr "); dbg_u32(addr); dbg_puts(" <= 0x"); dbg_u32(value);
    dbg_puts("  rd => 0x"); dbg_u32(rd(addr)); dbg_putc('\n');
}

static void scanner_probe(void)
{
    /* NON-ACTUATING probe: write-readback of the capture engine's config-holder
     * registers ONLY. We do NOT power the scanner (0xc0c85614), do NOT set the
     * engine enable (0xc0d60000/+8), and do NOT set the start bit (+0x14), so
     * nothing moves and no capture runs. If the written values stick, the block
     * is powered/clocked; if they read back 0, it is clock-gated and we must
     * find its enable before any capture. 0xc0d601dc(+0x1c) is a dest *pointer*
     * holder here (a scratch RAM address), harmless without a start. */
    dbg_puts("[scan] capture-engine config write-readback (non-actuating)\n");
    wrb(0xc0d601e8u, 0x00000003u);   /* +0x28 mode holder */
    wrb(0xc0d601dcu, 0x20300000u);   /* +0x1c dest ptr holder (scratch RAM) */
    wrb(0xc0d601e0u, 0x00000640u);   /* +0x20 count holder */
    wrb(0xc0d601ecu, 0x12345678u);   /* +0x2c timing-pair holder */
    wrb(0xc0d60164u, 0x0000ff00u);   /* 0x140+0x24 line-count holder */
    wrb(0xc0d60130u, 0x00000020u);   /* 0x100+0x30 dpi index holder */
    wrb(0xc0d60084u, 0x00000000u);   /* +0x84 cleared each block (stock) */
    dbg_puts("[scan] probe done\n");
}

static void scanner_peek(void)
{
    dbg_puts("[scan] serial controller 0xc0120000:\n");
    for (unsigned i = 0; i < ARRAY_SIZE(peek_regs); ++i) {
        dbg_puts("  ");
        dbg_u32(peek_regs[i]); dbg_puts(" = 0x"); dbg_u32(rd(peek_regs[i])); dbg_putc('\n');
    }
    dbg_puts("[scan] capture engine 0xc0d60000:\n");
    for (unsigned i = 0; i < ARRAY_SIZE(cap_regs); ++i) {
        dbg_puts("  ");
        dbg_u32(cap_regs[i]); dbg_puts(" = 0x"); dbg_u32(rd(cap_regs[i])); dbg_putc('\n');
    }
    dbg_puts("[scan] peek done\n");
}

/* Engine bring-up: enable the capture block and power the scanner, then re-test
 * the registers that were dark in the non-actuating probe. This turns the
 * scanner subsystem on (the CIS lamp may illuminate) but does NOT start a
 * capture (no +0x14 write) and does NOT move the motor. Bounded throughout. */
static void scanner_bringup(void)
{
    dbg_puts("[scan] bring-up: before power ");
    dbg_puts("c0c85614=0x"); dbg_u32(rd(0xc0c85614u));
    dbg_puts(" c0d60000=0x"); dbg_u32(rd(0xc0d60000u)); dbg_putc('\n');

    wr(0xc0d60000u, 1u);                 /* engine enable  */
    wr(0xc0d60008u, 1u);                 /* engine ack/clear */
    wr(0xc0c85614u, 1u);                 /* scanner power on (lamp may light) */
    delay_ticks(CLOCK_TICKS_PER_MS * 10u);   /* 10ms settle */

    dbg_puts("[scan] bring-up: after power ");
    dbg_puts("c0c85614=0x"); dbg_u32(rd(0xc0c85614u));
    dbg_puts(" c0d60000=0x"); dbg_u32(rd(0xc0d60000u));
    dbg_puts(" c0d60008=0x"); dbg_u32(rd(0xc0d60008u)); dbg_putc('\n');

    dbg_puts("[scan] retest previously-dark registers:\n");
    wrb(0xc0d601dcu, 0x20300000u);       /* +0x1c dest holder */
    wrb(0xc0d60164u, 0x0000ff00u);       /* 0x140+0x24 line count */
    wrb(0xc0d60130u, 0x00000020u);       /* 0x100+0x30 dpi index */

    wr(0xc0c85614u, 0u);                 /* power the scanner back off (lamp off) */
    dbg_puts("[scan] bring-up done (scanner powered back off)\n");
}

/* Guarded stationary single-block capture. Follows the stock 0xffa18 register
 * order but writes ONLY capture-path registers (never the motor blocks
 * 0xc0d60400/0x440), targets our own cap_buf, and starts the engine ONLY if the
 * destination register latched to cap_buf (otherwise it aborts before start, so
 * no DMA can target an unknown address). The completion poll is bounded, so a
 * non-completing engine cannot hang the management loop. Lamp is powered back
 * off at the end. No carriage motion is possible: no motor register is written. */
static void scanner_capture(void)
{
    const uint32_t dest = (uint32_t)(uintptr_t)cap_buf;
    dbg_puts("[scan] stationary capture (guarded, no motor)\n");
    memset(cap_buf, 0xa5, sizeof(cap_buf));          /* sentinel to detect DMA */

    wr(0xc0c85614u, 1u);                             /* scanner power / lamp on */
    wr(0xc0d60084u, 0u);                             /* clear */
    wr(0xc0d60294u, 0x1111u);                        /* CIS clock/timing mux */
    wr(0xc0d601e8u, 0x00000003u);                    /* +0x28 mode */
    wr(0xc0d601e0u, 0x00000100u);                    /* +0x20 count */
    wr(0xc0d601ecu, 0x00100010u);                    /* +0x2c timing pair (placeholder) */
    wr(0xc0d601f0u, 0x00100010u);                    /* +0x30 */
    wr(0xc0d601f4u, 0x00100010u);                    /* +0x34 */
    wr(0xc0d60000u, 1u); wr(0xc0d60008u, 1u);        /* engine enable/ack */
    wr(0xc0d601dcu, dest);                           /* +0x1c dest pointer */

    uint32_t rb = rd(0xc0d601dcu);
    dbg_puts("  dest wrote=0x"); dbg_u32(dest);
    dbg_puts(" read=0x"); dbg_u32(rb); dbg_putc('\n');
    if (rb != dest) {
        dbg_puts("  dest did NOT latch -> abort BEFORE start (no DMA, safe)\n");
        wr(0xc0c85614u, 0u);
        dbg_puts("[scan] capture aborted\n");
        return;
    }

    dbg_puts("  dest latched -> starting capture\n");
    wr(0xc0d601d4u, 1u);                             /* +0x14 start */
    uint32_t t0 = time_ms();
    bool done = false;
    while ((uint32_t)(time_ms() - t0) < 60u) {        /* bounded 60 ms */
        if (!(rd(0xc0d601d4u) & 1u)) { done = true; break; }
    }
    uint32_t status = rd(0xc0d601d8u);
    wr(0xc0c85614u, 0u);                             /* lamp back off */

    dbg_puts(done ? "  completed" : "  TIMEOUT (bounded)");
    dbg_puts(" status=0x"); dbg_u32(status); dbg_putc('\n');
    dbg_puts("  buf[0..31]:");
    static const char h[] = "0123456789abcdef";
    for (unsigned i = 0; i < 32; ++i) {
        dbg_putc(' '); dbg_putc(h[cap_buf[i] >> 4]); dbg_putc(h[cap_buf[i] & 15]);
    }
    dbg_putc('\n');
    dbg_puts("[scan] capture done\n");
}

/* Enable the engine + configure the CIS mux (as stock does at the start of a
 * capture block), then re-test whether the dark dpi/line-count/dest registers
 * now latch. Bounded, no capture start (+0x14 untouched), no motor register. */
static void scanner_enable_test(void)
{
    dbg_puts("[scan] clock-gate + enable + mux, then re-test dark sub-blocks\n");
    wr(0xc0c85614u, 1u);                 /* scanner power / lamp on */
    /* Peripheral clock-gate the stock scanner path sets (bit 0x10 of
     * 0xc0133100 at scan start, 0xc0133104 in scanner init). RMW mirrors stock. */
    modify(0xc0133100u, 0u, 0x10u);
    modify(0xc0133104u, 0u, 0x10u);
    dbg_puts("  clkgate c0133100=0x"); dbg_u32(rd(0xc0133100u));
    dbg_puts(" c0133104=0x"); dbg_u32(rd(0xc0133104u)); dbg_putc('\n');
    wr(0xc0d60084u, 0u);                 /* clear */
    wr(0xc0d60000u, 1u); wr(0xc0d60008u, 1u);   /* control/engine enable */
    wr(0xc0d60294u, 0x1111u);            /* CIS clock/timing mux */
    wr(0xc0d602c8u, 0u);
    modify(0xc0d602c0u, 0u, 0x7u);       /* per-colour enable bits 1|2|4 */
    delay_ticks(CLOCK_TICKS_PER_MS * 5u);

    wrb(0xc0d60130u, 0x00000020u);       /* dpi (0x100+0x30) */
    wrb(0xc0d60164u, 0x0000ff00u);       /* line count (0x140+0x24) */
    wrb(0xc0d601dcu, 0x20023040u);       /* dest offset (0x1c0+0x1c) */
    dbg_puts("  ctrl c0d60000=0x"); dbg_u32(rd(0xc0d60000u));
    dbg_puts(" +8=0x"); dbg_u32(rd(0xc0d60008u));
    dbg_puts(" mux c0d60294=0x"); dbg_u32(rd(0xc0d60294u));
    dbg_puts(" c0d602c0=0x"); dbg_u32(rd(0xc0d602c0u)); dbg_putc('\n');

    wr(0xc0c85614u, 0u);                 /* power back off */
    dbg_puts("[scan] enable+mux test done\n");
}

/* Correct-field latch test (fixes the earlier measurement error: the dpi field
 * is bits 0-4, line-count is bits 0-7; prior probes wrote bits outside them).
 * Writes value 1 into the LOW field and reads it back, first with no enable then
 * with the SCNIO engine enabled, saving/restoring the originals. Bounded; no
 * scan start, no lamp, no motor-capable write. */
static void scanner_field_test(void)
{
    uint32_t s130 = rd(0xc0d60130u), s164 = rd(0xc0d60164u);
    dbg_puts("[scan] correct-field latch test\n");
    dbg_puts("  saved 130=0x"); dbg_u32(s130); dbg_puts(" 164=0x"); dbg_u32(s164); dbg_putc('\n');

    wr(0xc0d60130u, (s130 & ~0x1fu) | 1u);
    wr(0xc0d60164u, (s164 & ~0xffu) | 1u);
    dbg_puts("  no-enable: 130[0:4]=0x"); dbg_u32(rd(0xc0d60130u) & 0x1fu);
    dbg_puts(" 164[0:7]=0x"); dbg_u32(rd(0xc0d60164u) & 0xffu); dbg_putc('\n');

    wr(0xc0d60130u, s130); wr(0xc0d60164u, s164);
    wr(0xc0d60000u, 1u); wr(0xc0d60008u, 1u);           /* SCNIO engine enable */
    wr(0xc0d60130u, (s130 & ~0x1fu) | 1u);
    wr(0xc0d60164u, (s164 & ~0xffu) | 1u);
    dbg_puts("  enabled:   130[0:4]=0x"); dbg_u32(rd(0xc0d60130u) & 0x1fu);
    dbg_puts(" 164[0:7]=0x"); dbg_u32(rd(0xc0d60164u) & 0xffu); dbg_putc('\n');

    wr(0xc0d60130u, s130); wr(0xc0d60164u, s164);         /* restore */
    dbg_puts("[scan] field test done\n");
}

/* Characterise the real pixel-DMA block (LDMAC 4ch @ 0xc0d50000..0x300, BUFADM
 * @ 0xc0d70000) on hardware: peek the channel registers, then write-readback the
 * channel-0 address-candidate registers (+0x14/0x18/0x1c/0x20) to see which hold
 * a full 32-bit value (address) vs a masked value (length/ctrl). NO DMA trigger
 * and NO motor: setting a descriptor register without an enable/start moves
 * nothing and transfers nothing. Originals saved/restored. */
static void scanner_ldmac_test(void)
{
    static const uint32_t off[] = { 0x00u, 0x04u, 0x08u, 0x14u, 0x18u, 0x1cu, 0x20u, 0x24u };
    dbg_puts("[scan] LDMAC/BUFADM peek\n");
    dbg_puts("  BUFADM c0d70000=0x"); dbg_u32(rd(0xc0d70000u)); dbg_putc('\n');
    for (unsigned ch = 0; ch < 4; ++ch) {
        uint32_t base = 0xc0d50000u + ch * 0x100u;
        dbg_puts("  ch"); dbg_dec(ch); dbg_puts(":");
        for (unsigned i = 0; i < ARRAY_SIZE(off); ++i) {
            dbg_putc(' '); dbg_u32(off[i]); dbg_putc('=');
            dbg_u32(rd(base + off[i]));
        }
        dbg_putc('\n');
    }
    /* Channel-0 init/enable (f1f18: +0x08=0; +0x00=0; +0x00=1) — the step the
     * first probe skipped. This only enables the channel's register access; it
     * does NOT arm (+0x08[1:0]=1) and does NOT trigger any DMA. No motor. */
    wr(0xc0d50008u, 0u); wr(0xc0d50000u, 0u); wr(0xc0d50000u, 1u);
    dbg_puts("  after ch0 enable: +00=0x"); dbg_u32(rd(0xc0d50000u));
    dbg_puts(" +08=0x"); dbg_u32(rd(0xc0d50008u)); dbg_putc('\n');

    dbg_puts("[scan] ch0 address-field write-readback (enabled, no trigger):\n");
    const uint32_t test = 0x20023040u;
    static const uint32_t areg[] = { 0x14u, 0x18u, 0x1cu, 0x20u };
    for (unsigned i = 0; i < ARRAY_SIZE(areg); ++i) {
        uint32_t a = 0xc0d50000u + areg[i];
        uint32_t saved = rd(a);
        wr(a, test);
        dbg_puts("  +"); dbg_u32(areg[i]); dbg_puts(" <= 0x"); dbg_u32(test);
        dbg_puts(" rd => 0x"); dbg_u32(rd(a)); dbg_putc('\n');
        wr(a, saved);
    }
    wr(0xc0d50008u, 0u); wr(0xc0d50000u, 0u);     /* disable ch0 again */
    dbg_puts("[scan] ldmac test done\n");
}

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

/* Validate the LDMAC descriptor programming on hardware: program the two-channel
 * profile-7 descriptors into a RAM buffer, read them back, then disable the
 * channels. No SCNIO run, no arm, no trigger, no motor — descriptor setup only.
 * The SCNIO/AFE config + run/request to actually move pixels comes next (from the
 * codex recipe). */
static void scanner_dma_setup(void)
{
    const uint32_t R = (uint32_t)(uintptr_t)cap_buf;
    dbg_puts("[scan] LDMAC descriptor programming (no run, no motor)\n");
    dbg_puts("  R=0x"); dbg_u32(R); dbg_putc('\n');
    memset(cap_buf, 0xa5, sizeof(cap_buf));
    ldmac_descriptor(0, R,           R, R + 0x3000u, 0xc00u);
    ldmac_descriptor(1, R + 0xc00u,  R, R + 0x3000u, 0xc00u);
    for (unsigned ch = 0; ch < 2; ++ch) {
        uint32_t b = 0xc0d50000u + ch * 0x100u;
        dbg_puts("  ch"); dbg_dec(ch);
        dbg_puts(" +08="); dbg_u32(rd(b + 0x08));
        dbg_puts(" +14="); dbg_u32(rd(b + 0x14));
        dbg_puts(" +1c="); dbg_u32(rd(b + 0x1c));
        dbg_puts(" +20="); dbg_u32(rd(b + 0x20));
        dbg_puts(" +28="); dbg_u32(rd(b + 0x28)); dbg_putc('\n');
    }
    wr(0xc0d50008u, 0u); wr(0xc0d50000u, 0u);     /* disable ch0 */
    wr(0xc0d50108u, 0u); wr(0xc0d50100u, 0u);     /* disable ch1 */
    dbg_puts("[scan] dma setup done\n");
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
static void scanner_full_capture(unsigned v)
{
    const uint32_t R = (uint32_t)(uintptr_t)cap_buf;
    bool optical = (v & 0x40u) != 0;
    uint32_t samples = optical ? 0xa20u : 0xc00u;
    uint32_t window = optical ? 0x2880u : 0x3000u;
    last_capture_ok = false;
    dbg_puts("[scan] FULL capture v=0x"); dbg_u32(v); dbg_puts(" R=0x"); dbg_u32(R); dbg_putc('\n');
    memset(cap_buf, 0xa5, sizeof(cap_buf));

    modify(0xc0133100u, 0u, 0x10u); wr(0xc0133100u, 8u);     /* A.1 IRQ18 src */
    modify(0xc0133104u, 0u, 0x10u); wr(0xc0133104u, 8u);     /* A.1 IRQ19 src */

    if (v & 0x02u) {
        dbg_puts("  AFE serial config (type0)\n");
        afe_setup(); afe_word(0x0004u); afe_word(0x2000u);
        delay_ticks(CLOCK_TICKS_PER_MS * 5u);
        modify(0xc0c8121cu, 0u, 0x300u);
    }
    if (v & 0x04u) {
        dbg_puts("  lamp / scanner power ON\n");
        wr(0xc0c85614u, 1u);
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
    if (v & 0x02u) afe_operating();                           /* A.4 operating AFE */
    /* Actual DMA registers, not the legacy scalar at SCNIO+0x1dc. */
    if (rd(0xc0d50014u) != R || rd(0xc0d50114u) != R + samples ||
        rd(0xc0d5001cu) != R + window || rd(0xc0d5011cu) != R + window) {
        apply_seq(scnio_stop, scnio_stop_N); wr(0xc0c85614u, 0u);
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
    if (v & 0x04u) wr(0xc0c85614u, 0u);                       /* lamp off */

    const uint16_t *s = (const uint16_t *)(uintptr_t)cap_buf;
    unsigned nz = 0, bad = 0, changed[2] = {0, 0};
    for (unsigned i = 0; i < samples; ++i) {
        if (s[i] != 0xa5a5u) { ++nz; ++changed[i < samples / 2u ? 0 : 1]; }
        if ((i & 0x100u) ? (s[i] < 0xb000u) : (s[i] > 0x4000u)) ++bad;
    }
    bool guard = true;
    for (unsigned i = 0x3000u; i < sizeof(cap_buf); ++i)
        if (cap_buf[i] != 0xa5u) guard = false;
    /* Fixed 80ms drain is the physically tested contract; progress alone is
     * not a DMA fence. Require new data in BOTH segments and intact guard. */
    last_capture_ok = rc != rc0 && (ev0 & 4u) && (ev1 & 4u) &&
                      changed[0] >= samples / 2u - 6u && changed[1] >= samples / 2u - 6u && guard;
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
    wr(0xc0c85614u, 1u);                                     /* scanner power */
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
    wr(0xc0c85614u, 0u);
    illum_on = false;
}

void scanner_lamp_off(void)
{
    if (illum_on) illum_stop();
    else { apply_seq(scnio_stop, scnio_stop_N); wr(0xc0c85614u, 0u); }
}

bool scanner_capture_row(void)
{
    if (scanner_motor_busy()) return false;
    scanner_lamp_off();
    scanner_full_capture(0x46u); /* optical P0 + AFE + power; stock exposure */
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
    if (peek_pending) { peek_pending = false; scanner_peek(); }
    if (probe_pending) { probe_pending = false; scanner_probe(); }
    if (bringup_pending) { bringup_pending = false; scanner_bringup(); }
    if (capture_pending) { capture_pending = false; scanner_capture(); }
    if (enable_pending) { enable_pending = false; scanner_enable_test(); }
    if (field_pending) { field_pending = false; scanner_field_test(); }
    if (ldmac_pending) { ldmac_pending = false; scanner_ldmac_test(); }
    if (dmasetup_pending) { dmasetup_pending = false; scanner_dma_setup(); }
}
