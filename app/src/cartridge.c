/* Print-cartridge fuse-ROM identification (read-only) with the stock head
 * interface power sequence.
 *
 * Each MG2550s cartridge carries a small one-time-programmable "fuse ROM" that
 * the ASIC samples one bit at a time: a selector word chooses a fuse, a trigger
 * pulses the sense circuit and a GPIO input returns the fuse state (active low).
 * The 32 black / 48 colour bits carry a cartridge ID, a destination code, head
 * rank values and the seven-bit field the stock firmware labels INKLVL.
 *
 * The first physical attempt (evidence/CARTRIDGE.md) read a stuck-low sense
 * line because the head interface was never prepared. The sequence below adds
 * the stock preparation, translated register for register from the traced
 * contract in fw_dump/gpt/BRIEF_mode4_head_power.md (response section) and
 * fw_dump/PROGRESS.md section 1k:
 *
 *   1. connection-response test (stock 0xde4f8): GPIO loop through the head
 *      connector, result per head;
 *   2. cold preparation (stock 0xe1764): head GPIO bank controls, 0xc0c84664
 *      and 0xc0c84654 raised, then a nominal 500 ms settle;
 *   3. acquire "mode 4" (stock 0xe19ae): selector clear, bank bits, 0xc0c84604
 *      raised, nominal 2 ms settle;
 *   4. the fuse read itself, nominal 300 us per sample (stock timer 900 at 3 MHz);
 *   5. release (0xc0c84604 low, nominal 3 ms) and, unless asked to stay
 *      powered, the stock shutdown (0xe1860) that returns every control to 0.
 *
 * Stock sequences its waits with timer 3 and IRQ 3 and an RTOS event; nothing
 * but the CPU consumes that timer, so this file replaces it with bounded
 * software delays and keeps USB management polled during the long ones. The
 * RTOS delay unit D(n) is not proved; it is treated as milliseconds and the
 * long settles are doubled. All of this is stock behaviour at every boot and
 * before every stock fuse read; it is not a nozzle-firing path.
 *
 * This module only reads. The stock programming path additionally raises GPIO
 * 0xc0c84624 bit 0 (fuse-cut enable) and uses a four-times longer pulse. This
 * file never writes that GPIO or a non-zero pair word, so it cannot cut fuses. */
#include "app.h"

#define FUSE_ENABLE  0xc0c2954cu
#define FUSE_SELECT  0xc0c29540u
#define FUSE_PAIR    0xc0c29550u
#define FUSE_TRIGGER 0xc0c29544u
#define FUSE_SENSE   0xc0c84618u   /* bit 0: sampled fuse, active low */

/* Head interface controls named by the stock trace (roles inferred). */
#define HEAD_CTL_ACQ  0xc0c84604u  /* 1 while a head operation holds the interface */
#define HEAD_CTL_COLD 0xc0c84654u  /* 1 after cold preparation */
#define HEAD_CTL_RES  0xc0c84664u  /* 1 while the shared head GPIO resource is held */
#define BANK_ROUTE    0xc0c812c0u  /* head GPIO bank: bit 0 cleared by acquire */
#define BANK_CFG      0xc0c812c4u  /* head GPIO bank: bit 0 set by acquire, bit 6 by G+ */
#define CONN_IN_BLACK 0xc0c84638u
#define CONN_IN_COLOUR 0xc0c84648u

#define CART_WAIT_UNIT (CLOCK_TICKS_PER_MS / 100u)   /* host wait unit: 10 us */
#define CART_WAIT_DEFAULT 30u                        /* 300 us per sample, stock nominal */
#define CART_WAIT_MAX 200u                           /* 2 ms per sample */

/* Options in the request's high byte. */
#define CART_OPT_RAW   0x100u   /* skip the power sequence (first-attempt behaviour) */
#define CART_OPT_KEEP  0x200u   /* leave the head interface prepared after the read */
#define CART_OPT_DIAG  0x400u   /* log register read-backs and a sense-line pulse capture */
#define CART_OPT_NO_HEADINIT 0x800u /* skip the stock head-block initialization (experiment only) */

/* Selector base per group of eight bits; bit k uses base + 0x10 * (k % 8).
 * Black groups 4 and 5 repeat groups 0 and 1; only bits 0..31 are read. */
static const uint16_t selector_base[2][6] = {
    { 0x641, 0x643, 0x541, 0x543, 0x641, 0x643 },   /* head 0: pigment black */
    { 0x441, 0x443, 0x343, 0x341, 0x243, 0x241 },   /* head 1: colour */
};

