/* On-device page printer over the hybrid band path.
 *
 * Input rows (300 dpi grey or sRGB, from urf.c) are separated into K/C/M/Y
 * ink amounts, scaled to the 600 dpi engine (each input row gives two output
 * rows; horizontally every output column samples input column
 * X_IN0 + X / 2.048: the 1.024 width compensation measured on sheet PG2052,
 * applied before halftoning as print_doc.py does) and halftoned with an 8x8
 * ordered dither into packed bit rows held in rings. Bands are then printed
 * exactly as the host tools do (page_live.py / print_doc.py):
 *   home, head prepare (cartridge read, keep), stock load (routine 8),
 *   first-line advance (routine 10), per band and sweep: geometry, prepare
 *   (routine 5, fresh once per job), the tile raster written into the stock
 *   managers, executor (routine 7, live, stock CR), then the band advance;
 *   stock eject (routine 9), home.
 * Colour pages use stock print mode 7 with 152-row bands; the C/M/Y arrays
 * sit 128 rows below black row 0 on paper, so a band's black covers data rows
 * [152b - 128, 152b + 24) while its colour covers [152b, 152b + 152). Mono
 * pages use mode 1, 304-row bands, one 4224-column segment. Each band uses
 * two disjoint row passes, 16 rows apart: physical rows 0..15 in every
 * 32-row group cover both halves of the image. Q272125 showed recurring
 * weak rows in the other half, independent of sweep width and density.
 * This avoids those rows without changing head drive timing. Within a pass
 * the sweeps come from plan_phase(): the occupied 16-column tiles of the
 * rows that pass prints, covered greedily by windows of at most one segment
 * and cropped to their last occupied tile, so blank phases cost only their
 * feed and sparse content costs narrow traverses. Everything here is
 * polled; the shim runs each stock routine inside hybrid_poll(). A per-job
 * timing profile (USB 0xa2 wIndex 1/2, one log line each at completion)
 * attributes device time to conversion, filling, input waits and each
 * stock routine class; stock_shim.c adds motion, wait and delay counters. */
#include "app.h"
#include "print_job.h"

#define COLS 4224u                 /* printable width, 600 dpi columns (178.8 mm nominal) */
#define SEG_COLOUR 2112u           /* columns per sweep in mode 7 (arena budget) */
#define SEG_MONO 4224u
#define ROW_BYTES (COLS / 8u)
#define K_RING 320u                /* rows: a 304-row mono band, or 152 + the 128-row colour lead */
#define C_RING 160u
#define MAX_ROWS 6400u             /* 271 mm of output rows */
#define COLOUR_LEAD 128u           /* black row 0 prints 128 rows above the colour arrays' row 0 */
#define X_START 35008u             /* our carriage count of output column 0 (print_doc.py) */
#define STOCK_CAP 2688u
#define PLACE_K 34272u
#define FIRST_ULF_COLOUR 394u      /* 650 Ulf puts black row 0 ~3.2 mm from the edge; black leads the colour origin by 128 rows = 256 Ulf */
#define FIRST_ULF_MONO 650u
#define X_IN0 183u                 /* input pixel under output column 0: (35008 - 29000) / 16 columns = 15.5 mm at 300 dpi */
#define Y_IN0 38u                  /* input row under output row 0: 3.2 mm at 300 dpi */
#define AMBIENT 50u

enum step { S_NONE, S_HOMING, S_HEAD, S_LOADED, S_FIRST, S_BAND, S_PREPARED, S_FIRED, S_ADVANCED, S_EJECTED, S_HOMING2 };

static enum print_engine_state state;
static enum step step;
static bool waiting, page_open, input_done, mono, job_fresh, sheet_loaded, cancel_wanted;
static bool dry;                   /* emulator exercise (USB 0xa3): no homing, head or sheet checks; dry executor */
static uint32_t expect;
static unsigned rows_in, in_row, band_rows, band_next, bands_total, pages, sweeps, failures, last_fail, routines_before, phases_skipped;
/* Sweep planning (print-speed brief step 2): the 16-column tiles carrying ink
 * in the rows a phase prints are read from the rings when the phase starts
 * (exact per tile: one interval per row would merge a left and a right mark
 * into a full-width sweep, QI272327); a phase's sweeps are the greedy cover
 * of the occupied tiles by windows of at most seg_cols() columns, each
 * cropped to its last occupied tile (32-column granularity, 16-column start
 * quantization as the tile layout and placement need). A phase with no ink
 * is skipped; its feed still happens, so the paper coordinate is unchanged. */
