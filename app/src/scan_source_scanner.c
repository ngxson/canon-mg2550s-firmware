/* Scan source backed by the scanner: the page sequencer (scan_page.c) homes,
 * steps to each line and captures it stationary with optical profile 0; this
 * file turns each 2592-sample line into output pixels.
 *
 * Geometry: 38400 encoder counts per inch along travel (128 per 1/300 inch),
 * so 75 and 150 dpi are pitches of 512 and 256 counts (the sequencer's limits).
 * Across, the 2592 samples are assumed to span the 216 mm platen width (as the
 * page PoC assumed); each output pixel averages the samples it covers.
 *
 * Levels are not calibrated: no dark/white shading. A fixed linear map per
 * mode takes a measured dark level to black and a measured paper-white level
 * to white, followed by a gamma 1/2.2 curve (the sensor is linear; viewers
 * expect sRGB-like encoding, without it paper looks grey). The CIS is driven
 * monochrome, so only greyscale (and thresholded B/W) exist. */
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
#define CONT_BLACK 1000u
#define CONT_WHITE 36000u
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

static const uint16_t resolutions[] = {75, 150};
const struct scan_caps scan_caps = {
    .max_width = 2550, .max_height = 3507,          /* 216 x 297 mm platen */
    .min_size = 16, .optical = 300,
    .resolutions = resolutions, .resolution_count = ARRAY_SIZE(resolutions),
    .gray = true, .color = false,
};

static uint16_t first_sample[SCAN_MAX_PIXELS];
static uint8_t sample_count[SCAN_MAX_PIXELS];
static uint16_t reciprocal[9];          /* 65536 / n, n = samples per pixel */
static unsigned black_level, level_scale;   /* fixed point: no per-pixel division */
static unsigned pixels;

bool scan_source_start(const struct scan_params *p)
{
    unsigned step = 1;
    while (step * p->resolution < 300u) ++step;
    if (p->color || step * p->resolution != 300u || p->pixels > SCAN_MAX_PIXELS) return false;
    pixels = p->pixels;
    for (unsigned n = 1; n < ARRAY_SIZE(reciprocal); ++n) reciprocal[n] = (uint16_t)((65536u + n - 1u) / n);
    for (unsigned i = 0; i < pixels; ++i) {
        if (!(i & 127u)) app_yield();
        unsigned u = p->x + i * step;
        unsigned s0 = u * SAMPLES / SAMPLE_SPAN, s1 = (u + step) * SAMPLES / SAMPLE_SPAN;
        if (s0 >= SAMPLES) s0 = SAMPLES - 1u;
        if (s1 > SAMPLES) s1 = SAMPLES;
        if (s1 <= s0) s1 = s0 + 1u;
        if (s1 - s0 > 8u) s1 = s0 + 8u;               /* 75 dpi needs at most 5 */
        first_sample[i] = (uint16_t)s0; sample_count[i] = (uint8_t)(s1 - s0);
    }
    if (!scan_page_start(p->lines, step * COUNTS_PER_UNIT, p->y * COUNTS_PER_UNIT, true)) return false;
    bool cont = scan_page_continuous();
    black_level = cont ? CONT_BLACK : STEP_BLACK;
    level_scale = 255u * 65536u / ((cont ? CONT_WHITE : STEP_WHITE) - black_level);
    return true;
}

int scan_source_row(uint8_t *out)
{
    unsigned row;
    const uint16_t *samples = scan_page_take(&row);
    if (!samples) return scan_page_failed() ? -1 : 0;
    app_section = "convert";
    for (unsigned i = 0; i < pixels; ++i) {
        if (!(i & 15u)) app_yield();
        const uint16_t *s = samples + first_sample[i];
        unsigned sum = 0, n = sample_count[i];
        for (unsigned k = 0; k < n; ++k) sum += s[k];
        unsigned level = (unsigned)(((unsigned long long)sum * reciprocal[n]) >> 16);
        unsigned value = level <= black_level ? 0u : ((level - black_level) * level_scale) >> 16;
        out[i] = gamma_lut[value > 255u ? 255u : value];
    }
    return scan_page_release(row) ? 1 : -1;
}

void scan_source_cancel(void) { scan_page_cancel(); }