struct head_result { uint32_t low, low_repeat, high, sense_idle; };
static struct head_result result[2];
static uint32_t state;              /* 0 never run, 1 pending, 2 done */
static uint32_t heads_done, wait_ticks, sequence, connection, options_used;
static unsigned pending_heads, pending_wait, pending_options;
static bool prepared;               /* head interface left in the stock "ready" state */
static bool head_initialized;       /* stock head-block configuration written since boot */

/* Millisecond waits that keep USB management alive (not re-entrant into USB:
 * cartridge_poll runs from the main loop, never from inside usb_poll). */
static void wait_ms(unsigned ms)
{
    while (ms--) { delay_ticks(CLOCK_TICKS_PER_MS); usb_poll(); leds_tick(); }
}

static uint32_t selector(unsigned head, unsigned bit)
{ return selector_base[head][bit / 8] + 0x10u * (bit % 8); }

/* Stock G+ (0xde2a8): take the shared head GPIO resource. */
static void gpio_take(void)
{
    modify(0xc0c81584u, 0x83u, 0); modify(0xc0c815c4u, 0x0cu, 0); modify(BANK_CFG, 0, 0x40u);
    wr(0xc0c81594u, 0x83u); wr(HEAD_CTL_RES, 1); wait_ms(6);
    wr(0xc0c81588u, 0x83u); modify(0xc0c81584u, 0, 0x83u);
    wr(0xc0c815c8u, 0x0cu); modify(0xc0c815c4u, 0, 0x0cu); wait_ms(10);
}

/* Stock G- (0xde2ea), last user: give the shared head GPIO resource back. */
static void gpio_give(void)
{
    wr(0xc0c81584u, 0); wr(0xc0c815c4u, 0); wr(0xc0c81604u, 0); wr(0xc0c81644u, 0);
    wr(0xc0c81580u, 0); wr(0xc0c815c0u, 0); wr(0xc0c81600u, 0); wr(0xc0c81640u, 0);
    wr(0xc0c81594u, 0); wr(HEAD_CTL_RES, 0); wait_ms(6);
    wr(0xc0c81588u, 0); wr(0xc0c815c8u, 0);
    modify(0xc0c81584u, 0, 0x83u); modify(0xc0c815c4u, 0, 0x0cu); wait_ms(41);
}

static void conn_outputs(uint32_t v)
{
    static const uint32_t outs[] = { 0xc0c85c24u, 0xc0c85c34u, 0xc0c85c44u, 0xc0c85c54u,
                                     0xc0c85c64u, 0xc0c85e04u, 0xc0c85e14u };
    for (unsigned i = 0; i < ARRAY_SIZE(outs); ++i) wr(outs[i], v);
}

/* Stock connection-response test (0xde4f8). Returns black | colour << 8,
 * 1 = every expected response seen, 2 = at least one mismatch. */
static uint32_t connection_test(void)
{
    unsigned black = 1, colour = 1;
    wr(0xc0c81584u, 0); wr(0xc0c815c4u, 0); wr(0xc0c81604u, 0); wr(0xc0c81644u, 0);
    wr(0xc0c81580u, 0); wr(0xc0c815c0u, 0); wr(0xc0c81600u, 0); wr(0xc0c81640u, 0);
    gpio_take(); wait_ms(6);
    conn_outputs(0); wr(0xc0c81608u, 0); wr(0xc0c81648u, 0);
    modify(0xc0c81584u, 0, 0x7cu); modify(0xc0c815c4u, 0, 3u);
    modify(0xc0c81604u, 0, 1u); modify(0xc0c81644u, 0, 1u);
    wait_ms(4);
    if (rd(CONN_IN_BLACK) != 1) black = 2;
    if (rd(CONN_IN_COLOUR) != 1) colour = 2;
    conn_outputs(1); wr(0xc0c81608u, 1);
    wait_ms(4);
    if (rd(CONN_IN_BLACK) != 0) black = 2;
    if (rd(CONN_IN_COLOUR) != 0) colour = 2;
    wr(0xc0c85e04u, 0); wr(0xc0c85e14u, 0);
    wait_ms(4);
    if (rd(CONN_IN_BLACK) != 1) black = 2;
    if (rd(CONN_IN_COLOUR) != 1) colour = 2;
    modify(0xc0c81584u, 0x7cu, 0); modify(0xc0c815c4u, 3u, 0);
    modify(0xc0c81604u, 1u, 0); modify(0xc0c81644u, 1u, 0);
    gpio_give();
    return black | colour << 8;
}