static uint8_t k_blank[K_RING], c_blank[3][C_RING];   /* ring row holds no ink: the planner skips reading it (QI272358: 5 s of ring reads for 68 empty passes) */
static struct sweep { uint16_t col0, cols; } plan[4];
static unsigned plan_n, plan_i, pending_ulf;   /* feeds of skipped passes, merged into moves of at most 1200 Ulf before the next printed pass */
static bool planned;
#define FEED_MAX 1200u
#define TILES (COLS / 16u)
static unsigned nozzle_phase;      /* 0 or 16 paper rows into this band's two-pass coverage */
static uint32_t job_started_ms;
/* Timing profile (print-speed brief step 1): device time per phase of one
 * job. Phases that run asynchronously (homing, head, input wait, routines)
 * subtract the row conversion that ran inside them, so every bucket is
 * disjoint and `other` is the unassigned remainder of the job time. */
enum { P_CONVERT, P_FILL, P_INPUT, P_HOME, P_HEAD, P_LOAD, P_PREPARE, P_FIRE, P_ADVANCE, P_EJECT, P_N };
static uint32_t prof_msacc[P_N], prof_rem[P_N], prof_calls[P_N], bytes_in, conv_ticks;   /* conv_ticks: running tick total of conversion (wraps; deltas only) */
static uint32_t phase_t0, phase_conv0; static unsigned phase_kind; static bool phase_open, input_waiting;
static struct hybrid_profile shim0; static uint32_t shim_routines0[22];
/* 32-bit only (no libgcc): whole milliseconds accumulate, the tick remainder carries. */
static void prof_add_ticks(unsigned k, uint32_t ticks)
{
    prof_rem[k] += ticks; prof_msacc[k] += prof_rem[k] / CLOCK_TICKS_PER_MS; prof_rem[k] %= CLOCK_TICKS_PER_MS; ++prof_calls[k];
}
static void phase_begin(unsigned kind) { phase_kind = kind; phase_t0 = app_clock_ticks(); phase_conv0 = conv_ticks; phase_open = true; }
static void phase_end(void)
{
    if (!phase_open) return;
    phase_open = false;
    prof_add_ticks(phase_kind, (app_clock_ticks() - phase_t0) - (conv_ticks - phase_conv0));
}
static uint32_t prof_ms(unsigned k) { return prof_msacc[k]; }
static struct urf_decoder decoder;
static uint8_t k_ring[K_RING][ROW_BYTES], c_ring[3][C_RING][ROW_BYTES];
static const uint8_t bayer[8][8] = {
    {0, 32, 8, 40, 2, 34, 10, 42}, {48, 16, 56, 24, 50, 18, 58, 26}, {12, 44, 4, 36, 14, 46, 6, 38}, {60, 28, 52, 20, 62, 30, 54, 22},
    {3, 35, 11, 43, 1, 33, 9, 41}, {51, 19, 59, 27, 49, 17, 57, 25}, {15, 47, 7, 39, 13, 45, 5, 37}, {63, 31, 55, 23, 61, 29, 53, 21}};

bool print_engine_available(void) { return hybrid_panel_available(); }
bool print_engine_busy(void) { return state == PE_RECEIVING || state == PE_PRINTING; }
enum print_engine_state print_engine_state(void) { return state; }

static unsigned seg_cols(void) { return mono ? SEG_MONO : SEG_COLOUR; }
/* Rows the rings can hold ahead of the next band (ring arithmetic in the header comment). */
static unsigned accept_limit(void) { return band_rows * band_next + (mono ? K_RING : C_RING); }
static int place_for_x(unsigned x)
{
    unsigned raw = (x + STOCK_CAP + 32u) / 64u * 64u;
    return (int)((raw - PLACE_K) / 4u);
}

