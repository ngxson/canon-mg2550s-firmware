/* Scan source backed by the scanner: the page sequencer (scan_page.c) homes
 * and acquires lines during continuous travel or stationary steps; this file
 * turns each 2592-sample line into output pixels.
 *
 * Geometry: 38400 encoder counts per inch along travel (128 per 1/300 inch),
 * so 75 and 150 dpi are pitches of 512 and 256 counts (the sequencer's limits).
 * Across, the 2592 samples are assumed to span the 216 mm platen width (as the
 * page PoC assumed); each output pixel averages the samples it covers.
 *
 * Run-in dark/white references provide per-sample shading, with fixed levels
 * as a fallback, followed by a gamma 1/2.2 curve (the sensor is linear; viewers
 * expect sRGB-like encoding, without it paper looks grey). Colour uses the
 * three-phase illumination (one component row per LED); grey the combined
 * exposure. */
#include <string.h>
#include "app.h"

#define SAMPLES 2592u
#define SAMPLE_SPAN 2550u          /* 1/300 inch covered by the samples */
#define COUNTS_PER_UNIT 128u       /* encoder counts per 1/300 inch */
/* Stop-and-capture (AFE offset 0xff): dark ~26,000, saturating whites.
 * Continuous (AFE offset 0, exposure 6/8, gain 13): dark clips at 0; plain
 * paper p10/p50/p90 across the sensor ~23,000/30,000/41,000, the lid strip
 * ~31,000..48,000 (150-row runs, 2026-10-24; no shading correction yet). */
#define STEP_BLACK 26000u
#define STEP_WHITE 65000u
#define CONT_BLACK 1200u           /* AFE offset 0x80 pedestal: no-light level (platen edge bar) */
#define CONT_WHITE 48000u          /* fallback only; the run-in references below normally apply */
/* Run-in references (2026-10-27 raw ring dumps, 1/300 inch from the mechanical
 * scan start): the lid's white strip spans ~12..100, the platen-edge dark bar
 * ~128..152, stock's document origin lies 156 later than our start (swatch
 * edges against the stock scan), and stock's first column is 8 units right of
 * sample 0. Every page scans from the mechanical start; the run-in lines give
 * the per-sample white/dark arrays (stock's calibration, brief 21 §5).
 * The strip's central notch remains dark through ~30 units. Sampling
 * 20..90 averaged that notch into the centre columns, creating a bright
 * vertical stripe. Raw full-width reference captures show 40..75 is white
 * across the whole sensor and precedes the strip's trailing falloff. */
#define DOC_ORIGIN 156u
#define SAMPLE_ORIGIN 4u
#define STRIP_FROM 40u
#define STRIP_TO 76u
#define DARK_FROM 132u
#define DARK_TO 148u
#define TRAVEL_UNITS 3672u         /* mechanical start to the end of travel, 1/300 inch (stock's 3508 rows from its origin) */
/* gamma[i] = round(255 * (i / 255) ** (1 / 2.2)); mirrored by the emulator tests. */
static const uint8_t gamma_lut[256] = {
      0,  21,  28,  34,  39,  43,  46,  50,  53,  56,  59,  61,  64,  66,  68,  70,
     72,  74,  76,  78,  80,  82,  84,  85,  87,  89,  90,  92,  93,  95,  96,  98,
     99, 101, 102, 103, 105, 106, 107, 109, 110, 111, 112, 114, 115, 116, 117, 118,
    119, 120, 122, 123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135,
    136, 137, 138, 139, 140, 141, 142, 143, 144, 144, 145, 146, 147, 148, 149, 150,
    151, 151, 152, 153, 154, 155, 156, 156, 157, 158, 159, 160, 160, 161, 162, 163,
    164, 164, 165, 166, 167, 167, 168, 169, 170, 170, 171, 172, 173, 173, 174, 175,
    175, 176, 177, 178, 178, 179, 180, 180, 181, 182, 182, 183, 184, 184, 185, 186,
    186, 187, 188, 188, 189, 190, 190, 191, 192, 192, 193, 194, 194, 195, 195, 196,
    197, 197, 198, 199, 199, 200, 200, 201, 202, 202, 203, 203, 204, 205, 205, 206,
    206, 207, 207, 208, 209, 209, 210, 210, 211, 212, 212, 213, 213, 214, 214, 215,
    215, 216, 217, 217, 218, 218, 219, 219, 220, 220, 221, 221, 222, 223, 223, 224,
    224, 225, 225, 226, 226, 227, 227, 228, 228, 229, 229, 230, 230, 231, 231, 232,
    232, 233, 233, 234, 234, 235, 235, 236, 236, 237, 237, 238, 238, 239, 239, 240,
    240, 241, 241, 242, 242, 243, 243, 244, 244, 245, 245, 246, 246, 247, 247, 248,
    248, 249, 249, 249, 250, 250, 251, 251, 252, 252, 253, 253, 254, 254, 255, 255
};

