/* Stationary print-head prefire ("spit") on the black lanes.
 *
 * First head actuation of the open firmware. It reproduces, register for
 * register and in order, the stock stationary prefire work list ROM ID 24 as
 * recovered in fw_dump/gpt/BRIEF_head_fire.md (response, section 4, steps
 * 0a..13 and section 5 record layout) with the heater waveform words from
 * fw_dump/gpt/BRIEF_head_drive.md (section 3): two records of `count` pulses
 * on logical lanes 0 and 1 (the two black lanes, selection mask 3), waveform
 * mode 8, work mode 0. Stock runs that list routinely with the carriage
 * stationary, so it is the lowest-exposure head operation known to us; this
 * module invents no shorter pulse, other mask or other mode.
 *
 * Not established by the briefs, and therefore not assumed here: the physical
 * nozzle permutation, the time unit of the waveform halfwords, and that one
 * count is exactly one drop. Guards that follow from that:
 *   - only the two stock prefire lists: black ID 0x24 (lanes 0/1, count 100)
 *     and colour ID 0x25 (lanes 2..7, count 600); counts 15..600;
 *   - the carriage must be referenced and idle (the host positions it);
 *   - the cartridge fuse ROM must have been read in this session and show the
 *     ranks the embedded waveform rows were computed for (black rank 37,
 *     colour ranks 16/7, giving stock LCL 16 / SCL 13);
 *   - the head thermistor ADC is sampled before and after, raw counts;
 *   - the stock ambient-temperature selector input is a request parameter in
 *     half-degrees C (default 50 = 25 C) because the fuse-calibrated conversion
 *     from ADC counts is not reproduced;
 *   - a record that does not complete within HF_TIMEOUT_MS ends the list with
 *     the stock work-engine cleanup, then (non-stock, safety) disarms, then
 *     releases the owner. Nothing stays armed.
 * Two writes are not in the stock list and are marked as such in the code:
 * the disarm after a timeout, and restoring the block-init value of
 * 0xc0c290b0 after the cleanup (the stock prefire leaves the mode-8 value).
 * A dry run performs every configuration write but never arms the engine, so
 * register acceptance can be checked on hardware without firing.
 *
 * The sequence blocks for about a second (stock settle waits dominate) and
 * keeps USB management polled, like cartridge.c. Stock sequences it through
 * IRQ 0x2f; here the completion bit is polled with a deadline. */
#include "app.h"

#define HF_COUNT_DEFAULT 100u    /* stock ID 0x24 (black) record count */
#define HF_COUNT_COLOUR 600u     /* stock ID 0x25 (colour) record count */
#define HF_COUNT_MIN 15u         /* smallest count in any stock work record */
#define HF_AMBIENT_DEFAULT 50u   /* half-degrees C: 25 C */
#define HF_AMBIENT_MAX 200u
#define HF_TIMEOUT_MS 1000u
#define HF_MASK_BK 3u            /* logical lanes 0 and 1: the black lanes */
#define HF_OPT_DRY 0x100u        /* configuration writes only, no arm */
#define HF_OPT_KEEP 0x200u       /* leave the head interface prepared */
#define HF_OPT_REC1_ONLY 0x400u  /* run only the first stock record (selection A) */
#define HF_OPT_REC2_ONLY 0x800u  /* run only the second stock record (selection B) */
#define HF_OPT_COLOUR 0x1000u    /* stock colour list ID 0x25 instead of the black list ID 0x24 */
#define HF_OPT_CUSTOM 0x2000u    /* one black record with the mask words set by USB 0x83/0x84 (experiment) */
#define HF_OPTIONS (HF_OPT_DRY | HF_OPT_KEEP | HF_OPT_REC1_ONLY | HF_OPT_REC2_ONLY | HF_OPT_COLOUR | HF_OPT_CUSTOM)
/* Ranks the embedded waveform rows were computed for (brief 4, section 3). */
#define RANK_BK 37u
#define RANK_CL1 16u
#define RANK_CL2 7u

/* Head work engine registers named in the briefs (stock software roles;
 * electrical meanings are not established). */
