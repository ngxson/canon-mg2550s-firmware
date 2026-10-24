/* Scan source backed by the scanner: the page sequencer (scan_page.c) homes,
 * steps to each line and captures it stationary with optical profile 0; this
 * file turns each 2592-sample line into output pixels.
 *
 * Geometry: 38400 encoder counts per inch along travel (128 per 1/300 inch),
 * so 75 and 150 dpi are pitches of 512 and 256 counts (the sequencer's limits).
 * Across, the 2592 samples are assumed to span the 216 mm platen width (as the
 * page PoC assumed); each output pixel averages the samples it covers.
 *
 * Levels are not calibrated: no dark/white shading. A fixed map takes the
 * dark platen-edge level (~26000) to black and near-saturation (~65000) to
 * white, as measured in a 24-row capture on the development board. The
 * CIS is driven monochrome, so only greyscale (and thresholded B/W) exist. */
#include "app.h"

#define SAMPLES 2592u
#define SAMPLE_SPAN 2550u          /* 1/300 inch covered by the samples */
#define COUNTS_PER_UNIT 128u       /* encoder counts per 1/300 inch */
#define BLACK_LEVEL 26000u
#define WHITE_LEVEL 65000u
/* Fixed-point factors: no divisions per pixel (software division is slow). */
#define LEVEL_SCALE (255u * 65536u / (WHITE_LEVEL - BLACK_LEVEL))

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
    return scan_page_start(p->lines, step * COUNTS_PER_UNIT, p->y * COUNTS_PER_UNIT, true);
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
        unsigned value = level <= BLACK_LEVEL ? 0u : ((level - BLACK_LEVEL) * LEVEL_SCALE) >> 16;
        out[i] = (uint8_t)(value > 255u ? 255u : value);
    }
    return scan_page_release(row) ? 1 : -1;
}

void scan_source_cancel(void) { scan_page_cancel(); }