/* Stock cold preparation (0xe1764) after a connection test: ends in the
 * "ready" state stock keeps for the rest of its uptime. */
static void cold_prepare(void)
{
    gpio_take(); wait_ms(12);
    wr(0xc0c8159cu, 0); wr(0xc0c81580u, 0x7cu); wr(0xc0c815c0u, 3u);
    wr(0xc0c81600u, 1u); wr(0xc0c81640u, 1u);
    wait_ms(20);
    wr(HEAD_CTL_COLD, 1);
    wait_ms(600);                 /* stock: five 100 ms timer intervals */
    prepared = true;
}

/* Stock mode-4 acquire (0xe19ae) from the ready state. */
static void acquire(void)
{
    wr(FUSE_SELECT, 0); wr(FUSE_PAIR, 0);
    wr(HEAD_CTL_ACQ, rd(HEAD_CTL_ACQ));
    modify(BANK_CFG, 0, 1u); modify(BANK_ROUTE, 1u, 0);
    wr(HEAD_CTL_ACQ, 1);
    wait_ms(4);                   /* stock: 6000 counts at 3 MHz = 2 ms */
}

/* Stock mode-4 release (0xe18c8) plus the completion its timer provides. */
static void release(void)
{
    wr(HEAD_CTL_ACQ, 0);
    wait_ms(6);                   /* stock: 9010 counts at 3 MHz = 3 ms */
}

/* Stock shutdown from the ready state (0xe1860): every control back to 0. */
static void shutdown(void)
{
    wr(HEAD_CTL_ACQ, 0); modify(BANK_ROUTE, 1u, 0); wr(HEAD_CTL_COLD, 0);
    wait_ms(350);                 /* stock: D(151) + D(20) */
    gpio_give();
    prepared = false;
}

/* Sample bits first..last (inclusive, at most 32) and return the complemented
 * packed word. Like stock, unsampled upper bits come back as ones. */
static uint32_t read_bits(unsigned head, unsigned first, unsigned last)
{
    uint32_t packed = 0;
    wr(FUSE_ENABLE, 1); wr(FUSE_SELECT, 0); wr(FUSE_PAIR, 0); wr(FUSE_TRIGGER, 1);
    delay_ticks(wait_ticks);
    for (unsigned bit = first; bit <= last; ++bit) {
        wr(FUSE_SELECT, selector(head, bit)); wr(FUSE_PAIR, 0); wr(FUSE_TRIGGER, 1);
        delay_ticks(wait_ticks);
        packed |= (rd(FUSE_SENSE) & 1u) << (bit - first);
    }
    wr(FUSE_SELECT, 0); wr(FUSE_PAIR, 0); wr(FUSE_ENABLE, 0);
    return ~packed;
}

/* Stock head-block initialization (0xc3096): the fixed words, values recomputed
 * from this dump's ROM tables by the RE answer (fw_dump/gpt/BRIEF_head_supply.md,
 * Q3). Field meanings are not established. Physically decisive: without the
 * head_cfg3 group the fuse transaction produced no sense response at all
 * (evidence/CARTRIDGE.md). Configuration only: no data, trigger or power write. */
static const uint32_t head_cfg[][2] = {
    { 0xc0c29588u, 0 }, { 0xc0c29590u, 0x14040007u }, { 0xc0c29598u, 0x00000700u },
    { 0xc0c295a0u, 0x1f07000bu }, { 0xc0c295a8u, 0x07010002u },
};
static const uint32_t head_cfg2[][2] = {
    { 0xc0c29580u, 1 }, { 0xc0c29584u, 0 }, { 0xc0c295f0u, 0 },
};
/* The rest of 0xc3096's fixed words, in the order the RE answer lists them. */
static const uint32_t head_cfg3[][2] = {
    { 0xc0c29d4cu, 0 }, { 0xc0c291d0u, 0xfffffffcu }, { 0xc0c29050u, 0xffu }, { 0xc0c29060u, 7 },
    { 0xc0c29070u, 0x02022033u }, { 0xc0c290d0u, 0x58u }, { 0xc0c290b0u, 0xfcu },
    { 0xc0c290f0u, 0x04040506u }, { 0xc0c290f4u, 0x02020303u },
    { 0xc0c29130u, 0x00070701u }, { 0xc0c29138u, 0x00070701u }, { 0xc0c2913cu, 0x00070700u },
    { 0xc0c29150u, 8 }, { 0xc0c29154u, 0xfcu }, { 0xc0c29158u, 0x1cu }, { 0xc0c29160u, 0x0au },
    { 0xc0c29168u, 0x0au }, { 0xc0c29174u, 0x00121c12u }, { 0xc0c29184u, 0 },
};