#define HE_STATUS   0xc0c29030u  /* bit 0 set by hardware on work completion (stock IRQ 0x2f test) */
#define HE_CTRL     0xc0c29034u  /* bit 0 held while a work item is armed */
#define HE_ARM      0xc0c29af0u  /* 1 arms the work, 0 after completion */
#define HE_SELECT   0xc0c29ae0u  /* lane selection mask of the armed work */
#define HE_INTERVAL 0xc0c29ae4u  /* ((scalar - 1) >> 4) - 1; scalar 16000 when a black lane is selected */
#define HE_UNION    0xc0c29aa0u  /* selection A | B */
#define HE_SEL_B    0xc0c29ac0u  /* selection B low word */
#define HE_WORKMODE 0xc0c29ab0u  /* 0 = immediate count programming */
#define HE_AUX      0xc0c29ac8u
#define HE_FLAG     0xc0c29ad0u  /* bit 0 set before each record */
#define HE_COUNT0   0xc0c29b10u  /* ten lane count registers, (count - 1) */
#define HE_REC18    0xc0c291e0u  /* record words +18 / +1c */
#define HE_REC1C    0xc0c291e8u
#define HE_TIMING   0xc0c29174u  /* written before and after the counts, same word as the block init */
#define CR_WORK14   0xc0c51d14u  /* positioned-work words cleared by the stock cleanup */
#define CR_WORK1C   0xc0c51d1cu

/* Paired waveform words P[j] = H[j] << 16 | L[j], exact stock table values for
 * the ranks above (brief 4, section 3). Entries 5..15 repeat entry 4. The
 * selector is chosen by the ambient bin (below); every stock row is kept so
 * the choice stays data, not code. Units unknown; never edited by hand. */
struct waveform { uint32_t p[5], tail; };
static const struct waveform bk37[7] = {
    { { 0x001400d8u, 0x001400d2u, 0x001400cau, 0x001400cau, 0x001400cau }, 0x0014014cu },
    { { 0x001700dau, 0x001400d2u, 0x001400cau, 0x001400cau, 0x001400cau }, 0x0014014cu },
    { { 0x002000dfu, 0x001e00d8u, 0x001b00ceu, 0x001b00ceu, 0x001b00ceu }, 0x0014014cu },
    { { 0x002900e5u, 0x002800dfu, 0x002600d6u, 0x002600d6u, 0x002600d6u }, 0x0014014cu },
    { { 0x003300ecu, 0x003200e6u, 0x003100ddu, 0x003100ddu, 0x003100ddu }, 0x0014014cu },
    { { 0x00140078u, 0x00140077u, 0x00140075u, 0x00140075u, 0x00140075u }, 0x0014009eu },
    { { 0x003200dfu, 0x003100d9u, 0x002e00ceu, 0x002e00ceu, 0x002e00ceu }, 0x0014014cu },
};
static const struct waveform lcl16[8] = {
    { { 0x0014004eu, 0x0014004cu, 0x0014004au, 0x00140048u, 0x00140046u }, 0x001400aeu },
    { { 0x001a0051u, 0x0018004du, 0x00160049u, 0x00140048u, 0x00140046u }, 0x001400aeu },
    { { 0x0025005au, 0x00240056u, 0x00220053u, 0x00210050u, 0x001f004cu }, 0x001400aeu },
    { { 0x002f0062u, 0x002e005fu, 0x002d005cu, 0x002c005au, 0x002b0057u }, 0x001400aeu },
    { { 0x00380069u, 0x00380067u, 0x00370065u, 0x00370063u, 0x00360060u }, 0x001400aeu },
    { { 0x00140088u, 0x00140087u, 0x00140086u, 0x00140085u, 0x00140084u }, 0x001400aeu },
    { { 0x003c0093u, 0x003d0093u, 0x003e0093u, 0x003e0092u, 0x003f0092u }, 0x0018009eu },
    { { 0x00280050u, 0x0029004eu, 0x0029004cu, 0x0029004bu, 0x00290049u }, 0x0014009eu },
};
static const struct waveform scl13[8] = {
    { { 0x00140054u, 0x00140052u, 0x00140050u, 0x00140050u, 0x0014004eu }, 0x001400aeu },
    { { 0x0020005du, 0x0020005au, 0x001f0057u, 0x001f0057u, 0x001e0055u }, 0x001400aeu },
    { { 0x00280063u, 0x00280061u, 0x0027005eu, 0x0027005eu, 0x0026005cu }, 0x001400aeu },
    { { 0x00310069u, 0x00310067u, 0x00300065u, 0x00300065u, 0x00300063u }, 0x001400aeu },
    { { 0x0037006fu, 0x0037006du, 0x0037006bu, 0x0037006bu, 0x00370069u }, 0x001400aeu },
    { { 0x0014008au, 0x00140089u, 0x00140089u, 0x00140089u, 0x00140088u }, 0x001400aeu },
    { { 0x00440090u, 0x00450090u, 0x00460090u, 0x00460090u, 0x00470090u }, 0x0018009eu },
    { { 0x002e005bu, 0x002e005au, 0x002f0058u, 0x002f0058u, 0x002f0057u }, 0x0014009eu },
};
/* Stock ordinary selector choice by ambient bin (half-degrees): <=34, 35..45,
 * 46..52, 53..59, >=60. With the unmodified ROM tables the head-minus-ambient
 * index does not change the selector, so ambient alone decides. */