static const uint16_t resolutions[] = {75, 150, 300};
const struct scan_caps scan_caps = {
    .max_width = 2480, .max_height = 3507,          /* 210 x 297 mm, stock's output size */
    .min_size = 16, .optical = 300,
    .resolutions = resolutions, .resolution_count = ARRAY_SIZE(resolutions),
    .gray = true, .color = true,
};

static uint16_t first_sample[SCAN_MAX_PIXELS];
static uint8_t sample_count[SCAN_MAX_PIXELS];
static uint32_t reciprocal[9];          /* 16.16: n=1 requires 65536, not uint16_t */
static unsigned black_level, level_scale;   /* fixed point: no per-pixel division */
static unsigned pixels, components;
/* Per-sample shading from the page's dark/white references (scanner_calibrate):
 * out = (raw - dark[x]) * scale[x] >> 16 with scale = 255 * 65536 / (white[x] -
 * dark[x]) * WHITE_TARGET / 255; a sample whose references are unusable keeps
 * scale 0 and falls back to the fixed levels. 16.16 products fit 32 bits. */
#define CAL_MIN_SPAN 1024u
#define WHITE_TARGET 250u           /* home-burst path: the reference strip maps to this */
/* Stock match (2026-10-27 fit of the stock scan against ours on the swatch
 * sheet): stock keeps a black floor of ~5 % linear and maps the lid strip to
 * ~0.90 (red/green) and ~0.85 (blue) of full scale. value = floor + span. */
#define BLACK_FLOOR 14u
static const uint16_t white_span[3] = {230u, 230u, 217u};   /* per component, strip level above the floor */
static uint16_t cal_dark[3][SAMPLES];
static uint32_t cal_scale[3][SAMPLES];
static uint32_t acc_white[3][SAMPLES], acc_dark[3][SAMPLES];
static unsigned acc_nw, acc_nd, runin_total, runin_done, step_units, pad_rows;
static bool cal_built, cal_on;

static void cal_build(void)
{
    cal_on = scanner_cal_valid(components);
    for (unsigned c = 0; cal_on && c < components; ++c) {
        const uint16_t *d = scanner_cal_row(0, c), *w = scanner_cal_row(1, c);
        for (unsigned x = 0; x < SAMPLES; ++x) {
            if (!(x & 127u)) app_yield();
            cal_dark[c][x] = d[x];
            cal_scale[c][x] = w[x] > d[x] + CAL_MIN_SPAN ? (WHITE_TARGET * 65536u) / (w[x] - d[x]) : 0u;
        }
    }
    cal_built = true;
}
/* Component order of the three illumination phases as delivered by the ring
 * (0, 1, 2) to output R, G, B; set from the swatch measurement (USB 0x9c). */
static uint8_t comp_of_rgb[3] = {0, 1, 2};
bool scan_source_set_order(unsigned r, unsigned g, unsigned b)
{
    if (r > 2u || g > 2u || b > 2u || r == g || g == b || r == b) return false;
    comp_of_rgb[0] = (uint8_t)r; comp_of_rgb[1] = (uint8_t)g; comp_of_rgb[2] = (uint8_t)b; return true;
}

bool scan_source_start(const struct scan_params *p)
{
    unsigned step = 1;
    while (step * p->resolution < 300u) ++step;
    if (step * p->resolution != 300u || p->pixels > SCAN_MAX_PIXELS) return false;
    pixels = p->pixels; components = p->color ? 3u : 1u; cal_built = cal_on = false;
    for (unsigned n = 1; n < ARRAY_SIZE(reciprocal); ++n) reciprocal[n] = (65536u + n - 1u) / n;
    for (unsigned i = 0; i < pixels; ++i) {
        if (!(i & 127u)) app_yield();
        unsigned u = p->x + i * step + SAMPLE_ORIGIN;
        unsigned s0 = u * SAMPLES / SAMPLE_SPAN, s1 = (u + step) * SAMPLES / SAMPLE_SPAN;
        if (s0 >= SAMPLES) s0 = SAMPLES - 1u;
        if (s1 > SAMPLES) s1 = SAMPLES;
        if (s1 <= s0) s1 = s0 + 1u;
        if (s1 - s0 > 8u) s1 = s0 + 8u;               /* 75 dpi needs at most 5 */
        first_sample[i] = (uint16_t)s0; sample_count[i] = (uint8_t)(s1 - s0);
    }
    scan_page_set_components(components);
    step_units = step; runin_total = (DOC_ORIGIN + p->y + step - 1u) / step; runin_done = 0; acc_nw = acc_nd = 0; pad_rows = 0;
    for (unsigned c = 0; c < 3u; ++c)
        for (unsigned x = 0; x < SAMPLES; ++x) { if (!(x & 255u)) app_yield(); acc_white[c][x] = 0; acc_dark[c][x] = 0; }
    unsigned count = p->lines + runin_total, max_count = TRAVEL_UNITS / step;
    if (count > max_count) { pad_rows = count - max_count; count = max_count; }   /* beyond the travel: white rows */
    if (!scan_page_start(count, step * COUNTS_PER_UNIT, 0, true)) return false;
    bool cont = scan_page_continuous();
    black_level = cont ? CONT_BLACK : STEP_BLACK;
    level_scale = 255u * 65536u / ((cont ? CONT_WHITE : STEP_WHITE) - black_level);
    return true;
}