static void log_cfg3(const char *label)
{
    dbg_puts(label);
    for (unsigned i = 0; i < ARRAY_SIZE(head_cfg3); ++i) { dbg_putc(' '); dbg_u32(rd(head_cfg3[i][0])); }
    dbg_putc('\n');
}

static void log_reg(const char *name, uint32_t a)
{ dbg_puts(name); dbg_putc('='); dbg_u32(rd(a)); dbg_putc(' '); }

/* Diagnostics: does the head block accept writes, what do the bank/alias words
 * around the sense pin hold, and does the sense line pulse after a trigger? */
static void diagnostics(void)
{
    dbg_puts("[cart] bank "); log_reg("812c0", BANK_ROUTE); log_reg("812c4", BANK_CFG);
    log_reg("812c8", 0xc0c812c8u); log_reg("812dc", 0xc0c812dcu); dbg_putc('\n');
    dbg_puts("[cart] alias "); log_reg("84600", 0xc0c84600u); log_reg("84604", 0xc0c84604u);
    log_reg("84608", 0xc0c84608u); log_reg("8460c", 0xc0c8460cu); log_reg("84610", 0xc0c84610u);
    log_reg("84614", 0xc0c84614u); log_reg("84618", 0xc0c84618u); log_reg("8461c", 0xc0c8461cu);
    log_reg("84688", 0xc0c84688u); dbg_putc('\n');
    wr(FUSE_ENABLE, 1); wr(FUSE_SELECT, 0x641u); wr(FUSE_PAIR, 0);
    dbg_puts("[cart] head block readback "); log_reg("2954c", FUSE_ENABLE); log_reg("29540", FUSE_SELECT);
    log_reg("29550", FUSE_PAIR); log_reg("29544", FUSE_TRIGGER); log_reg("29548", 0xc0c29548u); dbg_putc('\n');
    static const uint32_t probes[][2] = { {0, 0}, {1, 0} };
    for (unsigned p = 0; p < ARRAY_SIZE(probes); ++p) {
        unsigned head = probes[p][0], bit = probes[p][1];
        wr(FUSE_SELECT, selector(head, bit)); wr(FUSE_PAIR, 0); wr(FUSE_TRIGGER, 1);
        uint32_t ones = 0, first = 0xffffffffu, last = 0, t0 = app_clock_ticks();
        for (uint32_t i = 0; i < 20000u; ++i) {
            if (rd(FUSE_SENSE) & 1u) { ++ones; if (first == 0xffffffffu) first = i; last = i; }
        }
        uint32_t span = app_clock_ticks() - t0;
        dbg_puts("[cart] pulse head="); dbg_dec(head); dbg_puts(" bit="); dbg_dec(bit);
        dbg_puts(" sel=0x"); dbg_u32(selector(head, bit));
        dbg_puts(" ones="); dbg_dec(ones); dbg_puts("/20000 first="); dbg_dec(first);
        dbg_puts(" last="); dbg_dec(last); dbg_puts(" span_us="); dbg_dec(span / (CLOCK_TICKS_PER_MS / 1000u));
        dbg_puts(" trig_rb="); dbg_dec(rd(FUSE_TRIGGER)); dbg_putc('\n');
        delay_ticks(wait_ticks);
    }
    wr(FUSE_SELECT, 0); wr(FUSE_PAIR, 0); wr(FUSE_ENABLE, 0);
}

static void log_head(unsigned head, const struct head_result *r)
{
    uint32_t low = r->low;
    dbg_puts(head ? "[cart] colour" : "[cart] black");
    dbg_puts(" low=0x"); dbg_u32(low);
    dbg_puts(" high=0x"); dbg_u32(r->high);
    dbg_puts(low == r->low_repeat ? " stable" : " UNSTABLE");
    dbg_puts(" id="); dbg_dec((low >> 7) & 15u);
    dbg_puts(" dest="); dbg_dec(head ? (low >> 25) & 63u : (low >> 22) & 63u);
    dbg_puts(" rank1="); dbg_dec(head ? (low >> 11) & 31u : (low >> 11) & 63u);
    if (head) { dbg_puts(" rank2="); dbg_dec((low >> 16) & 15u); }
    dbg_puts(" inklvl="); dbg_dec(low & 127u);
    dbg_puts(" sense_idle="); dbg_dec(r->sense_idle & 1u);
    dbg_putc('\n');
}