/* ---- rows in: separation, scaling, halftone ---- */
static bool sink_page(const struct urf_page *p)
{
    if (page_open) return false;                              /* the previous page is still printing */
    mono = p->components == 1u;
    band_rows = mono ? 304u : 152u;
    rows_in = in_row = band_next = bands_total = plan_n = plan_i = nozzle_phase = pending_ulf = 0; planned = false; page_open = true; input_done = false;
    dbg_puts("[print] page "); dbg_dec(p->index); dbg_putc('/'); dbg_dec(p->count); dbg_putc(' ');
    dbg_dec(p->width); dbg_putc('x'); dbg_dec(p->height); dbg_puts(mono ? " grey\n" : " colour\n");
    return true;
}

/* One input row -> output rows d and d + 1 (each with its own dither row).
 * Per output byte the eight columns' bits are gathered in registers and
 * stored once per plane and row: the rings live in uncached DRAM, so the
 * earlier per-column read-modify-write dominated (24 ms per input row). */
static void __attribute__((noinline)) convert_pair(const uint8_t *px, unsigned width, unsigned comps, unsigned d, bool blank)   /* noinline: the emulator capture hooks it */
{
    uint8_t *k0 = k_ring[d % K_RING], *k1 = k_ring[(d + 1u) % K_RING];
    uint8_t *c0 = c_ring[0][d % C_RING], *c1 = c_ring[0][(d + 1u) % C_RING];
    uint8_t *m0 = c_ring[1][d % C_RING], *m1 = c_ring[1][(d + 1u) % C_RING];
    uint8_t *y0 = c_ring[2][d % C_RING], *y1 = c_ring[2][(d + 1u) % C_RING];
    k_blank[d % K_RING] = k_blank[(d + 1u) % K_RING] = blank;
    for (unsigned i = 0; i < 3; ++i) c_blank[i][d % C_RING] = c_blank[i][(d + 1u) % C_RING] = blank;
    if (blank) {                                               /* white row (URF white tail / repeat): no halftoning */
        memset(k0, 0, ROW_BYTES); memset(k1, 0, ROW_BYTES);
        if (!mono) { memset(c0, 0, ROW_BYTES); memset(c1, 0, ROW_BYTES); memset(m0, 0, ROW_BYTES); memset(m1, 0, ROW_BYTES); memset(y0, 0, ROW_BYTES); memset(y1, 0, ROW_BYTES); }
        return;
    }
    const uint8_t *t0 = bayer[d & 7u], *t1 = bayer[(d + 1u) & 7u];
    for (unsigned byte = 0; byte < ROW_BYTES; ++byte) {
        unsigned kb0 = 0, kb1 = 0, cb0 = 0, cb1 = 0, mb0 = 0, mb1 = 0, yb0 = 0, yb1 = 0;
        for (unsigned bit = 0; bit < 8u; ++bit) {
            unsigned X = byte * 8u + bit, xi = X_IN0 + ((X * 125u) >> 8);   /* X / 2.048 */
            if (xi >= width) break;
            unsigned th0 = (unsigned)t0[bit] * 4u + 2u, th1 = (unsigned)t1[bit] * 4u + 2u, b = 1u << bit;
            const uint8_t *s = px + xi * comps;
            if (mono) {
                unsigned ki = 255u - s[0];
                if (ki > th0) kb0 |= b;
                if (ki > th1) kb1 |= b;
                continue;
            }
            unsigned ci = 255u - s[0], mi = 255u - s[1], yi = 255u - s[2];
            unsigned ki = ci < mi ? (ci < yi ? ci : yi) : (mi < yi ? mi : yi);   /* grey component replacement */
            ci -= ki; mi -= ki; yi -= ki;
            if (ki > th0) kb0 |= b;
            if (ki > th1) kb1 |= b;
            if (ci > th0) cb0 |= b;
            if (ci > th1) cb1 |= b;
            if (mi > th0) mb0 |= b;
            if (mi > th1) mb1 |= b;
            if (yi > th0) yb0 |= b;
            if (yi > th1) yb1 |= b;
        }
        k0[byte] = (uint8_t)kb0; k1[byte] = (uint8_t)kb1;
        if (!mono) { c0[byte] = (uint8_t)cb0; c1[byte] = (uint8_t)cb1; m0[byte] = (uint8_t)mb0; m1[byte] = (uint8_t)mb1; y0[byte] = (uint8_t)yb0; y1[byte] = (uint8_t)yb1; }
        if (!(byte & 31u)) app_yield();
    }
}

