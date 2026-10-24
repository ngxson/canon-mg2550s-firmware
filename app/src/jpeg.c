/* Streaming baseline JPEG writer (JFIF) for scan output.
 *
 * Rows are collected into one 8-row strip, then encoded MCU by MCU, so output
 * can be produced into whatever space the USB transmit buffer has and the
 * page is never held in memory. Greyscale or YCbCr colour with no chroma
 * subsampling (one 8x8 block per component per MCU), the Annex K example
 * quantisation tables scaled for the requested quality, and the Annex K
 * Huffman tables. The ARM946E-S has no FPU, so the forward DCT is the
 * classic accurate integer (Loeffler-Ligtenberg-Moschytz) transform. */
#include "app.h"

static const uint8_t zigzag[64] = {
     0,  1,  8, 16,  9,  2,  3, 10, 17, 24, 32, 25, 18, 11,  4,  5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13,  6,  7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
};

/* Annex K.1 example tables, natural (row-major) order. */
static const uint8_t base_quant[2][64] = {
    {16, 11, 10, 16, 24, 40, 51, 61,   12, 12, 14, 19, 26, 58, 60, 55,
     14, 13, 16, 24, 40, 57, 69, 56,   14, 17, 22, 29, 51, 87, 80, 62,
     18, 22, 37, 56, 68,109,103, 77,   24, 35, 55, 64, 81,104,113, 92,
     49, 64, 78, 87,103,121,120,101,   72, 92, 95, 98,112,100,103, 99},
    {17, 18, 24, 47, 99, 99, 99, 99,   18, 21, 26, 66, 99, 99, 99, 99,
     24, 26, 56, 99, 99, 99, 99, 99,   47, 66, 99, 99, 99, 99, 99, 99,
     99, 99, 99, 99, 99, 99, 99, 99,   99, 99, 99, 99, 99, 99, 99, 99,
     99, 99, 99, 99, 99, 99, 99, 99,   99, 99, 99, 99, 99, 99, 99, 99},
};

/* Annex K.3 Huffman tables: code counts per length 1..16, then symbols. */
static const uint8_t dc_bits[2][16] = {
    {0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0},
    {0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0},
};
static const uint8_t dc_vals[12] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11};
static const uint8_t ac_bits[2][16] = {
    {0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7d},
    {0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77},
};
static const uint8_t ac_vals[2][162] = {
    {0x01,0x02,0x03,0x00,0x04,0x11,0x05,0x12,0x21,0x31,0x41,0x06,0x13,0x51,0x61,0x07,
     0x22,0x71,0x14,0x32,0x81,0x91,0xa1,0x08,0x23,0x42,0xb1,0xc1,0x15,0x52,0xd1,0xf0,
     0x24,0x33,0x62,0x72,0x82,0x09,0x0a,0x16,0x17,0x18,0x19,0x1a,0x25,0x26,0x27,0x28,
     0x29,0x2a,0x34,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,0x45,0x46,0x47,0x48,0x49,
     0x4a,0x53,0x54,0x55,0x56,0x57,0x58,0x59,0x5a,0x63,0x64,0x65,0x66,0x67,0x68,0x69,
     0x6a,0x73,0x74,0x75,0x76,0x77,0x78,0x79,0x7a,0x83,0x84,0x85,0x86,0x87,0x88,0x89,
     0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0xa2,0xa3,0xa4,0xa5,0xa6,0xa7,
     0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,0xb5,0xb6,0xb7,0xb8,0xb9,0xba,0xc2,0xc3,0xc4,0xc5,
     0xc6,0xc7,0xc8,0xc9,0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,0xe1,0xe2,
     0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf1,0xf2,0xf3,0xf4,0xf5,0xf6,0xf7,0xf8,
     0xf9,0xfa},
    {0x00,0x01,0x02,0x03,0x11,0x04,0x05,0x21,0x31,0x06,0x12,0x41,0x51,0x07,0x61,0x71,
     0x13,0x22,0x32,0x81,0x08,0x14,0x42,0x91,0xa1,0xb1,0xc1,0x09,0x23,0x33,0x52,0xf0,
     0x15,0x62,0x72,0xd1,0x0a,0x16,0x24,0x34,0xe1,0x25,0xf1,0x17,0x18,0x19,0x1a,0x26,
     0x27,0x28,0x29,0x2a,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,0x45,0x46,0x47,0x48,
     0x49,0x4a,0x53,0x54,0x55,0x56,0x57,0x58,0x59,0x5a,0x63,0x64,0x65,0x66,0x67,0x68,
     0x69,0x6a,0x73,0x74,0x75,0x76,0x77,0x78,0x79,0x7a,0x82,0x83,0x84,0x85,0x86,0x87,
     0x88,0x89,0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0xa2,0xa3,0xa4,0xa5,
     0xa6,0xa7,0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,0xb5,0xb6,0xb7,0xb8,0xb9,0xba,0xc2,0xc3,
     0xc4,0xc5,0xc6,0xc7,0xc8,0xc9,0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,
     0xe2,0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf2,0xf3,0xf4,0xf5,0xf6,0xf7,0xf8,
     0xf9,0xfa},
};