static const uint8_t bk_selector[5] = { 4, 3, 3, 2, 2 }, cl_selector[5] = { 4, 3, 2, 2, 1 };
static unsigned ambient_bin(unsigned a) { return a <= 34u ? 0 : a <= 45u ? 1 : a <= 52u ? 2 : a <= 59u ? 3 : 4; }

/* The two ordinary records of the stock prefire lists: black ID 0x24
 * (brief 5, section 4: waveform mode 8, count 100) and colour ID 0x25
 * (brief 7, answer 3: waveform mode 9, count 600). Fields: selection A low,
 * selection B low, words +18 and +1c. Mode 9's pattern, control and waveform
 * writes are identical to mode 8's for the installed ranks (brief 7), so the
 * two lists differ only in these records, the count and the interval scalar
 * (16000 when a black lane is selected, else 9600). */
struct record { uint32_t sel_a, sel_b, w18, w1c; };
static const struct record id24[2] = {
    { 3u, 0u, 0xfffffc00u, 0xffffffffu },
    { 0u, 3u, 0xffffffffu, 0xffffffffu },
};
static const struct record id25[2] = {
    { 0xa4u, 0xa4u, 0xffffffffu, 0xfffffc00u },   /* lanes 2, 5, 7 */
    { 0x58u, 0x58u, 0xffffffffu, 0xfffffc00u },   /* lanes 3, 4, 6 */
};
/* Experiment: stock black record 1 (selection A = 3, mode 8) with the two
 * record words replaced by host-supplied values, to map the bits of
 * 0xc0c291e0/0xc0c291e8 to nozzle groups on paper. Everything else in the
 * sequence (waveform, control words, count range, acquire/release) stays
 * the stock black-list sequence; only these two words change. */
static struct record custom = { 3u, 0u, 0xffffffffu, 0xffffffffu };
/* Experiment: the two mode-8 pattern words (stock 0x03000300 / 0x00030003, four
 * nibble fields each) can be overridden for the next fires and jobs (USB 0x89/0x8a);
 * 0 restores stock. Their effect inside a nozzle group is what the test measures. */
static uint32_t pattern_a = 0x03000300u, pattern_b = 0x00030003u;



enum hf_state { HF_IDLE = 0, HF_PENDING, HF_DONE };
enum hf_result { HFR_NONE = 0, HFR_RUNNING, HFR_OK, HFR_RANK, HFR_CARRIAGE, HFR_TIMEOUT, HFR_DRY_OK, HFR_BUSY, HFR_PARAM };

static enum hf_state state;
static enum hf_result result;
static unsigned count, ambient, options, bk_sel, cl_sel, records_done, sequence;
static uint32_t adc_before[2], adc_after[2], elapsed_ms, wait_ms_last, status_end;
static int32_t x_fire;

static uint32_t ms_since(uint32_t t) { return (app_clock_ticks() - t) / CLOCK_TICKS_PER_MS; }
bool head_fire_set_pattern(unsigned which, uint32_t value)
{
    if (state == HF_PENDING || which > 1) return false;
    if (which) pattern_b = value ? value : 0x00030003u; else pattern_a = value ? value : 0x03000300u;
    return true;
}

bool head_fire_set_word(unsigned which, uint32_t value)
{
    if (state == HF_PENDING || which > 1) return false;
    if (which) custom.w1c = value; else custom.w18 = value;
    return true;
}


static void write_bank(uint32_t base, uint32_t tail, const struct waveform *w)
{
    for (unsigned j = 0; j < 16; ++j) wr(base + 4u * j, w->p[j < 5 ? j : 4]);
    wr(tail, w->tail);
}

