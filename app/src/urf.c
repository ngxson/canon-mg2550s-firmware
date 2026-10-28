/* Apple URF ("UNIRAST") decoder, streaming.
 *
 * File: "UNIRAST\0", uint32 page count. Page: 32-byte header (bits per pixel,
 * colour space, duplex, quality, 2 reserved words, width, height, dots per
 * inch, 2 reserved words; integers big-endian), then rows: a repeat byte
 * (lines - 1), then codes until the row is full: 0x80 = the rest of the row
 * is white, c <= 0x7f = the next pixel repeated c + 1 times, c >= 0x81 =
 * 257 - c literal pixels. Pixels are bpp / 8 bytes: 8 = grey, 24 = sRGB. */
#include "print_job.h"

enum { S_FILE, S_PAGE, S_REPEAT, S_CODE, S_PIXEL, S_LITERAL, S_EMIT, S_DONE };

static uint32_t be32(const uint8_t *p) { return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3]; }

void urf_init(struct urf_decoder *d, const struct urf_sink *sink)
{
    memset(d, 0, sizeof(*d));
    d->sink = sink; d->stage = S_FILE; d->need = 12;
}

static bool offer_row(struct urf_decoder *d)
{
    while (d->repeat) {
        if (!d->sink->row(d->row, d->page.width, d->page.components, d->blank)) return false;
        --d->repeat;
    }
    return true;
}

/* All lines of the current encoded row have been delivered. */
static void row_complete(struct urf_decoder *d)
{
    d->stage = S_REPEAT;
    d->fill += d->row_lines;
    if (d->fill >= d->page.height) {                           /* page complete */
        d->fill = 0; d->sink->page_end();
        d->stage = d->page.index >= d->page.count ? S_DONE : S_PAGE;
        if (d->stage == S_DONE) d->done = true;
    }
}

/* Re-offer a page header or row the sink refused earlier. */
bool urf_flush(struct urf_decoder *d)
{
    if (d->error) return true;
    if (d->page_pending) {
        if (!d->sink->page(&d->page)) return false;
        d->page_pending = false;
    }
    if (d->row_pending) {
        if (!offer_row(d)) return false;
        d->row_pending = false;
        row_complete(d);
    }
    return true;
}

static void page_header(struct urf_decoder *d)
{
    const uint8_t *h = d->head;
    d->page.bpp = h[0]; d->page.width = be32(h + 12); d->page.height = be32(h + 16); d->page.dpi = be32(h + 20);
    d->page.components = d->page.bpp / 8u;
    ++d->page.index;
    if ((d->page.bpp != 8u && d->page.bpp != 24u) || !d->page.width || d->page.width > URF_MAX_WIDTH || !d->page.height) { d->error = true; return; }
    d->page_pending = !d->sink->page(&d->page);
    d->stage = S_REPEAT; d->x = 0;
}

unsigned urf_feed(struct urf_decoder *d, const uint8_t *data, unsigned len)
{
    unsigned i = 0;
    if (!urf_flush(d)) return 0;
    while (i < len && !d->error && d->stage != S_DONE) {
        uint8_t b = data[i];
        unsigned px = d->page.components;
        switch (d->stage) {
        case S_FILE:
            d->head[d->pos++] = b; ++i;
            if (d->pos == 12) {
                static const char magic[7] = {'U', 'N', 'I', 'R', 'A', 'S', 'T'};
                for (unsigned k = 0; k < 7; ++k) if (d->head[k] != (uint8_t)magic[k]) d->error = true;
                if (d->error) break;
                d->page.count = be32(d->head + 8); d->pos = 0; d->stage = S_PAGE;
            }
            break;
        case S_PAGE:
            d->head[d->pos++] = b; ++i;
            if (d->pos == 32) { d->pos = 0; page_header(d); if (d->page_pending) return i; }
            break;
        case S_REPEAT:
            d->repeat = d->row_lines = (unsigned)b + 1u; d->x = 0; d->blank = true; d->stage = S_CODE; ++i;
            break;
        case S_CODE:
            ++i;
            if (b == 0x80u) {                              /* white to the end of the row */
                memset(d->row + d->x * px, 0xff, (d->page.width - d->x) * px);
                d->x = d->page.width; d->stage = S_EMIT;
            } else if (b <= 0x7fu) { d->run = (unsigned)b + 1u; d->pos = 0; d->stage = S_PIXEL; }
            else { d->run = 257u - (unsigned)b; d->pos = 0; d->stage = S_LITERAL; }
            break;
        case S_PIXEL:
            d->pixel[d->pos++] = b; ++i;
            if (d->pos == px) {
                d->pos = 0;
                for (unsigned k = 0; k < px; ++k) if (d->pixel[k] != 0xffu) d->blank = false;
                for (; d->run && d->x < d->page.width; --d->run, ++d->x) memcpy(d->row + d->x * px, d->pixel, px);
                d->stage = d->x >= d->page.width ? S_EMIT : S_CODE;
            }
            break;
        case S_LITERAL:
            if (d->x < d->page.width) { d->row[d->x * px + d->pos] = b; if (b != 0xffu) d->blank = false; }
            ++i;
            if (++d->pos == px) { d->pos = 0; ++d->x; if (!--d->run) d->stage = d->x >= d->page.width ? S_EMIT : S_CODE; }
            break;
        default:
            break;
        }
        if (d->stage == S_EMIT) {
            if (!offer_row(d)) { d->row_pending = true; return i; }
            row_complete(d);
        }
    }
    if (d->error) return len;                              /* swallow the rest of a bad document */
    return i;
}