struct huffman { uint16_t code[256]; uint8_t size[256]; };
static struct huffman dc_huff[2], ac_huff[2];
static uint8_t quant[2][64];            /* scaled tables, natural order */
static uint16_t reciprocal[2][64];      /* 65536 / (8 * quant), rounded */
static uint8_t strip[8][SCAN_MAX_PIXELS * 3u];

/* Annex C: canonical codes from the code counts. */
static void build_huffman(struct huffman *h, const uint8_t bits[16], const uint8_t *vals)
{
    unsigned k = 0, code = 0;
    for (unsigned length = 1; length <= 16u; ++length, code <<= 1)
        for (unsigned i = 0; i < bits[length - 1u]; ++i, ++k, ++code) {
            h->code[vals[k]] = (uint16_t)code;
            h->size[vals[k]] = (uint8_t)length;
        }
}

/* Output cursor plus the bit accumulator carried across calls. */
struct sink { struct jpeg_writer *j; uint8_t *out; unsigned n; };

static void put_byte(struct sink *s, uint8_t byte) { s->out[s->n++] = byte; }

static __attribute__((optimize("O2"))) void put_bits(struct sink *s, uint32_t code, unsigned size)
{
    struct jpeg_writer *j = s->j;
    j->acc = (j->acc << size) | (code & ((1u << size) - 1u));
    j->count += size;
    while (j->count >= 8u) {
        uint8_t byte = (uint8_t)(j->acc >> (j->count - 8u));
        put_byte(s, byte);
        if (byte == 0xff) put_byte(s, 0);               /* byte stuffing */
        j->count -= 8u;
    }
}

static unsigned magnitude_bits(unsigned value)
{
    unsigned bits = 0;
    while (value) { ++bits; value >>= 1; }
    return bits;
}

/* Huffman-code one value: category from the table, then its extra bits. */
static __attribute__((optimize("O2"))) void put_value(struct sink *s, const struct huffman *h, unsigned symbol_base, int value)
{
    unsigned magnitude = (unsigned)(value < 0 ? -value : value), bits = magnitude_bits(magnitude);
    unsigned symbol = symbol_base | bits;
    put_bits(s, h->code[symbol], h->size[symbol]);
    if (bits) put_bits(s, (uint32_t)(value < 0 ? value - 1 : value), bits);
}

#define CONST_BITS 13
#define PASS1_BITS 2
#define DESCALE(x, n) (((x) + (1 << ((n) - 1))) >> (n))

/* One 8-point LLM pass over a row (stride 1) or column (stride 8); `last`
 * selects the second pass's descaling. Always inlined with constant
 * arguments so the -Os build does not compute strided indexes at run time. */