/* Steps 5..10 for one record. Returns 0 completed, 1 timed out, 2 dry run. */
static unsigned run_record(const struct record *r, bool dry)
{
    uint32_t scalar = ((r->sel_a | r->sel_b) & 3u) ? 16000u : 9600u;     /* 5: 998 black, 598 colour */
    wr(HE_INTERVAL, ((scalar - 1u) >> 4) - 1u);
    wr(HE_WORKMODE, 0); wr(HE_AUX, 0); modify(HE_FLAG, 0, 1u); wr(HE_UNION, r->sel_a | r->sel_b);  /* 6 */
    wr(HE_TIMING, 0x00121c12u); wr(HE_REC18, r->w18); wr(HE_REC1C, r->w1c); wr(HE_SEL_B, r->sel_b);  /* 7 */
    wr(0xc0c290a0u, 0); wr(0xc0c290b0u, 0); wr(0xc0c290c0u, 0);
    for (unsigned i = 0; i < 10; ++i) wr(HE_COUNT0 + 4u * i, 0);        /* 8 */
    for (unsigned i = 0; i < 10; ++i) if ((r->sel_a | r->sel_b) & (1u << i)) wr(HE_COUNT0 + 4u * i, (count - 1u) & 0xffffu);
    wr(HE_TIMING, 0x00121c12u);
    if (dry) return 2;
    modify(HE_STATUS, 1u, 0); modify(HE_CTRL, 0, 1u); wr(HE_ARM, 1u); wr(HE_SELECT, r->sel_a | r->sel_b);  /* 9 */
    uint32_t t0 = app_clock_ticks();                                  /* 10: stock IRQ 0x2f condition */
    while (!(rd(HE_STATUS) & 1u)) {
        if (ms_since(t0) >= HF_TIMEOUT_MS) { wait_ms_last = ms_since(t0); return 1; }
        carriage_poll();                                              /* a stamp job fires while crawling */
        usb_poll();
    }
    wait_ms_last = ms_since(t0);
    modify(HE_CTRL, 1u, 0); modify(HE_STATUS, 1u, 0); wr(HE_ARM, 0);
    return 0;
}

/* Steps 0..4: interface, owner acquire, pattern words, waveform banks, control words. */
static void engine_setup(void)
{
    head_block_init();                       /* 0: stock head-block configuration (once per boot) */
    head_if_prepare();                       /*    connection test and cold preparation (once) */
    head_if_acquire();                       /* 0a: owner acquire, same writes as the mode-4 acquire */
    /* 1: pattern scratch words for waveform mode 8 (stock values unless overridden) */
    wr(0xc0c29230u, pattern_a); wr(0xc0c29234u, pattern_b); wr(0xc0c29238u, 0);
    wr(0xc0c29280u, 0); wr(0xc0c29284u, 0); wr(0xc0c29288u, 0); wr(0xc0c29200u, 0); wr(0xc0c29204u, 0);
    /* 2: rank/temperature waveform banks: black (bank 3), LCL (bank 0), SCL (bank 2) */
    write_bank(0xc0c29850u, 0xc0c295dcu, &bk37[bk_sel]);
    write_bank(0xc0c29700u, 0xc0c295d0u, &lcl16[cl_sel]);
    write_bank(0xc0c297e0u, 0xc0c295d8u, &scl13[cl_sel]);
    /* 3: mode 8 control words */
    wr(0xc0c29190u, 0); wr(0xc0c29580u, 1u); wr(0xc0c291a0u, 0); wr(0xc0c295f0u, 0);
    wr(0xc0c29584u, 0); wr(0xc0c295b0u, 0x080c0009u); wr(0xc0c29600u, 0x00020002u);
    /* 4: commit-like word, then the mode-8 metadata word */
    wr(0xc0c29608u, 1u); wr(0xc0c29d50u, 0);
}

/* Stock work-engine cleanup at the list sentinel (c31ba), in order. */
static void cleanup(void)
{
    modify(HE_CTRL, 1u, 0); modify(HE_STATUS, 1u, 0);
    wr(HE_UNION, 0); wr(HE_SEL_B, 0); wr(HE_WORKMODE, 0); wr(HE_AUX, 0); wr(CR_WORK14, 0); wr(CR_WORK1C, 0);
}