static bool sink_row(const uint8_t *px, unsigned width, unsigned comps, bool blank)
{
    if (!page_open) return false;
    if (in_row < Y_IN0) { ++in_row; return true; }           /* above the printable area */
    if (rows_in + 2u > MAX_ROWS) { ++in_row; return true; }   /* below it: dropped */
    if (rows_in + 2u > accept_limit()) return false;          /* rings full: re-offered after the next band */
    ++in_row;
    uint32_t t0 = app_clock_ticks();
    convert_pair(px, width, comps, rows_in, blank); rows_in += 2u;   /* 300 -> 600 dpi: the pair shares pixels */
    { uint32_t dt = app_clock_ticks() - t0; conv_ticks += dt; prof_add_ticks(P_CONVERT, dt); }
    return true;
}

static void page_end(void)
{
    if (input_done) return;
    input_done = true;
    unsigned span = rows_in + (mono ? 0u : COLOUR_LEAD);    /* colour pages: the black lead trails the data */
    bands_total = (span + band_rows - 1u) / band_rows;
    if (!bands_total) bands_total = 1;
    dbg_puts("[print] page data complete: "); dbg_dec(rows_in); dbg_puts(" rows, "); dbg_dec(bands_total); dbg_puts(" bands\n");
}

static const struct urf_sink sink = {sink_page, sink_row, page_end};

/* ---- job control ---- */
bool print_engine_start(void)
{
    if (!print_engine_available() || print_engine_busy() || service_busy() || head_fire_busy() || cartridge_busy()) return false;
    urf_init(&decoder, &sink);
    state = PE_RECEIVING; step = S_NONE; waiting = page_open = input_done = cancel_wanted = false;
    rows_in = band_next = bands_total = plan_n = plan_i = pages = sweeps = phases_skipped = pending_ulf = 0; planned = false; job_fresh = true; sheet_loaded = false;
    job_started_ms = time_ms(); last_fail = 0;
    for (unsigned i = 0; i < P_N; ++i) { prof_msacc[i] = prof_rem[i] = prof_calls[i] = 0; }
    bytes_in = 0; phase_open = input_waiting = false; hybrid_profile_get(&shim0); hybrid_profile_routines(shim_routines0);
    dbg_puts("[print] job start\n");
    return true;
}

unsigned print_engine_feed(const uint8_t *data, unsigned len)
{
    if (state != PE_RECEIVING) return len;                    /* a finished or failed job drains its data */
    /* The shim services IPP while stock calls block, but its motor servos
     * also need that polling loop. Row conversion yields only to scanner
     * work: doing it here can overrun an LF move (QI272220: +64 Ulf).
     * Keep the USB packet pending until the stock routine has finished. */
    if (hybrid_busy()) return 0;
    unsigned n = urf_feed(&decoder, data, len);
    bytes_in += n;
    if (decoder.error) { dbg_puts("[print] bad URF document\n"); ++failures; last_fail = 1; state = page_open ? PE_PRINTING : PE_FAILED; if (page_open) page_end(); return len; }
    return n;
}

void print_engine_finish(void)
{
    if (state != PE_RECEIVING) return;
    if (!page_open && !decoder.done && !decoder.page_pending) { dbg_puts("[print] empty document\n"); ++failures; last_fail = 2; state = PE_FAILED; return; }
    if (page_open) page_end();                                /* a truncated page prints what arrived */
    state = PE_PRINTING;
}

void print_engine_cancel(void)
{
    if (print_engine_busy()) { cancel_wanted = true; dbg_puts("[print] cancel requested\n"); }
}

bool print_engine_control(unsigned op, unsigned arg)
{
    if (op == 1u) { if (print_engine_busy()) return false; dry = arg != 0; dbg_puts(dry ? "[print] dry mode\n" : "[print] live mode\n"); return true; }
    if (op == 2u) { print_engine_cancel(); return true; }
    return false;
}

/* Routine flags: live runs drive the carriage and feed; dry runs are the emulator's. */
#define F_LOAD (dry ? 0x800u : 0x100u | 0x800u | 0x1000u)
#define F_EJECT (dry ? 0x800u | 0x400u : 0x100u | 0x800u | 0x400u)
#define F_FIRE (dry ? 0x1000u | 0x2000u : 0x100u | 0x1000u)

