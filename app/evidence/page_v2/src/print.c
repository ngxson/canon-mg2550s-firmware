#include "print.h"

/* Decode one PackBits row up to its 0x80 terminator (Canon variant: control c<0x80
 * = (c+1) literals; c>0x80 = next byte repeated (257-c) times; c==0x80 = end of row).
 * Returns decoded byte count; *consumed = input bytes used incl. the terminator. */
static unsigned packbits_row(const uint8_t *in, unsigned in_len, uint8_t *out,
                             unsigned out_cap, unsigned *consumed)
{
    unsigned i = 0, o = 0;
    while (i < in_len) {
        uint8_t c = in[i++];
        if (c == 0x80) { *consumed = i; return o; }        /* row terminator */
        if (c < 0x80) {
            unsigned k = (unsigned)c + 1;
            while (k-- && i < in_len) { if (o < out_cap) out[o] = in[i]; ++o; ++i; }
        } else {
            unsigned k = 257u - c;
            uint8_t v = (i < in_len) ? in[i++] : 0;
            while (k--) { if (o < out_cap) out[o] = v; ++o; }
        }
    }
    *consumed = i;
    return o;
}

void print_reset(print_state_t *st)
{
    uint8_t *p = (uint8_t *)st;
    for (unsigned i = 0; i < sizeof(*st); ++i) p[i] = 0;
}

int print_decode(const uint8_t *s, unsigned n, print_state_t *st)
{
    unsigned i = 0, plane = 0;
    while (i < n) {
        uint8_t b = s[i];
        if (b == 0x0c) { ++i; ++st->page_ends; plane = 0; continue; }   /* form feed: end page */
        if (b != 0x1b) { ++i; ++st->errors; continue; }                 /* expect ESC */
        if (i + 1 >= n) break;
        uint8_t fr = s[i + 1];
        if (fr == 0x40) { i += 2; ++st->job_ends; continue; }           /* ESC @: end job */
        if (fr != 0x28 && fr != 0x5b) { i += 2; ++st->errors; continue; }
        if (i + 5 > n) { ++st->errors; break; }
        uint8_t cmd = s[i + 2];
        unsigned len = (unsigned)s[i + 3] | ((unsigned)s[i + 4] << 8);
        const uint8_t *p = s + i + 5;
        if (i + 5 + len > n) { ++st->errors; break; }
        i += 5 + len;
        if (fr == 0x5b) continue;                                       /* ESC [ (reset/boundary) */

        switch (cmd) {                                                  /* ESC ( <cmd> */
        case 'I': if (len >= 1 && p[0] == 0x01) st->mandatory |= 0x01; break;
        case 'J': if (len >= 1) { st->rows_per_plane = p[0]; st->mandatory |= 0x02; } break;
        case 'b': if (len >= 1) { st->compression = p[0]; st->mandatory |= 0x04; } break;
        case 'c': st->mandatory |= 0x08; break;
        case 'd': st->mandatory |= 0x10; break;
        case 'l': st->mandatory |= 0x20; break;
        case 'p': st->mandatory |= 0x40; break;
        case 't': st->mandatory |= 0x80; break;
        case 'L':
            st->num_planes = len > PRINT_MAX_PLANES ? PRINT_MAX_PLANES : len;
            for (unsigned k = 0; k < st->num_planes; ++k) st->planes[k] = p[k];
            plane = 0;
            break;
        case 'F': {                                                     /* raster: PackBits rows for next plane */
            unsigned which = plane < PRINT_MAX_PLANES ? plane : 0;       /* plane is kept wrapped below (no modulo) */
            unsigned off = 0, rows = 0, want = st->rows_per_plane ? st->rows_per_plane : 1;
            while (off < len && rows < want) {
                unsigned used = 0;
                st->row_bytes[which] = packbits_row(p + off, len - off,
                                                    st->row[which], PRINT_MAX_ROW, &used);
                if (!used) break;
                off += used; ++rows; ++st->rows_decoded;
            }
            if (st->num_planes) { ++plane; if (plane >= st->num_planes) plane = 0; }
            break;
        }
        default: break;
        }
    }
    return st->errors ? -1 : 0;
}

int print_mandatory_ok(const print_state_t *st)
{
    return st->mandatory == PRINT_MANDATORY;
}