bool head_fire_busy(void) { return state == HF_PENDING; }

bool head_fire_request(unsigned value, unsigned ambient_half_deg)
{
    if (state == HF_PENDING) return false;
    unsigned n = value & 0xffu, opt = value & ~0xffu, amb = ambient_half_deg;
    if (!n) n = (opt & HF_OPT_COLOUR) ? HF_COUNT_COLOUR : HF_COUNT_DEFAULT;
    else if (opt & HF_OPT_COLOUR) n *= 6u;                       /* colour count is given in sixths (max 0xff) */
    if (!amb) amb = HF_AMBIENT_DEFAULT;
    unsigned n_max = (opt & HF_OPT_COLOUR) ? HF_COUNT_COLOUR : HF_COUNT_DEFAULT;   /* never above the stock list's count */
    if (n < HF_COUNT_MIN || n > n_max || (opt & ~HF_OPTIONS) || amb > HF_AMBIENT_MAX) { result = HFR_PARAM; return false; }
    if ((opt & HF_OPT_REC1_ONLY) && (opt & HF_OPT_REC2_ONLY)) { result = HFR_PARAM; return false; }
    if ((opt & HF_OPT_CUSTOM) && (opt & (HF_OPT_COLOUR | HF_OPT_REC2_ONLY))) { result = HFR_PARAM; return false; }
    if (carriage_busy() || paper_feed_busy() || page_job_busy() || cartridge_busy() || scanner_motor_busy() || scan_page_busy() || print_mech_lf_busy()) {
        result = HFR_BUSY; return false;
    }
    if (!carriage_referenced()) { result = HFR_CARRIAGE; return false; }
    unsigned bk, cl1, cl2;
    if (!cartridge_ranks(&bk, &cl1, &cl2) || bk != RANK_BK || cl1 != RANK_CL1 || cl2 != RANK_CL2) {
        dbg_puts("[head] refused: cartridge ranks not read or not 37/16/7\n");
        result = HFR_RANK; return false;
    }
    count = n; ambient = amb; options = opt;
    bk_sel = bk_selector[ambient_bin(amb)]; cl_sel = cl_selector[ambient_bin(amb)];
    result = HFR_RUNNING; state = HF_PENDING;
    return true;
}

static void log_readback(void)
{
    static const uint32_t regs[] = { 0xc0c29850u, 0xc0c295dcu, 0xc0c29700u, 0xc0c295d0u, 0xc0c29600u, 0xc0c29608u,
                                     HE_COUNT0, HE_INTERVAL, HE_UNION, HE_STATUS, HE_CTRL, HE_ARM, HE_SELECT };
    dbg_puts("[head] readback");
    for (unsigned i = 0; i < ARRAY_SIZE(regs); ++i) { dbg_putc(' '); dbg_u32(rd(regs[i])); }
    dbg_putc('\n');
}

static void stamp_poll(void);

