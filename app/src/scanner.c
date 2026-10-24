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
    if (v & (0x02u | CAPTURE_AFE_RUN)) afe_operating();       /* A.4 operating AFE */
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
    if ((v & 0x04u) && !(v & CAPTURE_KEEP_POWER)) wr(0xc0c85614u, 0u);   /* lamp off */

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
#define BURST_MAX 8u
#define BURST_ROW 0x1440u
#define BURST_SEG 0x0a20u
#define BURST_GUARD 256u
static uint8_t burst_buf[(BURST_MAX + 1u) * BURST_ROW + BURST_GUARD] ALIGNED;
static unsigned burst_n, burst_exposure, burst_gain, burst_skip, burst_offset;
static bool burst_pending;

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

/* Hardware observation (2026-10-24, bursts on paper after webhook power
 * cycles): the FIRST capture setup after chip power-on outputs exact zeros
 * for every line, whether or not the carriage moved; every later setup, after
 * a complete power-off of the scanner block, outputs data. The mechanism is
 * not identified. A throwaway setup once per boot (~0.2 s) replaces the
 * ~3.5 s re-home retry that every first page used to take. */
static bool line_primed;

static void burst_stop(void)
{
    modify(0xc013002cu, 0u, 2u);
    wr(0xc0d6000cu, 0u);
    modify(0xc0d60008u, 0u, 0x80000000u);
    for (unsigned c = 0; c < 4u; ++c) { uint32_t b = 0xc0d50000u + c * 0x100u; wr(b + 8u, 0u); wr(b, 0u); }
    wr(0xc0d70000u, 1u); wr(0xc0d70000u, 0u); wr(0xc0d7001cu, 0u); wr(0xc0d70018u, 0u);
}