/* ---- band output ---- */
static void write_tiles(uint8_t *raster, unsigned stride, unsigned slots, const uint8_t *ring, unsigned ring_rows, int row0, unsigned rows, unsigned col0, unsigned ncols)
{
    for (unsigned g = 0; g < rows / 8u && g < slots; ++g)
        for (unsigned r = 0; r < 8u; ++r) {
            unsigned physical = 8u * g + r, logical = physical + nozzle_phase;
            int d = row0 + (int)logical;
            bool used = (physical & 31u) < 16u && logical < rows && d >= 0 && (unsigned)d < rows_in;
            const uint8_t *src = used ? ring + ((unsigned)d % ring_rows) * ROW_BYTES + col0 / 8u : NULL;
            /* Explicit blanking also removes the shim's synthetic eight-row
             * preparation packet from leading/trailing blank black rows. */
            for (unsigned t = 0; t < ncols / 16u; ++t) {
                uint8_t *o = raster + t * stride + 16u * g + 2u * r;
                o[0] = src ? src[2u * t] : 0; o[1] = src ? src[2u * t + 1u] : 0;
            }
            app_yield();
        }
}

/* Rows of plane i that this phase prints (the used physical rows mapped to data rows) carry ink in these tiles. */
static bool used_row(unsigned i, unsigned r, int *d)
{
    unsigned logical = r + nozzle_phase;
    *d = (int)(band_rows * band_next) - (i == 0 && !mono ? (int)COLOUR_LEAD : 0) + (int)logical;
    return (r & 31u) < 16u && logical < band_rows && *d >= 0 && (unsigned)*d < rows_in;
}

static bool tile_set(const uint32_t *occ, unsigned t) { return (occ[t >> 5] & (1u << (t & 31u))) != 0; }
static unsigned next_tile(const uint32_t *occ, unsigned from) { for (unsigned t = from; t < TILES; ++t) if (tile_set(occ, t)) return t; return TILES; }
static void plan_phase(void)
{
    uint32_t occ[(TILES + 31u) / 32u] = {0};
    for (unsigned i = 0; i < (mono ? 1u : 4u); ++i)
        for (unsigned r = 0; r < band_rows; ++r) {
            int d;
            if (!used_row(i, r, &d)) continue;
            if (i == 0 ? k_blank[(unsigned)d % K_RING] : c_blank[i - 1u][(unsigned)d % C_RING]) continue;
            const uint32_t *row = (const uint32_t *)(i == 0 ? k_ring[(unsigned)d % K_RING] : c_ring[i - 1u][(unsigned)d % C_RING]);
            for (unsigned j = 0; j < ROW_BYTES / 4u; ++j) {           /* one word = tiles 2j (low half) and 2j + 1 */
                uint32_t v = row[j];
                if (v & 0xffffu) occ[(2u * j) >> 5] |= 1u << ((2u * j) & 31u);
                if (v >> 16) occ[(2u * j + 1u) >> 5] |= 1u << ((2u * j + 1u) & 31u);
            }
            if (!(r & 15u)) app_yield();
        }
    plan_n = plan_i = 0;
    unsigned window = seg_cols() / 16u;
    for (unsigned t = next_tile(occ, 0); t < TILES && plan_n < 4u; t = next_tile(occ, t + window)) {
        unsigned end = t + window < TILES ? t + window : TILES, last = t;
        for (unsigned u = t; u < end; ++u) if (tile_set(occ, u)) last = u;
        unsigned cols = ((last - t + 1u) * 16u + 31u) / 32u * 32u, col0 = t * 16u;
        if (col0 + cols > COLS) col0 = COLS - cols;
        plan[plan_n].col0 = (uint16_t)col0; plan[plan_n].cols = (uint16_t)cols; ++plan_n;
    }
}

