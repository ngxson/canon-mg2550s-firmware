/* Observation-only startup: feed one debounced Power press through Canon's
 * existing panel logic. No GPIO output is driven. Preserve real button bits.
 * Counter lives in reserved SRAM because native startup clears external RAM.
 * The model resolves 64 released samples, 20 pressed samples, then release.
 * This remains a stock dependency, to be replaced by a source power manager.
 *
 * Boot INK_OFF re-assert (build flag ink_off_boot, needs ink_off too):
 * after the Power pulse counter saturates, one-shot check the EEPROM working
 * cache byte 0x22c (BK monitoring-off = bit 3, CL = bit 4; loaded early in
 * boot before the panel task samples) and, if either bit is clear, set
 * INK_OFF_BK/CL + the group-3 record mirror through the same verified field
 * path the USB 0xd5 override uses (stockapp_set_ink_off -> 0x10b1bc ->
 * 0x112020 commits). Retries on later panel samples while the field registry
 * finishes constructing; writes NVM only when a bit is actually missing, so
 * a normal boot with the override already persisted performs no EEPROM write.
 *
 * Reserved-SRAM state word 0xe00008f4 layout:
 *   bits 0..7   Power pulse counter (saturates at 84)
 *   bits 16..23 boot INK_OFF re-assert attempts (bounded, diagnostics)
 *   bit  31     boot INK_OFF re-assert done
 */
#include <stdint.h>

#ifdef INK_OFF_READY
extern uint32_t stockapp_set_ink_off(uint32_t mask, uint32_t value);
#endif

#define PWR_COUNT_MASK   0x000000ffu
#define ATTEMPT_SHIFT    16
#define ATTEMPT_MASK     0x00ff0000u
#define INKOFF_DONE_BIT  0x80000000u

uint32_t stockapp_power_sample(void)
{
    volatile uint32_t *count=(volatile uint32_t *)0xe00008f4u;
    uint32_t s=*count;
    uint32_t n=s&PWR_COUNT_MASK;
    if(n<84u) *count=s+1u;
    uint32_t keys=*(volatile uint32_t *)0xc0c8d028u;
    if(n>=64u && n<84u) keys|=0x40u;
#if defined(INK_OFF_BOOT) && defined(INK_OFF_READY)
    if(n>=84u && !(s&INKOFF_DONE_BIT))
    {
        volatile uint8_t *ink_off_byte=(volatile uint8_t *)(0x1878e200u+0x22cu);
        if(((*ink_off_byte)&0x18u)==0x18u)
        {
            *count=s|INKOFF_DONE_BIT;    /* already asserted: no NVM write */
        }
        else
        {
            uint32_t attempts=(s&ATTEMPT_MASK)>>ATTEMPT_SHIFT;
            if(attempts>=200u)
            {
                *count=s|INKOFF_DONE_BIT;    /* give up quietly */
            }
            else
            {
                uint32_t ok=stockapp_set_ink_off(0u,1u);   /* both tanks, set */
                uint32_t v=*count;
                v=(v&~ATTEMPT_MASK)|((attempts+1u)<<ATTEMPT_SHIFT);
                if(ok>=4u) v|=INKOFF_DONE_BIT;   /* 2 tanks x 2 fields */
                *count=v;
            }
        }
    }
#endif
    return keys;
}