static void scanner_burst(void)
{
    static const uint32_t start[3] = {0x99u, 0x2dbu, 0x67eu}, end[3] = {0x2abu, 0x64eu, 0x7dfu};
    const uint32_t R = (uint32_t)(uintptr_t)burst_buf;
    unsigned n = burst_n, window = (n + 1u) * BURST_ROW;
    uint32_t times[24]; unsigned raw = 0, q = 0, requests = 0;
    bool done = false, prepared = false;
    dbg_puts("[burst] n="); dbg_dec(n); dbg_puts(" exposure="); dbg_dec(burst_exposure);
    dbg_puts("/8 gain="); dbg_dec(burst_gain); dbg_puts(" offset=0x"); dbg_u32(burst_offset);
    dbg_puts(" skip="); dbg_dec(burst_skip); dbg_putc('\n');
    for (unsigned i = 0; i < sizeof(burst_buf) / 4u; ++i) ((uint32_t *)(uintptr_t)burst_buf)[i] = 0xa5a5a5a5u;
    burst_stop();
    modify(0xc0133100u, 0u, 0x10u); wr(0xc0133100u, 8u);
    modify(0xc0133104u, 0u, 0x10u); wr(0xc0133104u, 8u);
    wr(0xc0c85614u, 1u); delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    afe_setup();
    if (!afe_word(0x0004u) || !afe_word(0x2000u)) goto out;
    delay_ticks(CLOCK_TICKS_PER_MS * 5u);
    modify(0xc0c8121cu, 0u, 0x300u);
    for (unsigned c = 0; c < 4u; ++c) { uint32_t b = 0xc0d50000u + c * 0x100u; wr(b + 8u, 0u); wr(b, 0u); wr(b, 1u); }
    wr(0xc0d70000u, 1u); wr(0xc0d70000u, 0u); wr(0xc0d7001cu, 0u); wr(0xc0d70018u, 0u); wr(0xc0d70004u, 0u);
    modify(0xc0d60008u, 0u, 0x80000000u);
    for (unsigned c = 0; c < 2u; ++c) {
        ldmac_descriptor(c, R + c * BURST_SEG, R, R + window, BURST_SEG);
        modify(0xc0d50000u + c * 0x100u + 0x28u, 0xffffu, n);
    }
    for (unsigned c = 2; c < 4u; ++c) { uint32_t b = 0xc0d50000u + c * 0x100u; wr(b + 8u, 0u); wr(b, 0u); }
    apply_seq(scnio_p0, scnio_p0_N);
    if (burst_exposure < 8u)
        for (unsigned c = 0; c < 3u; ++c) {
            uint32_t width = (end[c] - start[c]) * burst_exposure / 8u;
            modify(0xc0d60264u + 8u * c, 0xfffffu, start[c]);
            modify(0xc0d60268u + 8u * c, 0xfffffu, start[c] + width);
        }
    delay_ticks(CLOCK_TICKS_PER_MS * 2u);
    if (!afe_operating_gain(burst_gain, burst_offset)) goto out;
    for (unsigned c = 0; c < 2u; ++c) {
        uint32_t b = 0xc0d50000u + c * 0x100u;
        if (rd(b + 0x14u) != R + c * BURST_SEG || rd(b + 0x18u) != R || rd(b + 0x1cu) != R + window ||
            rd(b + 0x20u) != 0x00010a20u || rd(b + 0x28u) != (0x10000u | n)) goto out;
    }
    /* Arm table without its final three timing enables; P0 period and d=1. */
    for (unsigned i = 0; i + 3u < scnio_arm_run_N; ++i) {
        uint32_t set = scnio_arm_run[i].set;
        if (scnio_arm_run[i].addr == 0xc0d60138u) set = 0x1a747u;
        if (scnio_arm_run[i].addr == 0xc0d60130u) set = 0u;
        modify(scnio_arm_run[i].addr, scnio_arm_run[i].clear, set);
    }
    prepared = true;
    uint32_t rc0 = rd(0xc0d60010u), t0 = app_clock_ticks();
    modify(0xc0133104u, 0u, 0x10u); modify(0xc0d60158u, 1u, 1u); modify(0xc0130028u, 0u, 2u);
    uint32_t limit = (400u + 2u * burst_skip) * CLOCK_TICKS_PER_MS;
    unsigned logged = 0;
    while (!done && app_clock_ticks() - t0 < limit) {
        if (!(rd(0xc0130020u) & 2u)) continue;
        /* Event times: the first few, then those around the first request. */
        if (logged < ARRAY_SIZE(times) && (raw < 4u || raw + 2u >= burst_skip))
            times[logged++] = app_clock_ticks() - t0;
        modify(0xc0d60158u, 1u, 1u);
        if (raw > burst_skip) {
            if (q < n) { wr(0xc0d6000cu, 1u); ++requests; }
            else if (q >= n + 2u) done = true;
            if (!done) ++q;
        }
        ++raw;
        modify(0xc0133104u, 0u, 0x10u);
    }
    uint32_t rc = rd(0xc0d60010u), ev0 = rd(0xc0d50010u), ev1 = rd(0xc0d50110u), st = rd(0xc0d60008u);
    burst_stop();
    dbg_puts("[burst] done="); dbg_dec(done); dbg_puts(" raw="); dbg_dec(raw); dbg_puts(" requests="); dbg_dec(requests);
    dbg_puts(" readcnt "); dbg_u32(rc0); dbg_puts("->"); dbg_u32(rc);
    dbg_puts(" ev0="); dbg_u32(ev0); dbg_puts(" ev1="); dbg_u32(ev1); dbg_puts(" st="); dbg_u32(st); dbg_putc('\n');
    dbg_puts("[burst] event us:");
    for (unsigned i = 0; i < logged; ++i) { dbg_putc(' '); dbg_dec(times[i] / 96u); }
    dbg_putc('\n');
    for (unsigned k = 0; k <= n; ++k)
        for (unsigned c = 0; c < 2u; ++c) {
            const uint16_t *s = (const uint16_t *)(uintptr_t)(burst_buf + k * BURST_ROW + c * BURST_SEG);
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
    for (unsigned i = window; i < sizeof(burst_buf); ++i) if (burst_buf[i] != 0xa5u) guard = false;
    dbg_puts("[burst] guard="); dbg_dec(guard); dbg_putc('\n');
out:
    if (!prepared) { burst_stop(); dbg_puts("[burst] setup failed\n"); }
    wr(0xc0c85614u, 0u);
}

/* Continuous (moving) line acquisition. One setup per page: AFE, P0 optics,
 * LDMAC for `rows` lines at the 0x1440 stride into a DRAM buffer above the
 * app, then SCNIO in moving mode: line period T, spatial pitch, positive
 * direction. The carriage travels at constant speed (scanner_motor_scan_move);
 * from the first scan position on, every IRQ19 event requests one line (the
 * stock normal-scan pattern), and a line counts as complete three events
 * after its request (the stock finite-path drain). Register sequence and
 * formulas: static analysis of the stock firmware; AFE offset 0, gain 13 and
 * exposure 6/8 come from the burst measurements (offset 0xff saturates). */
#define LINE_BUF 0x20280000u                /* above the app, below the stack */
#define LINE_BUF_BYTES 0x480000u
#define LINE_PROBE_MAX 4u                   /* optional stationary probe lines */
#define LINE_PROBE_DRAIN_MAX 32u
#define LINE_ROWS_MAX ((LINE_BUF_BYTES - BURST_GUARD) / BURST_ROW - 1u - LINE_PROBE_MAX)   /* + probes + reserved */
#define LINE_ATTEMPTS 3u
#define LINE_WARMUP 128u                    /* stationary P0 periods before any request */
#define LINE_AFE_GAIN 13u
#define LINE_AFE_OFFSET 0x00u
#define LINE_EXPOSURE 6u                    /* eighths of the P0 window widths */
static struct {
    bool running, done, failed, gate_pending, validated;
    unsigned rows, requested, q, raw, ready;
    unsigned probe;                         /* buffer rows before scan line 0 */
    uint32_t first, started_ms, budget_ms;
    uint32_t last_latched;
} line;
static uint16_t line_request_event[LINE_ROWS_MAX];
/* Stationary probe experiment (vendor 0x7d): request `probe_lines` lines after
 * the warm-up, wait `probe_drain` more events, then require the last probe
 * row to be non-zero before the carriage moves. Default off: on hardware a
 * single probe line came back as exact zeros on every attempt (2026-10-24)
 * while the probe-free build scanned, so the zero-output check happens on the
 * first two moving lines instead (scanner_line_poll). */
static unsigned probe_lines = 0u, probe_drain = 3u;
/* Optics experiment (vendor 0x7f): P0 exposure in eighths and AFE gain code. */
static unsigned line_exposure_eighths = LINE_EXPOSURE, line_gain = LINE_AFE_GAIN;

bool scanner_line_set_afe(unsigned exposure, unsigned gain)
{
    if (line.running || line.gate_pending || exposure < 1u || exposure > 8u || gain < 1u || gain > 13u) return false;
    line_exposure_eighths = exposure; line_gain = gain;
    dbg_puts("[line] afe exposure="); dbg_dec(exposure); dbg_puts("/8 gain="); dbg_dec(gain); dbg_putc('\n');
    return true;
}

unsigned scanner_line_max_rows(void) { return LINE_ROWS_MAX; }

bool scanner_line_probe_config(unsigned lines, unsigned drain)
{
    if (line.running || line.gate_pending || lines > LINE_PROBE_MAX || drain > LINE_PROBE_DRAIN_MAX) return false;
    probe_lines = lines; probe_drain = drain;
    dbg_puts("[line] probe lines="); dbg_dec(lines); dbg_puts(" drain="); dbg_dec(drain); dbg_putc('\n');
    return true;
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

void scanner_line_stop(void)
{
    if (line.running || line.gate_pending) {
        modify(0xc0d6011cu, 1u, 0u);
        modify(0xc0c57400u, 2u, 0u); wr(0xc0c57108u, 0u); modify(0xc0c57404u, 2u, 0u);
        wr(0xc0c57140u, 0u);
        burst_stop();
    }
    line.running = line.gate_pending = false;
    wr(0xc0c85614u, 0u);
}

static bool row_all_zero(unsigned k);
static unsigned row_sparse_max(unsigned k);

/* One setup attempt; on success timing runs at the scan period. Buffer rows
 * 0..P-1 receive the optional stationary probe lines after the warm-up; scan
 * line k lands in buffer row k + P (P = line.probe, 0 by default). */
static bool line_setup(unsigned rows, unsigned pitch, unsigned period)
{
    const uint32_t R = LINE_BUF;
    const unsigned P = line.probe, D = probe_drain;
    uint32_t window = (rows + P + 1u) * BURST_ROW;
    /* Sentinel only where it is checked: probe rows and the first scan rows
     * (filling the whole 4.5 MB window costs seconds in uncached DRAM). */
    for (uint32_t i = 0; i < (P + 4u) * BURST_ROW / 4u; ++i) ((uint32_t *)(uintptr_t)R)[i] = 0xa5a5a5a5u;
    burst_stop();
    modify(0xc0133100u, 0u, 0x10u); wr(0xc0133100u, 8u);
    modify(0xc0133104u, 0u, 0x10u); wr(0xc0133104u, 8u);
    wr(0xc0c85614u, 1u); delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    afe_setup();
    if (!afe_word(0x0004u) || !afe_word(0x2000u)) goto failed;
    delay_ticks(CLOCK_TICKS_PER_MS * 5u);
    modify(0xc0c8121cu, 0u, 0x300u);
    for (unsigned c = 0; c < 4u; ++c) { uint32_t b = 0xc0d50000u + c * 0x100u; wr(b + 8u, 0u); wr(b, 0u); wr(b, 1u); }
    wr(0xc0d70000u, 1u); wr(0xc0d70000u, 0u); wr(0xc0d7001cu, 0u); wr(0xc0d70018u, 0u); wr(0xc0d70004u, 0u);
    modify(0xc0d60008u, 0u, 0x80000000u);
    for (unsigned c = 0; c < 2u; ++c) {
        ldmac_descriptor(c, R + c * BURST_SEG, R, R + window, BURST_SEG);
        modify(0xc0d50000u + c * 0x100u + 0x28u, 0xffffu, rows + P);
    }
    for (unsigned c = 2; c < 4u; ++c) { uint32_t b = 0xc0d50000u + c * 0x100u; wr(b + 8u, 0u); wr(b, 0u); }
    apply_seq(scnio_p0, scnio_p0_N);
    line_exposure(line_exposure_eighths);
    delay_ticks(CLOCK_TICKS_PER_MS * 2u);
    if (!afe_operating_gain(line_gain, LINE_AFE_OFFSET)) goto failed;
    for (unsigned c = 0; c < 2u; ++c) {
        uint32_t b = 0xc0d50000u + c * 0x100u;
        if (rd(b + 0x14u) != R + c * BURST_SEG || rd(b + 0x18u) != R || rd(b + 0x1cu) != R + window ||
            rd(b + 0x20u) != 0x00010a20u || rd(b + 0x28u) != (0x10000u | (rows + P))) goto failed;
    }
    for (unsigned i = 0; i + 3u < scnio_arm_run_N; ++i) {
        uint32_t set = scnio_arm_run[i].set;
        if (scnio_arm_run[i].addr == 0xc0d60138u) set = 0x1a747u;   /* P0 period for warm-up */
        if (scnio_arm_run[i].addr == 0xc0d60130u) set = 0u;         /* d = 1 */
        modify(scnio_arm_run[i].addr, scnio_arm_run[i].clear, set);
    }
    /* Warm-up, stationary: the front end outputs zeros for its first tens of
     * line periods (burst measurements). Run LINE_WARMUP short P0 periods
     * (~145 ms), acknowledging events without requests, optionally take the
     * probe lines, then switch to the scan period and leave line timing
     * running into the travel. */
    modify(0xc0133104u, 0u, 0x10u); modify(0xc0d60158u, 1u, 1u); modify(0xc0130028u, 0u, 2u);
    uint32_t t0 = app_clock_ticks();
    unsigned events = 0, needed = LINE_WARMUP + (P ? P + D : 0u);
    while (events < needed && app_clock_ticks() - t0 < 500u * CLOCK_TICKS_PER_MS) {
        if (!(rd(0xc0130020u) & 2u)) continue;
        modify(0xc0d60158u, 1u, 1u);
        if (events >= LINE_WARMUP && events < LINE_WARMUP + P) wr(0xc0d6000cu, 1u);   /* a probe line */
        modify(0xc0133104u, 0u, 0x10u); ++events;
    }
    if (events < needed) { dbg_puts("[line] warm-up timeout\n"); goto failed; }
    if (P) {
        unsigned peak = row_sparse_max(P - 1u);
        dbg_puts("[line] probe row "); dbg_dec(P - 1u); dbg_puts(" max="); dbg_dec(peak); dbg_putc('\n');
        if (!peak) goto failed;
    }
    /* Moving mode (stock f6bda/f6c7a), only after the stationary phase: with
     * the mode bits set and S+0x11c bit0 (set by the position gate) clear, the
     * engine outputs blank lines. Then the scan period. */
    modify(0xc0d6011cu, 0x11111113u, 0x11000u);
    modify(0xc0d60120u, 1u, 0u); modify(0xc0d60124u, 1u, 0u);
    modify(0xc0d60128u, 0xffu, 0u); modify(0xc0d6012cu, 1u, 0u);
    modify(0xc0d60134u, 3u, 0u); modify(0xc0d60154u, 0xffffffu, 0u);
    modify(0xc0d60160u, 0xffffu, pitch - 1u);
    modify(0xc0d60164u, 0xffu, 0u);
    modify(0xc0d6015cu, 3u, 0u);
    modify(0xc0d60138u, 0xffffffu, period - 1u);
    return true;
failed:
    burst_stop(); wr(0xc0c85614u, 0u);
    return false;
}

/* Blocking setup (~0.2 s per attempt) with the motor off. */
bool scanner_line_prepare(unsigned rows, unsigned pitch, unsigned period)
{
    if (scanner_motor_busy() || !rows || rows > LINE_ROWS_MAX || pitch < 2u || pitch > 65536u ||
        period < 2u || period > 0x1000000u) return false;
    if (!line_primed) {
        memset(&line, 0, sizeof(line));
        line.rows = rows;                       /* no probe lines: nothing is read back */
        dbg_puts("[line] priming setup\n");
        (void)line_setup(rows, pitch, period);
        modify(0xc0d6011cu, 1u, 0u); burst_stop(); wr(0xc0c85614u, 0u);
        line_primed = true;
        delay_ticks(CLOCK_TICKS_PER_MS * 20u);
    }
    for (unsigned attempt = 1; attempt <= LINE_ATTEMPTS; ++attempt) {
        memset(&line, 0, sizeof(line));
        line.rows = rows; line.probe = probe_lines;
        if (line_setup(rows, pitch, period)) return true;
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
    dbg_puts("[line] start rows="); dbg_dec(line.rows); dbg_puts(" first="); dbg_u32(first);
    dbg_puts(" at="); dbg_u32(x); dbg_puts(line.gate_pending ? " gate\n" : " immediate\n");
    return true;
}

/* Sparse check (every 16th word): cheap enough for the motor-polled path. */
static bool row_all_zero(unsigned k)        /* buffer row, not scan line */
{
    const uint32_t *w = (const uint32_t *)(uintptr_t)(LINE_BUF + k * BURST_ROW);
    for (unsigned i = 0; i < BURST_ROW / 4u; i += 16u) if (w[i]) return false;
    return true;
}

/* Largest sample among every 32nd one of a buffer row (sentinel words count). */
static unsigned row_sparse_max(unsigned k)
{
    const uint16_t *s = (const uint16_t *)(uintptr_t)(LINE_BUF + k * BURST_ROW);
    unsigned peak = 0;
    for (unsigned i = 0; i < BURST_ROW / 2u; i += 32u) if (s[i] > peak) peak = s[i];
    return peak;
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
    if (latched >= line.first || line.requested == line.rows) {
        if (line.q < line.rows) {
            wr(0xc0d6000cu, 1u);
            line_request_event[line.requested++] = (uint16_t)line.raw;
            if (line.requested <= 2u || !(line.requested & 127u)) {
                dbg_puts("[line] request "); dbg_dec(line.requested); dbg_puts(" at ");
                dbg_u32(latched); dbg_putc('\n');
            }
        } else if (line.q >= line.rows + 2u) line.done = true;
        if (!line.done) ++line.q;
    }
    ++line.raw;
    modify(0xc0133104u, 0u, 0x10u);
    while (line.ready < line.requested &&
           (line.done || (uint16_t)(line.raw - line_request_event[line.ready]) >= 3u)) ++line.ready;
    if (line.done) { burst_stop(); line.running = false; dbg_puts("[line] done\n"); }
    /* The front end sometimes outputs exact zeros for a whole setup; check
     * the first two lines once, before any line is handed out, so the page can
     * redo the setup without duplicating output. */
    if (!line.validated && line.ready >= 2u) {
        if (row_all_zero(line.probe) && row_all_zero(line.probe + 1u)) {
            dbg_puts("[line] zero output\n"); line.failed = true; scanner_line_stop(); return;
        }
        line.validated = true;
    }
}

unsigned scanner_line_ready(void) { return line.validated ? line.ready : 0u; }

/* Diagnostic read of the raw line buffer (vendor 0x5d), in 256-byte chunks. */
unsigned scanner_line_dump(uint8_t *out, uint32_t chunk, unsigned max)
{
    uint32_t offset = chunk * 256u, end = (line.rows + line.probe + 1u) * BURST_ROW;
    if (line.running || offset >= end) return 0;
    unsigned n = end - offset < max ? end - offset : max;
    memcpy(out, (const uint8_t *)(uintptr_t)(LINE_BUF + offset), n);
    return n;
}
bool scanner_line_done(void) { return line.done && line.validated && line.ready == line.rows; }
bool scanner_line_failed(void) { return line.failed; }
const uint16_t *scanner_line_row(unsigned k)
{
    return k < scanner_line_ready() ? (const uint16_t *)(uintptr_t)(LINE_BUF + (k + line.probe) * BURST_ROW) : NULL;
}

void scanner_burst_request(unsigned n, unsigned exposure, unsigned gain, unsigned skip, unsigned offset)
{
    burst_n = n; burst_exposure = exposure; burst_gain = gain; burst_skip = skip;
    burst_offset = offset; burst_pending = true;
}

unsigned scanner_burst_dump(uint8_t *out, unsigned chunk, unsigned max)
{
    unsigned offset = chunk * 256u;
    if (offset >= sizeof(burst_buf)) return 0;
    unsigned n = sizeof(burst_buf) - offset;
    if (n > max) n = max;
    memcpy(out, burst_buf + offset, n);
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
    if (peek_pending) { peek_pending = false; scanner_peek(); }
    if (probe_pending) { probe_pending = false; scanner_probe(); }
    if (bringup_pending) { bringup_pending = false; scanner_bringup(); }
    if (capture_pending) { capture_pending = false; scanner_capture(); }
    if (enable_pending) { enable_pending = false; scanner_enable_test(); }
    if (field_pending) { field_pending = false; scanner_field_test(); }
    if (ldmac_pending) { ldmac_pending = false; scanner_ldmac_test(); }
    if (dmasetup_pending) { dmasetup_pending = false; scanner_dma_setup(); }
}
