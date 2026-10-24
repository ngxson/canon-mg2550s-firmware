/* Open Canon BJL / BJRaster3 print-stream receiver (parser + PackBits decoder).
 *
 * Portable C (no firmware deps) so it can be unit-tested on the host and reused.
 * Parses the ESC ( / ESC [ command framing, tracks the 8 mandatory setup
 * commands (I J b c d l p t -> 0x1806c9c4 == 0xff in stock), and decodes the
 * `F` raster packets (PackBits rows per plane, in the `L` plane order) into
 * per-plane row buffers. Matches the host encoder in hobby-printer-esp32
 * (components/canon_bjl) and the firmware receiver (fw_dump/gpt/print_re_codex.md). */
#ifndef PRINT_H
#define PRINT_H
#include <stdint.h>

#define PRINT_MAX_ROW 2048u
#define PRINT_MAX_PLANES 4u
#define PRINT_MANDATORY 0xffu   /* I|J|b|c|d|l|p|t */

typedef struct {
    unsigned mandatory;           /* OR of the setup-command bits seen */
    unsigned rows_per_plane;      /* ESC(J: 1, 8, or 16 */
    unsigned compression;         /* ESC(b: 0 or 1 */
    unsigned num_planes;          /* ESC(L length */
    unsigned rows_decoded;
    unsigned errors, page_ends, job_ends;
    uint8_t planes[PRINT_MAX_PLANES];              /* ESC(L selector bytes, e.g. 'C','M','Y','K' */
    uint8_t row[PRINT_MAX_PLANES][PRINT_MAX_ROW];  /* last decoded row per plane */
    unsigned row_bytes[PRINT_MAX_PLANES];
} print_state_t;

void print_reset(print_state_t *st);
/* Parse a complete stream (one or more bands). Returns 0 on clean parse. */
int print_decode(const uint8_t *stream, unsigned len, print_state_t *st);
int print_mandatory_ok(const print_state_t *st);

#endif