static bool fill_band(void)
{
    unsigned c0 = plan[plan_i].col0, n = plan[plan_i].cols;
    static const unsigned groups[4] = {6, 0, 2, 4};              /* K, C, M, Y managers */
    for (unsigned i = 0; i < (mono ? 1u : 4u); ++i) {
        unsigned slots, width, stride, size;
        uint8_t *raster = hybrid_manager_raster(groups[i], &slots, &width, &stride, &size);
        if (!raster || width < n || (n / 16u) * stride > size) { dbg_puts("[print] manager not ready\n"); return false; }
        int row0 = (int)(band_rows * band_next) - (i == 0 && !mono ? (int)COLOUR_LEAD : 0);
        write_tiles(raster, stride, slots, i == 0 ? &k_ring[0][0] : &c_ring[i - 1u][0][0], i == 0 ? K_RING : C_RING, row0, band_rows, c0, n);
    }
    return true;
}

/* Start a stock routine; the poll resumes at `next` once it has finished with `want`. */
static bool start(unsigned value, unsigned index, enum step next, uint32_t want, unsigned code)
{
    routines_before = hybrid_routines_run();
    if (!hybrid_run(value, index)) { dbg_puts("[print] routine refused\n"); last_fail = code; return false; }
    phase_begin(next == S_LOADED ? P_LOAD : next == S_PREPARED ? P_PREPARE : next == S_FIRED ? P_FIRE : next == S_EJECTED ? P_EJECT : P_ADVANCE);
    waiting = true; expect = want; step = next; return true;
}

static void profile_log(void);
static void phase_done(void);
static void fail(unsigned code)
{
    ++failures; last_fail = code; state = PE_FAILED; waiting = false;
    dbg_puts("[print] failed, code "); dbg_dec(code); dbg_putc('\n');
    if (sheet_loaded && start(9u | F_EJECT, AMBIENT, S_EJECTED, 0, code)) return;   /* leave the sheet out */
    step = S_NONE;
}

static void eject(void) { if (!start(9u | F_EJECT, AMBIENT, S_EJECTED, 0, 30)) fail(30); }

/* After a phase's sweeps (or a skipped phase): the 16-row feed to the second
 * pass, or the band's remaining feed, then the next band or the eject. */
static void phase_done(void)
{
    bool printed = plan_n != 0;
    unsigned ulf = nozzle_phase == 0 ? 32u : 2u * band_rows - 32u;
    plan_n = plan_i = 0;
    if (nozzle_phase == 0) nozzle_phase = 16;
    else { nozzle_phase = 0; ++band_next; if (input_done && band_next >= bands_total) { eject(); return; } }
    if (!printed) { pending_ulf += ulf; step = S_BAND; return; }   /* skipped: merged into the next printed pass's feed */
    if (!start(10u | 0x800u, ulf, S_ADVANCED, 0, 24)) fail(24);
}

static void page_done(void)
{
    ++pages; page_open = false; band_next = bands_total = 0; rows_in = 0;
    dbg_puts("[print] page printed, sweeps so far "); dbg_dec(sweeps); dbg_putc('\n');
    if (cancel_wanted) { state = PE_CANCELED; dbg_puts("[print] canceled\n"); }
}