static inline __attribute__((always_inline)) void dct_pass(int *d, const int stride, const int last)
{
    const int shift = last ? CONST_BITS + PASS1_BITS : CONST_BITS - PASS1_BITS;
    int tmp0 = d[0] + d[7 * stride], tmp7 = d[0] - d[7 * stride];
    int tmp1 = d[stride] + d[6 * stride], tmp6 = d[stride] - d[6 * stride];
    int tmp2 = d[2 * stride] + d[5 * stride], tmp5 = d[2 * stride] - d[5 * stride];
    int tmp3 = d[3 * stride] + d[4 * stride], tmp4 = d[3 * stride] - d[4 * stride];
    int tmp10 = tmp0 + tmp3, tmp13 = tmp0 - tmp3;
    int tmp11 = tmp1 + tmp2, tmp12 = tmp1 - tmp2;
    if (last) {
        d[0] = DESCALE(tmp10 + tmp11, PASS1_BITS);
        d[4 * stride] = DESCALE(tmp10 - tmp11, PASS1_BITS);
    } else {
        d[0] = (tmp10 + tmp11) * (1 << PASS1_BITS);
        d[4 * stride] = (tmp10 - tmp11) * (1 << PASS1_BITS);
    }
    int z1 = (tmp12 + tmp13) * 4433;                     /* 0.541196100 */
    d[2 * stride] = DESCALE(z1 + tmp13 * 6270, shift);    /* 0.765366865 */
    d[6 * stride] = DESCALE(z1 - tmp12 * 15137, shift);   /* 1.847759065 */
    int z3 = tmp4 + tmp6, z4 = tmp5 + tmp7, z5 = (z3 + z4) * 9633;   /* 1.175875602 */
    int z1o = (tmp4 + tmp7) * -7373, z2 = (tmp5 + tmp6) * -20995;     /* 0.899976223, 2.562915447 */
    z3 = z3 * -16069 + z5; z4 = z4 * -3196 + z5;                      /* 1.961570560, 0.390180644 */
    d[7 * stride] = DESCALE(tmp4 * 2446 + z1o + z3, shift);  /* 0.298631336 */
    d[5 * stride] = DESCALE(tmp5 * 16819 + z2 + z4, shift);  /* 2.053119869 */
    d[3 * stride] = DESCALE(tmp6 * 25172 + z2 + z3, shift);  /* 3.072711026 */
    d[stride] = DESCALE(tmp7 * 12299 + z1o + z4, shift);     /* 1.501321110 */
}

/* Forward DCT of 64 level-shifted samples in place; results are 8x the
 * orthonormal DCT, which the quantiser divides out. */
static __attribute__((optimize("O2"))) void forward_dct(int *data)
{
    for (int i = 0; i < 8; ++i) dct_pass(data + 8 * i, 1, 0);
    for (int i = 0; i < 8; ++i) dct_pass(data + i, 8, 1);
}

static inline __attribute__((always_inline)) unsigned quantize(int v, unsigned table, unsigned at)
{
    unsigned q = (unsigned)(v < 0 ? -v : v);
    return ((q + 4u * quant[table][at]) * reciprocal[table][at]) >> 16;
}

static __attribute__((optimize("O2"))) void encode_block(struct sink *s, int *block, unsigned table, int *dc)
{
    int coef[64];
    /* A uniform block transforms exactly to DC = 64 * value with every AC
     * coefficient zero (the integer passes round it exactly). Mostly-white
     * documents are dominated by such blocks. */
    bool flat = true;
    for (unsigned k = 1; k < 64u && flat; ++k) flat = block[k] == block[0];
    if (flat) {
        int v = block[0] * 64;
        unsigned q = quantize(v, table, 0);
        put_value(s, &dc_huff[table], 0, (v < 0 ? -(int)q : (int)q) - *dc);
        *dc = v < 0 ? -(int)q : (int)q;
        put_bits(s, ac_huff[table].code[0], ac_huff[table].size[0]);   /* EOB */
        return;
    }
    forward_dct(block);
    app_yield();
    for (unsigned k = 0; k < 64u; ++k) {
        if (!(k & 15u)) app_yield();
        unsigned at = zigzag[k];
        int v = block[at];
        unsigned q = quantize(v, table, at);
        coef[k] = v < 0 ? -(int)q : (int)q;
    }
    put_value(s, &dc_huff[table], 0, coef[0] - *dc);
    *dc = coef[0];
    unsigned run = 0;
    for (unsigned k = 1; k < 64u; ++k) {
        if (!(k & 15u)) app_yield();
        if (!coef[k]) { ++run; continue; }
        for (; run > 15u; run -= 16u) put_bits(s, ac_huff[table].code[0xf0], ac_huff[table].size[0xf0]);
        put_value(s, &ac_huff[table], run << 4, coef[k]);
        run = 0;
    }
    if (run) put_bits(s, ac_huff[table].code[0], ac_huff[table].size[0]);   /* EOB */
}

