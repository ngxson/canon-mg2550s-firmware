/* Streaming PNG writer for scan output.
 *
 * Each image row becomes one IDAT chunk holding one stored (uncompressed)
 * deflate block, so a row can be emitted as soon as the scanner delivers it
 * and nothing larger than a row is ever buffered. The zlib stream header goes
 * in the first IDAT and the Adler-32 trailer in the last; decoders treat the
 * concatenated IDAT data as one zlib stream. Compression can be added later
 * without changing the interface. */
#include "app.h"

/* CRC-32 (reflected 0xedb88320), a byte at a time with a 256-entry table
 * built on first use. */
static uint32_t crc_table[256];

static uint32_t crc32(const uint8_t *data, unsigned length)
{
    if (!crc_table[1])
        for (uint32_t n = 0; n < 256u; ++n) {
            if (!(n & 31u)) app_yield();
            uint32_t c = n;
            for (unsigned k = 0; k < 8u; ++k) c = c & 1u ? (c >> 1) ^ 0xedb88320u : c >> 1;
            crc_table[n] = c;
        }
    uint32_t crc = 0xffffffffu;
    for (unsigned i = 0; i < length; ++i) {
        if (!(i & 255u)) app_yield();
        crc = (crc >> 8) ^ crc_table[(crc ^ data[i]) & 255u];
    }
    return ~crc;
}

static uint32_t adler32(uint32_t adler, const uint8_t *data, unsigned length)
{
    uint32_t a = adler & 0xffffu, b = adler >> 16;
    while (length) {
        unsigned block = length < 5552u ? length : 5552u;  /* no 32-bit overflow */
        length -= block;
        app_yield();
        for (unsigned i = 0; i < block; ++i) {
            if (i && !(i & 255u)) app_yield();
            a += data[i]; b += a;
        }
        data += block;
        a %= 65521u; b %= 65521u;
    }
    return (b << 16) | a;
}

static void be32(uint8_t *out, uint32_t value)
{
    out[0] = (uint8_t)(value >> 24); out[1] = (uint8_t)(value >> 16);
    out[2] = (uint8_t)(value >> 8); out[3] = (uint8_t)value;
}

/* Wrap `length` bytes already at out + 8 as a chunk; returns its total size. */
static unsigned chunk(uint8_t *out, const char *type, unsigned length)
{
    be32(out, length);
    memcpy(out + 4, type, 4);
    be32(out + 8 + length, crc32(out + 4, length + 4u));
    return length + 12u;
}

unsigned png_begin(struct png_writer *png, unsigned width, unsigned height,
                   enum png_format format, unsigned dpi, uint8_t *out)
{
    static const uint8_t signature[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'};
    png->width = width; png->height = height; png->row = 0; png->adler = 1;
    png->row_bytes = format == PNG_BILEVEL ? (width + 7u) / 8u : width * (format == PNG_RGB ? 3u : 1u);
    memcpy(out, signature, 8);
    unsigned n = 8;
    uint8_t *ihdr = out + n + 8;
    be32(ihdr, width); be32(ihdr + 4, height);
    ihdr[8] = format == PNG_BILEVEL ? 1 : 8;              /* bit depth */
    ihdr[9] = format == PNG_RGB ? 2 : 0;                  /* truecolour or grey */
    ihdr[10] = ihdr[11] = ihdr[12] = 0;
    n += chunk(out + n, "IHDR", 13);
    uint8_t *phys = out + n + 8;
    uint32_t per_metre = (dpi * 10000u + 127u) / 254u;
    be32(phys, per_metre); be32(phys + 4, per_metre); phys[8] = 1;
    n += chunk(out + n, "pHYs", 9);
    return n;                                            /* PNG_HEADER_BYTES */
}

unsigned png_row(struct png_writer *png, const uint8_t *pixels, uint8_t *out)
{
    uint8_t *data = out + 8;
    unsigned k = 0, stored = png->row_bytes + 1u;        /* filter byte + pixels */
    bool last = png->row + 1u == png->height;
    if (!png->row) { data[k++] = 0x78; data[k++] = 0x01; }  /* zlib, 32 KiB window */
    data[k++] = last ? 1 : 0;                            /* BFINAL, BTYPE=stored */
    data[k++] = (uint8_t)stored; data[k++] = (uint8_t)(stored >> 8);
    data[k++] = (uint8_t)~stored; data[k++] = (uint8_t)(~stored >> 8);
    data[k] = 0;                                         /* filter: none */
    for (unsigned i = 0; i < png->row_bytes; i += 1024u) {
        unsigned piece = png->row_bytes - i < 1024u ? png->row_bytes - i : 1024u;
        memcpy(data + k + 1 + i, pixels + i, piece);
        app_yield();
    }
    png->adler = adler32(png->adler, data + k, stored);
    k += stored;
    if (last) { be32(data + k, png->adler); k += 4; }
    ++png->row;
    unsigned n = chunk(out, "IDAT", k);
    if (last) n += chunk(out + n, "IEND", 0);
    return n;
}