bool cartridge_request(unsigned value, unsigned wait_units)
{
    unsigned heads = value & 0xffu, options = value & ~0xffu;
    if (!heads || heads > 3 || (options & ~(CART_OPT_RAW | CART_OPT_KEEP | CART_OPT_DIAG | CART_OPT_NO_HEADINIT)) ||
        wait_units > CART_WAIT_MAX || state == 1) return false;
    pending_heads = heads; pending_options = options;
    pending_wait = wait_units ? wait_units : CART_WAIT_DEFAULT;
    state = 1;
    return true;
}

bool cartridge_busy(void) { return state == 1; }

void cartridge_poll(void)
{
    if (state != 1) return;
    wait_ticks = pending_wait * CART_WAIT_UNIT;
    heads_done = 0; options_used = pending_options;
    dbg_puts("[cart] fuse-ROM read, heads="); dbg_dec(pending_heads);
    dbg_puts(" wait_us="); dbg_dec(pending_wait * 10u);
    dbg_puts(" options=0x"); dbg_u32(pending_options); dbg_putc('\n');
    bool power = !(pending_options & CART_OPT_RAW);
    if (power && !head_initialized && !(pending_options & CART_OPT_NO_HEADINIT)) {
        log_cfg3("[cart] head block before:");
        for (unsigned i = 0; i < ARRAY_SIZE(head_cfg3); ++i) wr(head_cfg3[i][0], head_cfg3[i][1]);
        for (unsigned i = 0; i < ARRAY_SIZE(head_cfg); ++i) wr(head_cfg[i][0], head_cfg[i][1]);
        for (unsigned i = 0; i < ARRAY_SIZE(head_cfg2); ++i) wr(head_cfg2[i][0], head_cfg2[i][1]);
        log_cfg3("[cart] head block after:");
        head_initialized = true;
    }
    if (power) {
        if (!prepared) {
            connection = connection_test();
            dbg_puts("[cart] connection black="); dbg_dec(connection & 0xffu);
            dbg_puts(" colour="); dbg_dec(connection >> 8); dbg_putc('\n');
            cold_prepare();
        }
        acquire();
        dbg_puts("[cart] head ctl 84604/54/64="); dbg_dec(rd(HEAD_CTL_ACQ));
        dbg_putc('/'); dbg_dec(rd(HEAD_CTL_COLD)); dbg_putc('/'); dbg_dec(rd(HEAD_CTL_RES));
        dbg_puts(" bank 812c0/c4=0x"); dbg_u32(rd(BANK_ROUTE)); dbg_puts("/0x"); dbg_u32(rd(BANK_CFG));
        dbg_putc('\n');
    }
    if (pending_options & CART_OPT_DIAG) diagnostics();
    for (unsigned head = 0; head < 2; ++head) {
        if (!(pending_heads & (1u << head))) continue;
        struct head_result *r = &result[head];
        r->sense_idle = rd(FUSE_SENSE);
        r->low = read_bits(head, 0, 31);
        r->low_repeat = read_bits(head, 0, 31);
        r->high = head ? read_bits(head, 32, 47) & 0xffffu : 0;
        heads_done |= 1u << head;
        log_head(head, r);
    }
    if (power) {
        release();
        if (!(pending_options & CART_OPT_KEEP)) shutdown();
        dbg_puts(prepared ? "[cart] head interface left prepared\n" : "[cart] head interface shut down\n");
    }
    ++sequence;
    state = 2;
}

/* Status reply: 16 little-endian words. */
unsigned cartridge_status(uint8_t *out)
{
    put32(out, 0x5443474du);            /* MGCT */
    put32(out + 4, 2);                  /* layout version */
    put32(out + 8, state);
    put32(out + 12, heads_done);
    put32(out + 16, wait_ticks);
    put32(out + 20, sequence);
    put32(out + 24, connection);        /* black | colour << 8: 0 untested, 1 ok, 2 failed */
    put32(out + 28, options_used | (prepared ? 0x10000u : 0) | (head_initialized ? 0x20000u : 0));
    for (unsigned head = 0; head < 2; ++head) {
        uint8_t *p = out + 32 + head * 16;
        put32(p, result[head].low); put32(p + 4, result[head].low_repeat);
        put32(p + 8, result[head].high); put32(p + 12, result[head].sense_idle);
    }
    return 64;
}