static inline uint8_t convert(const uint16_t *s, unsigned s0, unsigned n, unsigned c)
{
    unsigned sum = 0, value;
    if (cal_on && cal_scale[c][s0]) {
        const uint16_t *d = cal_dark[c] + s0; const uint32_t *g = cal_scale[c] + s0;
        for (unsigned k = 0; k < n; ++k) { unsigned v = s[k] > d[k] ? (unsigned)(s[k] - d[k]) : 0u; sum += (v * g[k]) >> 16; }
        value = BLACK_FLOOR + ((sum * reciprocal[n]) >> 16);
    } else {
        for (unsigned k = 0; k < n; ++k) sum += s[k];
        unsigned level = (unsigned)(((unsigned long long)sum * reciprocal[n]) >> 16);
        value = level <= black_level ? 0u : ((level - black_level) * level_scale) >> 16;
    }
    return gamma_lut[value > 255u ? 255u : value];
}

/* Per-sample references from the run-in: mean strip line as white, mean dark
 * bar as black; samples without a usable span fall back to the fixed levels. */
static void cal_from_runin(void)
{
    cal_on = acc_nw >= 2u && acc_nd >= 1u;
    for (unsigned c = 0; cal_on && c < components; ++c)
        for (unsigned x = 0; x < SAMPLES; ++x) {
            if (!(x & 15u)) app_yield();                      /* two software divisions per sample */
            uint32_t d = acc_dark[c][x] / acc_nd, w = acc_white[c][x] / acc_nw;
            cal_dark[c][x] = (uint16_t)(d > 65535u ? 65535u : d);
            cal_scale[c][x] = w > d + CAL_MIN_SPAN ? ((uint32_t)white_span[c] * 65536u) / (w - d) : 0u;
        }
    cal_built = true;
    dbg_puts("[cal] run-in white lines="); dbg_dec(acc_nw); dbg_puts(" dark lines="); dbg_dec(acc_nd);
    if (cal_on) { dbg_puts(" mid-sample white/dark:"); for (unsigned c = 0; c < components; ++c) { dbg_putc(' '); dbg_dec(acc_white[c][SAMPLES / 2u] / acc_nw); dbg_putc('/'); dbg_dec(acc_dark[c][SAMPLES / 2u] / acc_nd); } }
    dbg_puts(cal_on ? "\n" : " (fixed levels)\n");
}

static void accumulate(unsigned row, const uint16_t *samples, uint32_t (*acc)[SAMPLES])
{
    for (unsigned c = 0; c < components; ++c) {
        const uint16_t *s = components == 3u ? scan_page_component(row, c) : samples;
        if (!s) return;
        for (unsigned x = 0; x < SAMPLES; ++x) { if (!(x & 255u)) app_yield(); acc[c][x] += s[x]; }
    }
}

int scan_source_row(uint8_t *out)
{
    unsigned row;
    const uint16_t *samples;
    for (;;) {
        samples = scan_page_take(&row);
        if (!samples) {
            if (scan_page_failed()) return -1;
            if (pad_rows && runin_done >= runin_total && !scan_page_busy()) {   /* travel ended before the region: white */
                memset(out, 255, pixels * components); --pad_rows; return 1;
            }
            return 0;
        }
        if (runin_done >= runin_total) break;
        unsigned u = runin_done * step_units;                 /* 1/300 inch from the mechanical start */
        app_section = "run-in"; app_yield();
        if (u >= STRIP_FROM && u < STRIP_TO) { accumulate(row, samples, acc_white); ++acc_nw; }
        else if (u >= DARK_FROM && u < DARK_TO) { accumulate(row, samples, acc_dark); ++acc_nd; }
        if (!scan_page_release(row)) return -1;
        if (++runin_done == runin_total) cal_from_runin();
    }
    app_section = "convert";
    if (!cal_built) cal_build();
    if (components == 1u) {
        for (unsigned i = 0; i < pixels; ++i) {
            if (!(i & 15u)) app_yield();
            out[i] = convert(samples + first_sample[i], first_sample[i], sample_count[i], 0);
        }
    } else {
        const uint16_t *comp[3];
        for (unsigned c = 0; c < 3u; ++c) {
            comp[c] = scan_page_component(row, comp_of_rgb[c]);
            if (!comp[c]) return -1;
        }
        for (unsigned i = 0; i < pixels; ++i) {
            if (!(i & 15u)) app_yield();
            unsigned s0 = first_sample[i], n = sample_count[i];
            out[3u * i] = convert(comp[0] + s0, s0, n, comp_of_rgb[0]);
            out[3u * i + 1u] = convert(comp[1] + s0, s0, n, comp_of_rgb[1]);
            out[3u * i + 2u] = convert(comp[2] + s0, s0, n, comp_of_rgb[2]);
        }
    }
    return scan_page_release(row) ? 1 : -1;
}

void scan_source_cancel(void) { scan_page_cancel(); }
