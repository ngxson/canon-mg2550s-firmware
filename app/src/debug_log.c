/* A small byte ring buffer drained over USB (see app.h, APP_REQUEST_LOG).
 * Writers append; the USB control handler pops. On overflow the oldest byte
 * is dropped so recent activity always survives. head/tail are free-running
 * counters masked to the buffer; their unsigned difference is the fill level. */
#include "app.h"

#define LOG_SIZE 32768u
#define LOG_MASK (LOG_SIZE - 1u)
static uint8_t buffer[LOG_SIZE];
static uint32_t head, tail;

void dbg_init(void) { head = tail = 0; }

void dbg_putc(char c)
{
    if (head - tail >= LOG_SIZE) tail++;      /* full: drop the oldest byte */
    buffer[head & LOG_MASK] = (uint8_t)c;
    head++;
}

void dbg_write(const void *data, unsigned length)
{
    const uint8_t *p = data;
    while (length--) dbg_putc((char)*p++);
}

void dbg_puts(const char *text)
{
    while (*text) dbg_putc(*text++);
}

void dbg_u32(uint32_t value)
{
    static const char hex[] = "0123456789abcdef";
    for (int shift = 28; shift >= 0; shift -= 4)
        dbg_putc(hex[(value >> shift) & 0xf]);
}

void dbg_hex(const char *label, uint32_t value)
{
    dbg_puts(label); dbg_puts("=0x"); dbg_u32(value); dbg_putc('\n');
}

/* Freestanding divide-by-10 (hardware integer divide would pull in
 * __aeabi_uidivmod, which this no-libc link does not provide; multiply is
 * fine on this core). */
static uint32_t div10(uint32_t n, uint32_t *rem)
{
    uint32_t q = (n >> 1) + (n >> 2);
    q += q >> 4; q += q >> 8; q += q >> 16; q >>= 3;
    uint32_t r = n - q * 10u;
    if (r >= 10u) { q++; r -= 10u; }
    *rem = r;
    return q;
}

void dbg_dec(uint32_t value)
{
    char tmp[10];
    unsigned n = 0;
    uint32_t rem;
    do { value = div10(value, &rem); tmp[n++] = (char)('0' + rem); } while (value);
    while (n--) dbg_putc(tmp[n]);
}

unsigned dbg_fill(uint8_t *out, unsigned max)
{
    unsigned n = 0;
    while (tail != head && n < max) out[n++] = buffer[tail++ & LOG_MASK];
    return n;
}
