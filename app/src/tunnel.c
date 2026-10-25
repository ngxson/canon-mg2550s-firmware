/* Register tunnel for hardware-in-the-loop emulation (USB 0x90..0x93).
 *
 * The stock firmware runs in the emulator; selected peripheral accesses are
 * forwarded to this board so the real ASIC answers them. The host sets an
 * address (0x90), then reads up to 256 bytes (0x91, IN) or writes one 32-bit
 * word (0x92). Reads are allowed only inside the allowlist below: GPIO and
 * panel blocks, the head blocks, the motor blocks and the ADC data; none of
 * these have read side effects in our own drivers. Writes additionally need
 * the tunnel to be unlocked (0x93 with the key) and are refused in the motor
 * blocks and anywhere outside the allowlist; the interrupt controller, clocks,
 * flash, the USB core and our own RAM are never reachable. Every access is
 * counted; the firmware log shows the first refusals. */
#include "app.h"

#define TUNNEL_KEY 0x7d4eu

struct range { uint32_t lo, hi; bool writable; };
static const struct range allow[] = {
    { 0xc0c20000u, 0xc0c2a000u, true },    /* head blocks: converter, engine, fuse, waveform */
    { 0xc0c50000u, 0xc0c58000u, false },   /* CR/LF/scanner motor blocks: status and counters only */
    { 0xc0c80000u, 0xc0c88000u, true },    /* GPIO, alias and sense banks */
    { 0xc0c8d000u, 0xc0c8e000u, false },   /* panel keys and LEDs: read only */
    { 0xc0c90000u, 0xc0c90100u, true },    /* ADC block */
};

static uint32_t address, reads, writes, refused;
static bool unlocked;

static const struct range *lookup(uint32_t a, uint32_t n)
{
    for (unsigned i = 0; i < ARRAY_SIZE(allow); ++i)
        if (a >= allow[i].lo && a + n <= allow[i].hi && a + n > a) return &allow[i];
    return 0;
}

bool tunnel_set_address(uint32_t a) { address = a & ~3u; return true; }

bool tunnel_unlock(unsigned key)
{
    unlocked = key == TUNNEL_KEY;
    dbg_puts(unlocked ? "[tunnel] writes unlocked\n" : "[tunnel] writes locked\n");
    return true;
}

/* 0x91 IN: `max` bytes (multiple of 4, at most 256) from the set address. */
unsigned tunnel_read(uint8_t *out, unsigned max)
{
    unsigned n = max > 256u ? 256u : max & ~3u;
    if (!n || !lookup(address, n)) { ++refused; if (refused <= 8) { dbg_puts("[tunnel] read refused at 0x"); dbg_u32(address); dbg_putc('\n'); } return 0; }
    for (unsigned i = 0; i < n; i += 4) put32(out + i, rd(address + i));
    ++reads;
    return n;
}

/* 0x92 OUT: one 32-bit write at the set address. */
bool tunnel_write(uint32_t value)
{
    const struct range *r = lookup(address, 4);
    if (!unlocked || !r || !r->writable) { ++refused; if (refused <= 8) { dbg_puts("[tunnel] write refused at 0x"); dbg_u32(address); dbg_putc('\n'); } return false; }
    wr(address, value); ++writes;
    return true;
}

/* 0x94 IN: counters. */
unsigned tunnel_status(uint8_t *out)
{
    put32(out, 0x4e55544du); put32(out + 4, 1); put32(out + 8, address); put32(out + 12, reads);
    put32(out + 16, writes); put32(out + 20, refused); put32(out + 24, unlocked ? 1u : 0);
    return 28;
}