void head_fire_poll(void)
{
    stamp_poll();
    if (state != HF_PENDING) return;
    bool dry = (options & HF_OPT_DRY) != 0;
    uint32_t t_start = app_clock_ticks();
    records_done = 0; wait_ms_last = 0; x_fire = carriage_position();
    dbg_puts(dry ? "[head] prefire DRY RUN " : "[head] prefire "); dbg_puts((options & HF_OPT_COLOUR) ? "colour count=" : "count="); dbg_dec(count);
    dbg_puts(" ambient/2C="); dbg_dec(ambient); dbg_puts(" bk_sel="); dbg_dec(bk_sel); dbg_puts(" cl_sel="); dbg_dec(cl_sel);
    dbg_puts(" x="); dbg_dec((uint32_t)x_fire);
    if (options & (HF_OPT_REC1_ONLY | HF_OPT_REC2_ONLY)) { dbg_puts(" record="); dbg_dec((options & HF_OPT_REC2_ONLY) ? 2u : 1u); }
    dbg_putc('\n');
    adc_before[0] = head_adc_read(0); adc_before[1] = head_adc_read(1);
    dbg_puts("[head] adc before black="); dbg_dec(adc_before[0]); dbg_puts(" colour="); dbg_dec(adc_before[1]); dbg_putc('\n');

    engine_setup();                          /* 0..4 */

    /* Record selection: the stock list runs both; one at a time is an experiment
     * to attribute marks on paper to a record (brief 6, answer 1). */
    unsigned first = (options & HF_OPT_REC2_ONLY) ? 1u : 0u, last = (options & HF_OPT_REC1_ONLY) ? 1u : 2u;
    const struct record *list = (options & HF_OPT_COLOUR) ? id25 : id24;
    if (options & HF_OPT_CUSTOM) { list = &custom; last = 1u; dbg_puts("[head] custom words "); dbg_u32(custom.w18); dbg_putc('/'); dbg_u32(custom.w1c); dbg_putc('\n'); }
    unsigned rc = 0;
    for (unsigned r = first; r < last; ++r) {
        rc = run_record(&list[r], dry);
        if (rc == 1) { dbg_puts("[head] record "); dbg_dec(r); dbg_puts(" TIMEOUT: no completion\n"); break; }
        ++records_done;
    }
    status_end = rd(HE_STATUS);
    cleanup();                               /* 12 */
    if (rc == 1) { wr(HE_ARM, 0); wr(HE_SELECT, 0); }   /* non-stock: never leave a timed-out item armed */
    /* Non-stock restore: step 7 wrote the mode-8 metadata word 0xc0c290b0 = 0;
     * put back the block-init value so the physically verified fuse-read
     * configuration group (cartridge.c head_cfg3) is intact for later reads. */
    wr(0xc0c290b0u, 0xfcu);
    log_readback();
    head_if_release();                       /* 13 */
    if (!(options & HF_OPT_KEEP)) head_if_shutdown();
    adc_after[0] = head_adc_read(0); adc_after[1] = head_adc_read(1);
    elapsed_ms = ms_since(t_start);
    result = rc == 1 ? HFR_TIMEOUT : dry ? HFR_DRY_OK : HFR_OK;
    state = HF_DONE; ++sequence;
    dbg_puts("[head] done result="); dbg_dec(result); dbg_puts(" records="); dbg_dec(records_done);
    dbg_puts(" wait_ms="); dbg_dec(wait_ms_last); dbg_puts(" ms="); dbg_dec(elapsed_ms);
    dbg_puts(" adc after black="); dbg_dec(adc_after[0]); dbg_puts(" colour="); dbg_dec(adc_after[1]); dbg_putc('\n');
}

/* Stamp job: fire record words at carriage positions while the carriage crawls.
 *
 * The host uploads entries (0x83/0x84 set the two record words, 0x85 commits
 * them with a position), then 0x87 runs the job: the carriage moves to a
 * lead-in before the first entry at normal speed, the engine is set up once
 * (steps 0..4), the carriage crawls toward the last entry at the requested
 * cruise, and every entry is fired (one black record, steps 5..10) when the
 * encoder reaches its position. Entries the carriage has already passed by
 * more than STAMP_LATE counts are counted as missed, never fired late. At the
 * end, or on cancel, the stock cleanup, owner release and shutdown follow.
 * Fires block for a few milliseconds each; the carriage controller is polled
 * from inside that wait. Smear per fire = crawl speed x pulse train length. */
#define STAMP_MAX 192u
#define STAMP_LEAD 1500u             /* counts before the first entry where the crawl starts */
#define STAMP_TAIL 600u              /* counts after the last entry where the crawl ends */
#define STAMP_LATE 192u              /* entry passed by more than this is missed */
#define STAMP_MIN_CRAWL 7000u        /* longer than the carriage short-move threshold */
#define STAMP_CRAWL_DEFAULT 10000u   /* counts/s; the controller's floor speed */
static struct { uint32_t x, a, b; } stamp[STAMP_MAX];
static unsigned stamp_n, stamp_next, stamp_fired, stamp_missed, stamp_state, stamp_result, stamp_count, stamp_options;
static uint32_t stamp_crawl, stamp_t0, stamp_ms;
static bool stamp_abort;
enum { SJ_IDLE = 0, SJ_APPROACH, SJ_CRAWL, SJ_FINISH, SJ_RETREAT };
enum { SJR_NONE = 0, SJR_RUNNING, SJR_OK, SJR_CARRIAGE, SJR_CANCELLED, SJR_TIMEOUT, SJR_PARAM, SJR_REFUSED };

bool stamp_clear(void) { if (stamp_state != SJ_IDLE) return false; stamp_n = 0; return true; }

