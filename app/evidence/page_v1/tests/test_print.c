/* Native unit test for the portable print receiver (print.c). Builds a minimal
 * valid one-band BJL/BJRaster3 stream and checks the parse + PackBits decode.
 * Build: cc -Iapp/include app/src/print.c app/tests/test_print.c -o test_print */
#include <stdio.h>
#include <string.h>
#include <assert.h>
#include "print.h"

static unsigned emit_cmd(uint8_t *b, unsigned o, char cmd, const uint8_t *pl, unsigned len)
{
    b[o++] = 0x1b; b[o++] = '('; b[o++] = (uint8_t)cmd; b[o++] = len & 0xff; b[o++] = len >> 8;
    if (len) { memcpy(b + o, pl, len); o += len; }
    return o;
}

/* Independent PackBits encoder (the test oracle; distinct code from print.c's
 * decoder). Emits Canon-variant ops: literal run -> (count-1) + bytes; identical
 * run >=2 -> (257-run) + byte; row ends with the 0x80 terminator. */
static unsigned pb_encode(const uint8_t *in, unsigned n, uint8_t *out)
{
    unsigned i = 0, o = 0;
    while (i < n) {
        unsigned run = 1;
        while (i + run < n && in[i + run] == in[i] && run < 128) ++run;
        if (run >= 2) {
            out[o++] = (uint8_t)(257 - run);
            out[o++] = in[i];
            i += run;
        } else {
            unsigned start = i, lit = 0;
            while (i < n && lit < 128 && !(i + 1 < n && in[i] == in[i + 1])) { ++i; ++lit; }
            out[o++] = (uint8_t)(lit - 1);
            memcpy(out + o, in + start, lit); o += lit;
        }
    }
    out[o++] = 0x80;
    return o;
}

/* Append an ESC(F carrying `nrows` PackBits-encoded rows for one plane. */
static unsigned emit_plane(uint8_t *b, unsigned o,
                           const uint8_t rows[][256], const unsigned rlen[], unsigned nrows)
{
    uint8_t enc[1024];
    unsigned e = 0;
    for (unsigned r = 0; r < nrows; ++r) e += pb_encode(rows[r], rlen[r], enc + e);
    return emit_cmd(b, o, 'F', enc, e);
}

/* Realistic page: 4 planes (CMYK), 2 rows/plane, with long identical runs and a
 * >64-byte literal run (so the decode crosses literal/repeat op boundaries and,
 * over USB, bulk packet boundaries). Asserts plane order, row accounting, and
 * that each plane's retained (last) row round-trips exactly. */