static void put16(struct sink *s, unsigned value) { put_byte(s, (uint8_t)(value >> 8)); put_byte(s, (uint8_t)value); }

static void put_huffman_table(struct sink *s, unsigned id, const uint8_t bits[16], const uint8_t *vals)
{
    unsigned count = 0;
    for (unsigned i = 0; i < 16u; ++i) count += bits[i];
    put_byte(s, 0xff); put_byte(s, 0xc4); put16(s, 2u + 1u + 16u + count); put_byte(s, (uint8_t)id);
    for (unsigned i = 0; i < 16u; ++i) put_byte(s, bits[i]);
    for (unsigned i = 0; i < count; ++i) put_byte(s, vals[i]);
}

unsigned jpeg_begin(struct jpeg_writer *j, unsigned width, unsigned height, bool color,
                    unsigned dpi, unsigned quality, uint8_t *out)
{
    *j = (struct jpeg_writer){.width = width, .height = height, .components = color ? 3u : 1u};
    unsigned scale = quality < 50u ? 5000u / quality : 200u - 2u * quality;
    for (unsigned t = 0; t < 2u; ++t) {
        for (unsigned k = 0; k < 64u; ++k) {
            if (!(k & 7u)) app_yield();               /* software divisions */
            unsigned q = (base_quant[t][k] * scale + 50u) / 100u;
            quant[t][k] = (uint8_t)(q < 1u ? 1u : q > 255u ? 255u : q);
            reciprocal[t][k] = (uint16_t)((65536u + 4u * quant[t][k]) / (8u * quant[t][k]));
        }
        build_huffman(&dc_huff[t], dc_bits[t], dc_vals);
        build_huffman(&ac_huff[t], ac_bits[t], ac_vals[t]);
    }
    unsigned tables = color ? 2u : 1u;
    struct sink s = {j, out, 0};
    put_byte(&s, 0xff); put_byte(&s, 0xd8);                       /* SOI */
    static const uint8_t jfif[] = {0xff, 0xe0, 0, 16, 'J', 'F', 'I', 'F', 0, 1, 1, 1};
    for (unsigned i = 0; i < sizeof(jfif); ++i) put_byte(&s, jfif[i]);
    put16(&s, dpi); put16(&s, dpi); put_byte(&s, 0); put_byte(&s, 0);   /* density, no thumbnail */
    for (unsigned t = 0; t < tables; ++t) {                         /* DQT */
        put_byte(&s, 0xff); put_byte(&s, 0xdb); put16(&s, 67); put_byte(&s, (uint8_t)t);
        for (unsigned k = 0; k < 64u; ++k) put_byte(&s, quant[t][zigzag[k]]);
    }
    put_byte(&s, 0xff); put_byte(&s, 0xc0); put16(&s, 8u + 3u * j->components);   /* SOF0 */
    put_byte(&s, 8); put16(&s, height); put16(&s, width); put_byte(&s, (uint8_t)j->components);
    for (unsigned c = 0; c < j->components; ++c) {
        put_byte(&s, (uint8_t)(c + 1u)); put_byte(&s, 0x11); put_byte(&s, c ? 1 : 0);
    }
    for (unsigned t = 0; t < tables; ++t) {                         /* DHT */
        put_huffman_table(&s, t, dc_bits[t], dc_vals);
        put_huffman_table(&s, 0x10u | t, ac_bits[t], ac_vals[t]);
    }
    put_byte(&s, 0xff); put_byte(&s, 0xda); put16(&s, 6u + 2u * j->components);   /* SOS */
    put_byte(&s, (uint8_t)j->components);
    for (unsigned c = 0; c < j->components; ++c) {
        put_byte(&s, (uint8_t)(c + 1u)); put_byte(&s, c ? 0x11 : 0x00);
    }
    put_byte(&s, 0); put_byte(&s, 63); put_byte(&s, 0);
    return s.n;
}

