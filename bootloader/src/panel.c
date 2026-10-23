#include "fw.h"
static unsigned next_led;

void panel_init(void)
{
    /* Panel multiplexing: observed boot values; logical map in evidence notes. */
    wr(PANEL+0x38,0x1f); wr(PANEL+0x3c,0x543210);
    wr(PANEL+0x40,0x20); wr(PANEL+0x44,0);
    wr(PANEL+0x18,0x25a6); wr(PANEL+0x1c,0x169); wr(PANEL+0x20,6);
    static const uint32_t modes[]={0x30,0x32,0x30,0x30,0x38,0};
    for (unsigned i=0;i<6;++i) {
        wr(PANEL+0x100+i*8,modes[i]); wr(PANEL+0x104+i*8,0);
        wr(PANEL+0x140+i*8,0); wr(PANEL+0x144+i*8,0);
    }
    wr(PANEL+0x180,0xf); wr(PANEL+0x184,0);
    wr(PANEL,1); wr(PANEL+4,1);
    wr(TIMER,0x80000000); while(rd(TIMER)) {}
    wr(0xc0101314,0); delay_ticks(0x2580);
    wr(0xc0101314,1); wr(0xc0101214,0);
    wr(TIMER,0x80000000); while(rd(TIMER)) {}
    wr(TIMER+4,0); modify(TIMER+0x14,0,3);
    wr(TIMER+8,0xb3b0); wr(TIMER+0x10,1);
    next_led=0; /* First main-loop tick displays the first LED and starts timer. */
}

void leds_tick(void)
{
    if (rd(TIMER) || (rd(PANEL+0x70)&0x1f)) return;
#ifdef APPLICATION_BUILD
    /* Demo application: power, then the other three LEDs, one at a time. */
    wr(PANEL+0x140,next_led==1?0x20:0);
    wr(PANEL+0x148,next_led==0?0x10:0);
    wr(PANEL+0x150,next_led==2?0x20:0);
    wr(PANEL+0x158,next_led==3?0x10:0);
    next_led=(next_led+1)&3;
#else
    /* User identified the first sequence LED as power on 2026-10-23.
     * Rescue alternates power ON/OFF; all other LED outputs remain zero. */
    wr(PANEL+0x140,0);
    wr(PANEL+0x148,next_led==0?0x10:0);
    wr(PANEL+0x150,0);
    wr(PANEL+0x158,0);
    next_led^=1;
#endif
    wr(PANEL+0x14,1);
    wr(TIMER,1);
}
