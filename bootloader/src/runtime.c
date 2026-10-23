#include "service.h"

/* The delay counter is free-running after stage-2 entry. Do not reset it in
 * delay_ticks: the boot window and transfer deadlines share this clock. The
 * 96 MHz rate was measured against host monotonic time on this board. */
#define COUNTER 0xc0c82008u
#define TICKS_PER_MS CLOCK_TICKS_PER_MS
static uint32_t previous_ticks, milliseconds, fractional_ticks;

void clock_init(void)
{
    wr(0xc0c8200c,0); wr(COUNTER,0); wr(0xc0c82010,0); wr(0xc0c8200c,1);
    previous_ticks=milliseconds=fractional_ticks=0;
}

uint32_t time_ms(void)
{
    uint32_t now=rd(COUNTER), delta=now-previous_ticks;
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
    uint32_t start=rd(COUNTER);
    while((uint32_t)(rd(COUNTER)-start)<ticks) {}
}

void *memcpy(void *to,const void *from,size_t length)
{ uint8_t *d=to; const uint8_t *s=from; while(length--) *d++=*s++; return to; }
void *memset(void *to,int value,size_t length)
{ uint8_t *d=to; while(length--) *d++=(uint8_t)value; return to; }
size_t strlen(const char *s)
{ const char *end=s; while(*end) ++end; return (size_t)(end-s); }