/* 0x85: entry `index` = (x, custom words A/B as last set by 0x83/0x84). */
bool stamp_commit(unsigned index, uint32_t x)
{
    if (stamp_state != SJ_IDLE || index >= STAMP_MAX || x > 116736u - STAMP_TAIL) return false;
    stamp[index].x = x; stamp[index].a = custom.w18; stamp[index].b = custom.w1c;
    if (index >= stamp_n) stamp_n = index + 1u;
    return true;
}

/* 0x87: wValue = count (0 = 15) | options (0x100 dry: no arm), wIndex = crawl counts/s / 100 (0 = 10000). */
bool stamp_run(unsigned value, unsigned crawl_units)
{
    unsigned n = value & 0xffu, opt = value & ~0xffu;
    if (stamp_state != SJ_IDLE || state == HF_PENDING || !stamp_n) { stamp_result = SJR_REFUSED; return false; }
    if (!n) n = HF_COUNT_MIN;
    if (n < HF_COUNT_MIN || n > HF_COUNT_DEFAULT || (opt & ~(HF_OPT_DRY | HF_OPT_KEEP))) { stamp_result = SJR_PARAM; return false; }
    for (unsigned i = 1; i < stamp_n; ++i) if (stamp[i].x <= stamp[i - 1].x) { stamp_result = SJR_PARAM; return false; }
    if (stamp[0].x < STAMP_LEAD) { stamp_result = SJR_PARAM; return false; }
    if (carriage_busy() || paper_feed_busy() || page_job_busy() || cartridge_busy() || scanner_motor_busy() || scan_page_busy() || print_mech_lf_busy()) { stamp_result = SJR_REFUSED; return false; }
    if (!carriage_referenced()) { stamp_result = SJR_CARRIAGE; return false; }
    unsigned bk, cl1, cl2;
    if (!cartridge_ranks(&bk, &cl1, &cl2) || bk != RANK_BK || cl1 != RANK_CL1 || cl2 != RANK_CL2) { stamp_result = SJR_REFUSED; return false; }
    /* The approach must be a sweep-controller move (the short-move mode stalls): if the
     * carriage is already within the short-move range of the approach point, retreat first. */
    int32_t approach = (int32_t)(stamp[0].x - STAMP_LEAD), here = carriage_position();
    uint32_t away = (uint32_t)(approach > here ? approach - here : here - approach);
    unsigned first_state = SJ_APPROACH; int32_t first_target = approach;
    if (away <= 6000u + 500u) { first_state = SJ_RETREAT; first_target = approach >= 8000 ? approach - 8000 : approach + 8000; }
    if (!carriage_move_request(first_target, 0)) { stamp_result = SJR_CARRIAGE; return false; }
    stamp_count = n; stamp_options = opt; stamp_crawl = crawl_units ? crawl_units * 100u : STAMP_CRAWL_DEFAULT;
    bk_sel = bk_selector[ambient_bin(ambient ? ambient : HF_AMBIENT_DEFAULT)]; cl_sel = cl_selector[ambient_bin(ambient ? ambient : HF_AMBIENT_DEFAULT)];
    stamp_next = stamp_fired = stamp_missed = 0; stamp_abort = false; stamp_result = SJR_RUNNING; stamp_state = first_state;
    stamp_t0 = app_clock_ticks();
    dbg_puts("[stamp] job n="); dbg_dec(stamp_n); dbg_puts(" count="); dbg_dec(n); dbg_puts(" crawl="); dbg_dec(stamp_crawl);
    dbg_puts(" x="); dbg_dec(stamp[0].x); dbg_puts(".."); dbg_dec(stamp[stamp_n - 1].x); dbg_putc('\n');
    return true;
}

bool stamp_busy(void) { return stamp_state != SJ_IDLE; }
void stamp_cancel(void) { if (stamp_state != SJ_IDLE) stamp_abort = true; }

static void stamp_finish(unsigned r)
{
    cleanup();
    wr(HE_ARM, 0); wr(HE_SELECT, 0);          /* non-stock: nothing stays armed after a list ends early */
    wr(0xc0c290b0u, 0xfcu);
    head_if_release();
    if (!(stamp_options & HF_OPT_KEEP)) head_if_shutdown();
    stamp_ms = ms_since(stamp_t0); stamp_result = r; stamp_state = SJ_IDLE;
    dbg_puts("[stamp] done result="); dbg_dec(r); dbg_puts(" fired="); dbg_dec(stamp_fired); dbg_puts(" missed="); dbg_dec(stamp_missed);
    dbg_puts(" ms="); dbg_dec(stamp_ms); dbg_putc('\n');
}

