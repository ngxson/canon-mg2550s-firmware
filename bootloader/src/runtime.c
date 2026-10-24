#include "service.h"

/* The delay counter is free-running after stage-2 entry. Do not reset it in
 * delay_ticks: the boot window and transfer deadlines share this clock. The
 * 96 MHz rate was measured against host monotonic time on this board. */
#define COUNTER 0xc0c82008u
#define TICKS_PER_MS CLOCK_TICKS_PER_MS
static uint32_t previous_ticks, milliseconds, fractional_ticks;
#ifdef APPLICATION_BUILD
/* The first long homing run exposed a raw counter discontinuity equivalent
 * to a 29-bit rollover. Accumulate low-29-bit deltas between frequent samples;
 * this also works on a wider counter. Never reset the shared timer to time a
 * move. App-only: the installed/hash-pinned recovery stages are unchanged. */
static uint32_t counter_sample, accumulated_ticks;
uint32_t app_clock_ticks(void)
{
    uint32_t raw = rd(COUNTER);
    accumulated_ticks += (raw - counter_sample) & 0x1fffffffu;
    counter_sample = raw;
    return accumulated_ticks;
}
#define READ_TICKS() app_clock_ticks()
#else
#define READ_TICKS() rd(COUNTER)
#endif

void clock_init(void)
{
    wr(0xc0c8200c,0); wr(COUNTER,0); wr(0xc0c82010,0); wr(0xc0c8200c,1);
    previous_ticks=milliseconds=fractional_ticks=0;
#ifdef APPLICATION_BUILD
    counter_sample=accumulated_ticks=0;
#endif
}

uint32_t time_ms(void)
{
    uint32_t now=READ_TICKS(), delta=now-previous_ticks;
    previous_ticks=now;
    milliseconds+=delta/TICKS_PER_MS;
    fractional_ticks+=delta%TICKS_PER_MS;
    if(fractional_ticks>=TICKS_PER_MS) {
        fractional_ticks-=TICKS_PER_MS; ++milliseconds;
    }
    return milliseconds;
}

void delay_ticks(uint32_t ticks)
{
    uint32_t start=READ_TICKS();
    while((uint32_t)(READ_TICKS()-start)<ticks) {}
}

void *memcpy(void *to,const void *from,size_t length)
{ uint8_t *d=to; const uint8_t *s=from; while(length--) *d++=*s++; return to; }
void *memset(void *to,int value,size_t length)
{ uint8_t *d=to; while(length--) *d++=(uint8_t)value; return to; }
size_t strlen(const char *s)
{ const char *end=s; while(*end) ++end; return (size_t)(end-s); }
