#include "service.h"
extern uint32_t __bss_start[],__bss_end[];

__attribute__((section(".app_entry"),noreturn)) void app_entry(void)
{
    volatile uint32_t *p=__bss_start;
    while(p<__bss_end) *p++=0;
    firmware_main();
    __builtin_unreachable();
}

/* A software reset, not a power-domain reset. The proven reset entry performs
 * CPU/cache and board reinitialization. Kept separate from protocol C. */
void cpu_reset(void)
{
    __asm__ volatile("msr cpsr_c, #0xd3\n bx %0" :: "r"(0) : "memory");
    __builtin_unreachable();
}
