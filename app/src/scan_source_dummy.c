/* Synthetic scan source: a resolution-independent test chart.
 *
 * Stands in for the scanner so the eSCL path can be exercised end to end.
 * Pixel values depend only on the page position in 1/300 inch: red rises
 * left to right, green top to bottom, blue alternates on a half-inch
 * checkerboard, and a black line marks every inch. app/tests/validate_escl.py
 * recomputes the same chart to check every byte that reaches the host.
 *
 * The scanner implementation replaces this file: same caps table, same three
 * functions, rows delivered whenever the hardware has one. */
#include "app.h"

static const uint16_t resolutions[] = {75, 150, 300};
const struct scan_caps scan_caps = {
    .max_width = 2550, .max_height = 3507,          /* 216 x 297 mm platen */
    .min_size = 16, .optical = 300,
    .resolutions = resolutions, .resolution_count = ARRAY_SIZE(resolutions),
    .gray = true, .color = true,
};

static struct scan_params params;
static unsigned next_row, step;
static bool running;
/* Per-column part of the chart, computed once per job: red, and bit 0 the
 * checker column parity, bit 1 an inch line. The CPU has no divide
 * instruction, so per-pixel divisions made the source the bottleneck. */
static uint8_t column_red[SCAN_MAX_PIXELS], column_flags[SCAN_MAX_PIXELS];

bool scan_source_start(const struct scan_params *p)
{
    params = *p; next_row = 0;
    for (step = 1; step * p->resolution < 300u; ++step) {}
    running = step * p->resolution == 300u && p->pixels <= SCAN_MAX_PIXELS;
    for (unsigned i = 0; running && i < p->pixels; ++i) {
        if (!(i & 127u)) app_yield();
        unsigned u = p->x + i * step;
        column_red[i] = (uint8_t)(u * 255u / 2550u);
        column_flags[i] = (uint8_t)(((u / 150u) & 1u) | (u % 300u < 3u ? 2u : 0u));
    }
    return running;
}

int scan_source_row(uint8_t *out)
{
    if (!running || next_row >= params.lines) return -1;
    unsigned v = params.y + next_row * step;
    unsigned g = v * 255u / 3507u, checker = (v / 150u) & 1u;
    bool line = v % 300u < 3u;
    for (unsigned i = 0; i < params.pixels; ++i) {
        if (!(i & 255u)) app_yield();
        unsigned r = column_red[i], flags = column_flags[i];
        unsigned b = (flags ^ checker) & 1u ? 224u : 64u, green = g;
        if (line || flags & 2u) r = green = b = 0;
        if (params.color) { *out++ = (uint8_t)r; *out++ = (uint8_t)green; *out++ = (uint8_t)b; }
        else *out++ = (uint8_t)((r * 77u + green * 150u + b * 29u) >> 8);
    }
    if (++next_row == params.lines) running = false;
    return 1;
}

void scan_source_cancel(void) { running = false; }