static void realistic_cmyk(void)
{
    /* Build raw rows. [plane][row][byte]; row 1 is the one print.c retains. */
    static uint8_t raw[4][2][256];
    static unsigned rlen[4][2];
    /* C: long run + literals; last row distinctive */
    for (unsigned i = 0; i < 150; ++i) raw[0][0][i] = 0xC0;
    raw[0][0][150] = 0x01; raw[0][0][151] = 0x02; raw[0][0][152] = 0x03;
    for (unsigned i = 0; i < 80; ++i) raw[0][0][153 + i] = 0x33;
    rlen[0][0] = 233;
    { uint8_t r[] = {0xCC,0xCC,0xCC,0xCC,0xCC,0x10,0x20,0x30,0,0,0,0,0,0,0,0,0,0,0,0}; memcpy(raw[0][1], r, sizeof r); rlen[0][1] = sizeof r; }
    /* M: 200-byte run (two repeat ops) then last row */
    for (unsigned i = 0; i < 200; ++i) raw[1][0][i] = 0xAA; rlen[1][0] = 200;
    { uint8_t r[] = {0x4D,0x4D,0x5A,0x5A,0x5A,0x5A,0x5A,0x5A,0x5A,0x5A,0x5A,0x5A,0x01}; memcpy(raw[1][1], r, sizeof r); rlen[1][1] = sizeof r; }
    /* Y: 100-byte ramp (one >64 literal run) then last row */
    for (unsigned i = 0; i < 100; ++i) raw[2][0][i] = (uint8_t)i; rlen[2][0] = 100;
    { uint8_t r[] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF}; memcpy(raw[2][1], r, sizeof r); rlen[2][1] = sizeof r; }
    /* K: 0x80 as *data* (3x) then last row with values around the terminator */
    raw[3][0][0] = 0x80; raw[3][0][1] = 0x80; raw[3][0][2] = 0x80; rlen[3][0] = 3;
    { uint8_t r[] = {0,0,0,0,0x7F,0x80,0x81,0xFF,0xFF,0xFF,0xFF}; memcpy(raw[3][1], r, sizeof r); rlen[3][1] = sizeof r; }

    uint8_t s[4096];
    unsigned o = 0;
    const uint8_t one[1] = {0x01};
    const uint8_t two[1] = {0x02};
    o = emit_cmd(s, o, 'I', one, 1);
    o = emit_cmd(s, o, 'J', two, 1);                            /* 2 rows per plane */
    o = emit_cmd(s, o, 'b', one, 1);
    o = emit_cmd(s, o, 'c', (const uint8_t[]){0x30, 0, 0}, 3);
    o = emit_cmd(s, o, 'd', (const uint8_t[]){0x02, 0x58, 0x02, 0x58}, 4);
    o = emit_cmd(s, o, 'l', (const uint8_t[]){0x34, 0, 0}, 3);
    uint8_t p46[46] = {0}, t42[42] = {0};
    o = emit_cmd(s, o, 'p', p46, 46);
    o = emit_cmd(s, o, 't', t42, 42);
    o = emit_cmd(s, o, 'L', (const uint8_t *)"CMYK", 4);        /* four planes */
    for (unsigned pl = 0; pl < 4; ++pl) {
        const uint8_t (*pr)[256] = raw[pl];
        o = emit_plane(s, o, pr, rlen[pl], 2);
    }
    s[o++] = 0x0c;                                             /* form feed: end page */
    s[o++] = 0x1b; s[o++] = 0x40;                              /* ESC @ end job */

    print_state_t st;
    print_reset(&st);
    int r = print_decode(s, o, &st);
    assert(r == 0);
    assert(print_mandatory_ok(&st));
    assert(st.num_planes == 4);
    assert(memcmp(st.planes, "CMYK", 4) == 0);
    assert(st.rows_decoded == 8);          /* 4 planes x 2 rows */
    assert(st.rows_per_plane == 2);
    assert(st.page_ends == 1 && st.job_ends == 1);
    assert(st.errors == 0);
    /* Each plane retains its LAST row; it must round-trip exactly. */
    for (unsigned pl = 0; pl < 4; ++pl) {
        assert(st.row_bytes[pl] == rlen[pl][1]);
        assert(memcmp(st.row[pl], raw[pl][1], rlen[pl][1]) == 0);
    }
    printf("PASS realistic_cmyk: planes=CMYK rows=%u streambytes=%u lastrows=[%u,%u,%u,%u]\n",
           st.rows_decoded, o, st.row_bytes[0], st.row_bytes[1], st.row_bytes[2], st.row_bytes[3]);
}

int main(void)
{
    uint8_t s[512];
    unsigned o = 0;
    const uint8_t one[1] = {0x01};
    o = emit_cmd(s, o, 'I', one, 1);                           /* multiraster */
    o = emit_cmd(s, o, 'J', one, 1);                           /* 1 row per plane */
    o = emit_cmd(s, o, 'b', one, 1);                           /* compression on */
    o = emit_cmd(s, o, 'c', (const uint8_t[]){0x30, 0, 0}, 3); /* colour/plain/quality */
    o = emit_cmd(s, o, 'd', (const uint8_t[]){0x02, 0x58, 0x02, 0x58}, 4); /* 600x600 dpi */
    o = emit_cmd(s, o, 'l', (const uint8_t[]){0x34, 0, 0}, 3); /* rear tray / plain */
    uint8_t p46[46] = {0}, t42[42] = {0};
    o = emit_cmd(s, o, 'p', p46, 46);                          /* page geometry */
    o = emit_cmd(s, o, 't', t42, 42);                          /* ink descriptor */
    o = emit_cmd(s, o, 'L', (const uint8_t *)"K", 1);          /* one plane: black */
    /* raster: 5x 0xFF then 5x 0x00, PackBits -> FC FF  FC 00  80(terminator) */
    o = emit_cmd(s, o, 'F', (const uint8_t[]){0xfc, 0xff, 0xfc, 0x00, 0x80}, 5);
    s[o++] = 0x1b; s[o++] = 0x40;                              /* ESC @ end job */

    print_state_t st;
    print_reset(&st);
    int r = print_decode(s, o, &st);
    assert(r == 0);
    assert(print_mandatory_ok(&st));
    assert(st.num_planes == 1 && st.planes[0] == 'K');
    assert(st.rows_decoded == 1 && st.job_ends == 1);
    assert(st.row_bytes[0] == 10);
    const uint8_t exp[10] = {0xff, 0xff, 0xff, 0xff, 0xff, 0, 0, 0, 0, 0};
    assert(memcmp(st.row[0], exp, 10) == 0);

    printf("PASS print_decode: mandatory=0x%02x planes=%u rows=%u K-row(%u)=[",
           st.mandatory, st.num_planes, st.rows_decoded, st.row_bytes[0]);
    for (int i = 0; i < 10; i++) printf("%02x", st.row[0][i]);
    printf("]\n");

    realistic_cmyk();

    printf("ALL PRINT RECEIVER (native) ASSERTIONS PASSED\n");
    return 0;
}
