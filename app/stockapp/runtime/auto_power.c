/* Observation-only startup: feed one debounced Power press through Canon's
 * existing panel logic. No GPIO output is driven. Preserve real button bits.
 * Counter lives in reserved SRAM because native startup clears external RAM.
 * The model resolves 64 released samples, 20 pressed samples, then release.
 * This remains a stock dependency, to be replaced by a source power manager.
 */
#include <stdint.h>
uint32_t stockapp_power_sample(void)
{
    volatile uint32_t *count=(volatile uint32_t *)0xe00008f4u;
    uint32_t n=*count;
    if(n<84) *count=n+1;
    uint32_t keys=*(volatile uint32_t *)0xc0c8d028u;
    if(n>=64 && n<84) keys|=0x40u;
    return keys;
}
