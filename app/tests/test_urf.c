/* Native unit test for the streaming URF decoder (urf.c).
 * Build: cc -Iapp/include -Ibootloader/include app/src/urf.c app/tests/test_urf.c -o test_urf */
#include "print_job.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <assert.h>

/* Independent encoder (oracle): runs of identical pixels, literals otherwise, 0x80 white tail. */
static unsigned put32(uint8_t *o, uint32_t v) { o[0] = v >> 24; o[1] = v >> 16; o[2] = v >> 8; o[3] = v; return 4; }
static unsigned encode_row(const uint8_t *px, unsigned w, unsigned bpp, uint8_t *o, unsigned repeat)
{
    unsigned n = 0, x = 0, c = bpp / 8;
    o[n++] = (uint8_t)(repeat - 1);
    while (x < w) {
        /* white tail? */
        unsigned k = x; while (k < w && !memcmp(px + k * c, "\xff\xff\xff", c)) ++k;
        if (k == w && x < w) { o[n++] = 0x80; return n; }
        unsigned run = 1; while (x + run < w && run < 128 && !memcmp(px + x * c, px + (x + run) * c, c)) ++run;
        if (run >= 2) { o[n++] = (uint8_t)(run - 1); memcpy(o + n, px + x * c, c); n += c; x += run; }
        else {
            unsigned start = x, lit = 0;
            while (x < w && lit < 127 && !(x + 1 < w && !memcmp(px + x * c, px + (x + 1) * c, c))) { ++x; ++lit; }
            o[n++] = (uint8_t)(257 - lit); memcpy(o + n, px + start * c, lit * c); n += lit * c;
        }
    }
    return n;
}

static uint8_t doc[1 << 20]; static unsigned doc_len;
static uint8_t pages[2][64 * 3 * 40]; static unsigned W[2] = {40, 37}, H[2] = {9, 6}, BPP[2] = {24, 8};

static void build_doc(void)
{
    unsigned n = 0;
    memcpy(doc, "UNIRAST\0", 8); n = 8; n += put32(doc + n, 2);
    for (unsigned p = 0; p < 2; ++p) {
        unsigned c = BPP[p] / 8;
        for (unsigned y = 0; y < H[p]; ++y) for (unsigned x = 0; x < W[p]; ++x) for (unsigned k = 0; k < c; ++k)
            pages[p][(y * W[p] + x) * c + k] = (y == 2) ? (uint8_t)(x * 7 + k * 50) : (x > 30 && y != 4) ? 0xff : (uint8_t)(y * 40 + k);
        doc[n++] = (uint8_t)BPP[p]; doc[n++] = c == 1 ? 0 : 1; doc[n++] = 1; doc[n++] = 4; n += put32(doc + n, 1); n += put32(doc + n, 0);
        n += put32(doc + n, W[p]); n += put32(doc + n, H[p]); n += put32(doc + n, 300); n += put32(doc + n, 0); n += put32(doc + n, 0);
        for (unsigned y = 0; y < H[p]; ) {
            unsigned rep = (y == 5 && y + 2 <= H[p]) ? 2 : 1;        /* rows 5 and 6 identical via repeat */
            if (rep == 2) memcpy(pages[p] + (y + 1) * W[p] * c, pages[p] + y * W[p] * c, W[p] * c);
            n += encode_row(pages[p] + y * W[p] * c, W[p], BPP[p], doc + n, rep); y += rep;
        }
    }
    doc_len = n;
}

static unsigned got_rows, got_pages, refuse_budget, ends; static int cur_page = -1;
static bool cb_page(const struct urf_page *p)
{
    if (refuse_budget) { --refuse_budget; return false; }
    assert(p->width == W[p->index - 1] && p->height == H[p->index - 1] && p->bpp == BPP[p->index - 1] && p->count == 2);
    cur_page = (int)p->index - 1; got_rows = 0; ++got_pages; return true;
}
static bool cb_row(const uint8_t *px, unsigned width, unsigned comps)
{
    if (refuse_budget) { --refuse_budget; return false; }
    unsigned c = BPP[cur_page] / 8;
    assert(width == W[cur_page] && comps == c);
    assert(!memcmp(px, pages[cur_page] + got_rows * width * c, width * c));
    ++got_rows; return true;
}
static void cb_end(void) { assert(got_rows == H[cur_page]); ++ends; }
static const struct urf_sink sink = {cb_page, cb_row, cb_end};

int main(void)
{
    build_doc();
    /* 1. whole document, big feed */
    struct urf_decoder d; urf_init(&d, &sink);
    unsigned n = urf_feed(&d, doc, doc_len);
    printf("debug: n=%u doc_len=%u done=%d error=%d pages=%u ends=%u rows=%u stage=%u fill=%u\n", n, doc_len, d.done, d.error, got_pages, ends, got_rows, d.stage, d.fill);
    assert(n == doc_len && d.done && !d.error && got_pages == 2 && ends == 2);
    printf("PASS whole document: %u bytes, 2 pages (RGB %ux%u, grey %ux%u)\n", doc_len, W[0], H[0], W[1], H[1]);
    /* 2. byte-at-a-time with refusals (backpressure re-offers) */
    got_pages = ends = 0; cur_page = -1; urf_init(&d, &sink);
    unsigned pos = 0, calls = 0, zero_returns = 0;
    refuse_budget = 7;
    while (pos < doc_len) {
        unsigned chunk = (calls % 5) + 1; if (pos + chunk > doc_len) chunk = doc_len - pos;
        unsigned took = urf_feed(&d, doc + pos, chunk); pos += took; ++calls;
        if (!took) { ++zero_returns; refuse_budget = 0; }
        assert(!d.error);
    }
    while (!urf_flush(&d)) {}
    assert(d.done && got_pages == 2 && ends == 2);
    printf("PASS streaming: %u feeds, %u paused, refusals re-offered, both pages identical\n", calls, zero_returns);
    /* 3. bad magic */
    urf_init(&d, &sink); uint8_t bad[12] = "UNIRASX\0\0\0\0\1";
    urf_feed(&d, bad, 12); assert(d.error);
    printf("PASS bad magic rejected\n");
    printf("ALL URF ASSERTIONS PASSED\n");
    return 0;
}