uint8_t *jpeg_row_buffer(struct jpeg_writer *j)
{
    bool strip_full = j->strip_rows == 8u || j->top + j->strip_rows == j->height;
    return strip_full ? NULL : strip[j->strip_rows];
}

void jpeg_row_written(struct jpeg_writer *j) { ++j->strip_rows; }

/* One MCU at column `mcu` of the buffered strip. Edges replicate the last
 * row and column, as the JPEG image is cropped to width x height. */
static __attribute__((optimize("O2"))) void encode_mcu(struct sink *s)
{
    struct jpeg_writer *j = s->j;
    int blocks[3][64];
    app_section = "jpeg";
    if (j->components == 1u && j->strip_rows == 8u && j->mcu * 8u + 8u <= j->width) {
        for (unsigned y = 0; y < 8u; ++y) {              /* interior grey block */
            const uint8_t *row = strip[y] + j->mcu * 8u;
            int *b = blocks[0] + y * 8u;
            for (unsigned x = 0; x < 8u; ++x) b[x] = row[x] - 128;
        }
        app_yield();
        encode_block(s, blocks[0], 0, &j->dc[0]);
        return;
    }
    for (unsigned y = 0; y < 8u; ++y) {
        app_yield();
        const uint8_t *row = strip[y < j->strip_rows ? y : j->strip_rows - 1u];
        for (unsigned x = 0; x < 8u; ++x) {
            unsigned column = j->mcu * 8u + x, i = y * 8u + x;
            if (column >= j->width) column = j->width - 1u;
            if (j->components == 1u) { blocks[0][i] = row[column] - 128; continue; }
            int r = row[3u * column], g = row[3u * column + 1u], b = row[3u * column + 2u];
            blocks[0][i] = ((77 * r + 150 * g + 29 * b + 128) >> 8) - 128;
            blocks[1][i] = (-43 * r - 85 * g + 128 * b + 128) >> 8;
            blocks[2][i] = (128 * r - 107 * g - 21 * b + 128) >> 8;
        }
    }
    for (unsigned c = 0; c < j->components; ++c) {
        app_yield();
        encode_block(s, blocks[c], c ? 1u : 0u, &j->dc[c]);
    }
}

unsigned jpeg_encode(struct jpeg_writer *j, uint8_t *out, unsigned max)
{
    struct sink s = {j, out, 0};
    bool strip_full = j->strip_rows == 8u || (j->strip_rows && j->top + j->strip_rows == j->height);
    if (!strip_full || j->finished) return 0;
    unsigned mcus = (j->width + 7u) / 8u;
    uint32_t started = time_ms();                 /* a few MCUs per call; see escl.c */
    while (j->mcu < mcus && max - s.n >= JPEG_MCU_MAX && time_ms() - started < 2u) { encode_mcu(&s); ++j->mcu; }
    if (j->mcu < mcus) return s.n;
    j->top += j->strip_rows; j->strip_rows = 0; j->mcu = 0;
    if (j->top == j->height) {
        if (j->count) put_bits(&s, 0x7f, 8u - j->count);          /* pad with 1 bits */
        put_byte(&s, 0xff); put_byte(&s, 0xd9);                   /* EOI */
        j->finished = true;
    }
    return s.n;
}