void print_engine_poll(void)
{
    if (!print_engine_busy() && state != PE_FAILED) return;
    if (state == PE_FAILED && !waiting && step == S_NONE) return;
    if (page_open) (void)urf_flush(&decoder);                 /* rows/pages refused earlier are re-offered */
    if (waiting) {
        if (hybrid_routines_run() == routines_before || hybrid_busy()) return;
        waiting = false; phase_end();
        if (hybrid_last_result() != expect && state != PE_FAILED) {
            /* The emulator's sheet model leaves the PE sensor clear after the loader, so its LF
             * advances report no paper (0xbada); dry runs log and continue, live runs fail. */
            if (dry && (step == S_FIRST || step == S_ADVANCED)) { dbg_puts("[print] dry: advance result ignored\n"); }
            else { fail(40u + (unsigned)step); return; }
        }
    }
    switch (step) {
    case S_NONE:
        if (state == PE_FAILED) return;
        if (cancel_wanted) { state = PE_CANCELED; dbg_puts("[print] canceled\n"); return; }
        if (!page_open) {
            if (state == PE_PRINTING && !decoder.page_pending) { state = PE_COMPLETED; dbg_puts("[print] job complete: "); dbg_dec(pages); dbg_puts(" pages, "); dbg_dec(sweeps); dbg_puts(" sweeps, "); dbg_dec(time_ms() - job_started_ms); dbg_puts(" ms\n"); profile_log(); }
            return;
        }
        if (rows_in < 2u && !input_done) return;             /* wait for the first rows before moving paper */
        if (service_busy() || cartridge_busy() || head_fire_busy()) return;
        if (dry) { step = S_HEAD; return; }
        if (!carriage_home_request()) return;
        phase_begin(P_HOME); step = S_HOMING; return;
    case S_HOMING:
        if (carriage_busy()) return;
        phase_end();
        if (!carriage_referenced()) { fail(10); return; }
        if (!head_if_prepared()) { if (!cartridge_request(3u | 0x200u, 0)) { fail(11); return; } phase_begin(P_HEAD); }
        step = S_HEAD; return;
    case S_HEAD:
        if (cartridge_busy()) return;
        phase_end();
        if (!dry && !head_if_prepared()) { fail(12); return; }
        if (!start(8u | F_LOAD, AMBIENT, S_LOADED, 0, 13)) fail(13);
        return;
    case S_LOADED:
        if (!dry && !hybrid_sheet_loaded()) { fail(14); return; }
        sheet_loaded = true;
        if (!start(10u | 0x800u, mono ? FIRST_ULF_MONO : FIRST_ULF_COLOUR, S_FIRST, 0, 15)) fail(15);
        return;
    case S_FIRST:
        plan_n = plan_i = nozzle_phase = pending_ulf = 0; planned = false; step = S_BAND; return;
    case S_BAND:
        if (plan_i == 0 && !planned) {
            if (cancel_wanted) { eject(); return; }
            if (input_done && band_next >= bands_total) { eject(); return; }   /* pending feed dropped: the eject moves the sheet out */
            if (!input_done && rows_in < band_rows * (band_next + 1u)) {    /* the intake fills the rings */
                if (!input_waiting) { input_waiting = true; phase_begin(P_INPUT); }
                return;
            }
            if (input_waiting) { input_waiting = false; phase_end(); }
            plan_phase(); planned = true;
            if (plan_n == 0) { ++phases_skipped; planned = false; phase_done(); return; }  /* nothing to print: its feed is merged */
        }
        if (pending_ulf) {                                      /* skipped passes' travel first, same total distance */
            unsigned ulf = pending_ulf > FEED_MAX ? FEED_MAX : pending_ulf;
            pending_ulf -= ulf;
            if (!start(10u | 0x800u, ulf, S_ADVANCED, 0, 24)) fail(24);
            return;
        }
        {
            const struct sweep *sw = &plan[plan_i];
            if (!hybrid_band_params(sw->cols, 304u | (mono ? 0u : 0x800u) | ((mono ? 1u : 7u) << 12), 0, false) ||
                !hybrid_band_params(0, 0, place_for_x(X_START + 16u * sw->col0), true)) { fail(18); return; }
            if (!start(5u | (job_fresh ? 0x400u : 0u), AMBIENT, S_PREPARED, 1, 19)) { fail(19); return; }
            job_fresh = false; return;
        }
    case S_PREPARED:
        { uint32_t t0 = app_clock_ticks(); bool ok = fill_band(); prof_add_ticks(P_FILL, app_clock_ticks() - t0); if (!ok) { fail(21); return; } }
        if (!start(7u | F_FIRE, AMBIENT, S_FIRED, 1, 22)) fail(22);
        return;
    case S_FIRED:
        ++sweeps;
        if (++plan_i < plan_n) { step = S_BAND; return; }
        planned = false; phase_done(); return;
    case S_ADVANCED:
        step = S_BAND; return;
    case S_EJECTED:
        sheet_loaded = false;
        if (!dry && carriage_home_request()) { phase_begin(P_HOME); step = S_HOMING2; return; }
        step = S_NONE; if (state != PE_FAILED) page_done(); return;
    case S_HOMING2:
        if (carriage_busy()) return;
        phase_end(); step = S_NONE; if (state != PE_FAILED) page_done(); return;
    }
}