static void stamp_poll(void)
{
    if (stamp_state == SJ_IDLE) return;
    if (stamp_abort) { carriage_cancel(); if (stamp_state == SJ_APPROACH || stamp_state == SJ_RETREAT) { stamp_state = SJ_IDLE; stamp_result = SJR_CANCELLED; return; } stamp_finish(SJR_CANCELLED); return; }
    if (stamp_state == SJ_RETREAT) {
        if (carriage_busy()) return;
        if (!carriage_move_request((int32_t)(stamp[0].x - STAMP_LEAD), 0)) { stamp_state = SJ_IDLE; stamp_result = SJR_CARRIAGE; return; }
        stamp_state = SJ_APPROACH;
        return;
    }
    if (stamp_state == SJ_APPROACH) {
        if (carriage_busy()) return;
        if (carriage_position() > (int32_t)(stamp[0].x - STAMP_LEAD / 2u)) { stamp_state = SJ_IDLE; stamp_result = SJR_CARRIAGE; return; }
        count = stamp_count;
        engine_setup();
        /* The crawl must be a sweep-controller move: shorter targets would take the short-move
         * mode, which does not sustain motion on hardware. Extend the end past the threshold. */
        uint32_t end = stamp[stamp_n - 1].x + STAMP_TAIL, min_end = (uint32_t)carriage_position() + STAMP_MIN_CRAWL;
        if (end < min_end) end = min_end;
        if (end > 116736u - 64u) end = 116736u - 64u;
        if (!carriage_move_request((int32_t)end, stamp_crawl)) { stamp_finish(SJR_CARRIAGE); return; }
        stamp_state = SJ_CRAWL;
        return;
    }
    if (stamp_state == SJ_CRAWL) {
        int32_t x = carriage_position();
        while (stamp_next < stamp_n && x >= (int32_t)stamp[stamp_next].x) {
            if (x > (int32_t)(stamp[stamp_next].x + STAMP_LATE)) { ++stamp_missed; ++stamp_next; continue; }
            struct record r = { 3u, 0u, stamp[stamp_next].a, stamp[stamp_next].b };
            unsigned rc = run_record(&r, (stamp_options & HF_OPT_DRY) != 0);
            if (rc == 1) { stamp_finish(SJR_TIMEOUT); return; }
            ++stamp_fired; ++stamp_next;
            x = carriage_position();
        }
        if (!carriage_busy()) { stamp_missed += stamp_n - stamp_next; stamp_next = stamp_n; stamp_finish(SJR_OK); }
    }
}

/* 0x88: 12 words. */
unsigned stamp_status(uint8_t *out)
{
    put32(out, 0x504d5453u);             /* STMP */
    put32(out + 4, 1); put32(out + 8, stamp_state); put32(out + 12, stamp_result);
    put32(out + 16, stamp_n); put32(out + 20, stamp_fired); put32(out + 24, stamp_missed);
    put32(out + 28, (uint32_t)carriage_position()); put32(out + 32, stamp_state == SJ_IDLE ? stamp_ms : ms_since(stamp_t0));
    put32(out + 36, stamp_count); put32(out + 40, stamp_crawl); put32(out + 44, stamp_options);
    return 48;
}

/* Status: 20 little-endian words. */
unsigned head_fire_status(uint8_t *out)
{
    put32(out, 0x4648474du);             /* MGHF */
    put32(out + 4, 1);
    put32(out + 8, state);
    put32(out + 12, result);
    put32(out + 16, count);
    put32(out + 20, records_done);
    put32(out + 24, (options & HF_OPT_COLOUR) ? 0xfcu : HF_MASK_BK);
    put32(out + 28, ambient);
    put32(out + 32, bk_sel);
    put32(out + 36, cl_sel);
    put32(out + 40, adc_before[0]); put32(out + 44, adc_before[1]);
    put32(out + 48, adc_after[0]); put32(out + 52, adc_after[1]);
    put32(out + 56, elapsed_ms);
    put32(out + 60, (uint32_t)x_fire);
    put32(out + 64, options | (head_if_prepared() ? 0x10000u : 0));
    put32(out + 68, sequence);
    put32(out + 72, status_end);
    put32(out + 76, wait_ms_last);
    return 80;
}