/* ---- profile output: one summary per job (log) and USB 0xa2 wIndex 1/2 ---- */
static const char *const engine_names[16] = {"total", "convert", "rows", "fill", "fills", "input-wait", "prepare", "fire", "advance", "load", "eject", "home", "head", "bytes", "other", "skipped"};
static const char *const shim_names[16] = {"cr", "cr-moves", "lf", "lf-moves", "wait", "waits", "timeout", "timeouts", "delay", "delays", "raminit", "prepare", "finish", "drain", "arena", "errors"};
static void profile_words(unsigned which, uint32_t *v)
{
    uint32_t total = state == PE_IDLE ? 0 : time_ms() - job_started_ms;
    if (which == 1u) {
        /* engine phases: disjoint device ms (conversion inside an asynchronous phase is charged to convert only) */
        v[0] = total; v[1] = prof_ms(P_CONVERT); v[2] = prof_calls[P_CONVERT]; v[3] = prof_ms(P_FILL); v[4] = prof_calls[P_FILL];
        v[5] = prof_ms(P_INPUT); v[6] = prof_ms(P_PREPARE); v[7] = prof_ms(P_FIRE); v[8] = prof_ms(P_ADVANCE); v[9] = prof_ms(P_LOAD);
        v[10] = prof_ms(P_EJECT); v[11] = prof_ms(P_HOME); v[12] = prof_ms(P_HEAD); v[13] = bytes_in;
        uint32_t sum = 0;
        for (unsigned i = 0; i < P_N; ++i) sum += prof_ms(i);
        v[14] = total > sum ? total - sum : 0; v[15] = phases_skipped;
        return;
    }
    /* shim counters since the job started; motion overlaps the event waits, both lie inside fire/advance/load/eject */
    struct hybrid_profile h; hybrid_profile_get(&h);
    const uint32_t *a = (const uint32_t *)&h, *b = (const uint32_t *)&shim0;
    for (unsigned i = 0; i < 14; ++i) v[i] = a[i] - b[i];
    v[14] = h.arena_peak; v[15] = h.errors - shim0.errors;
}
static void profile_log(void)
{
    uint32_t v[16];
    for (unsigned which = 1; which <= 2; ++which) {
        profile_words(which, v);
        dbg_puts(which == 1 ? "[print] profile ms:" : "[print] shim ms:");
        for (unsigned i = 0; i < 16; ++i) { dbg_putc(' '); dbg_puts((which == 1 ? engine_names : shim_names)[i]); dbg_putc(' '); dbg_dec(v[i]); }
        dbg_putc('\n');
    }
}

unsigned print_engine_profile(uint8_t *out, unsigned which)
{
    uint32_t v[22];
    if (which == 3u) {                                       /* "PRF3": per routine 1..10, delay ms | calls << 20 (words 1..11), wait ms | timeouts << 20 (12..22) */
        hybrid_profile_routines(v);
        put32(out, 0x33465250u);
        for (unsigned i = 0; i < 22; ++i) put32(out + 4 + 4 * i, ((v[i] & 0xfffffu) - (shim_routines0[i] & 0xfffffu)) | (((v[i] >> 20) - (shim_routines0[i] >> 20)) << 20));
        return 92;
    }
    profile_words(which, v);
    put32(out, which == 1u ? 0x31465250u : 0x32465250u);   /* "PRF1" / "PRF2" */
    for (unsigned i = 0; i < 15; ++i) put32(out + 4 + 4 * i, v[i]);
    put32(out + 64, v[15]);
    return 68;
}

unsigned print_engine_status(uint8_t *out)
{
    put32(out, 0x54525031u);                                 /* "PRT1" */
    put32(out + 4, (uint32_t)state | (uint32_t)step << 8 | (waiting ? 0x10000u : 0) | (dry ? 0x400000u : 0) | (mono ? 0x20000u : 0) | (page_open ? 0x40000u : 0) | (input_done ? 0x80000u : 0) | (sheet_loaded ? 0x100000u : 0) | (cancel_wanted ? 0x200000u : 0));
    put32(out + 8, rows_in); put32(out + 12, band_next); put32(out + 16, bands_total); put32(out + 20, plan_i | plan_n << 8 | phases_skipped << 16);
    put32(out + 24, pages); put32(out + 28, sweeps); put32(out + 32, failures); put32(out + 36, last_fail);
    put32(out + 40, decoder.page.width); put32(out + 44, decoder.page.height); put32(out + 48, decoder.page.index); put32(out + 52, decoder.page.count);
    put32(out + 56, state == PE_IDLE ? 0 : time_ms() - job_started_ms); put32(out + 60, hybrid_routines_run());
    return 64;
}
