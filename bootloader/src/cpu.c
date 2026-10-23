/* Only this file uses assembly: ARM reset/stack and privileged CPU controls. */
#include "fw.h"

__attribute__((naked, section(".vectors"), used)) void vectors(void)
{
    __asm__ volatile("b reset_entry\n b fault\n b fault\n b fault\n"
                     "b fault\n b fault\n b fault\n b fault\n");
}

__attribute__((naked, section(".reset"), used, noreturn)) void reset_entry(void)
{
    /* On this board internal SRAM is available before external DRAM setup.
     * AAPCS stack alignment is eight bytes; IRQ/FIQ stay masked. */
    __asm__ volatile("msr cpsr_c, #0xd3\n"
                     "ldr sp, =0xe0000f00\n"
                     "bl boot_c\n"
                     "ldr sp, =0x207ff000\n"
                     "ldr r0, =firmware_main\n"
                     "bx r0\n");
}

/* Compatibility landings for the previously installed updater's exits. */
__attribute__((naked, section(".warm_recovery"), used)) void warm_recovery(void)
{ __asm__ volatile("b reset_entry"); }
__attribute__((naked, section(".warm_normal"), used)) void warm_normal(void)
{ __asm__ volatile("b warm_cache"); }

/* The old RAM updater first jumps to flash 0x1800/0x1804, rather than directly
 * to its boot continuation. Source-defined branch landings permit replacement
 * of that updater without retaining any of its wrapper or executable body. */
__attribute__((naked, section(".updater_exit"), used)) void updater_exit(void)
{ __asm__ volatile("b warm_cache\n b warm_normal"); }

/* The old boot continuation can still be in I-cache after flash programming.
 * Its cache-invalidate entry is 0x1bc; retain this landing and invalidate before
 * fetching the new reset branch. All following initialization is our C code. */
__attribute__((naked, section(".warm_cache"), used)) void warm_cache(void)
{ __asm__ volatile("mov r0, #0\n mcr p15, 0, r0, c7, c5, 0\n b reset_entry"); }

BOOT __attribute__((noreturn)) void fault(void) { for (;;) {} }

BOOT void cpu_quiesce(void)
{
    uint32_t zero=0, control;
    /* Index clean+invalidate covers four ways, 128 32-byte lines each.
     * Needed when entered from an old updater with caches already enabled. */
    for (uint32_t way=0; way<4; ++way) {
        for (uint32_t line=0; line<0x1000; line+=32) {
            uint32_t index=(way<<30)|line;
            __asm__ volatile("mcr p15, 0, %0, c7, c10, 4\n"
                             "mcr p15, 0, %1, c7, c14, 2" :: "r"(zero), "r"(index) : "memory");
        }
    }
    __asm__ volatile("mcr p15, 0, %0, c7, c5, 0\n"
                     "mrc p15, 0, %1, c1, c0, 0" : "+r"(zero), "=r"(control) :: "memory");
    control &= ~0x1005u; /* Disable I-cache, D-cache and MPU for simple DMA coherency. */
    __asm__ volatile("mcr p15, 0, %0, c1, c0, 0" :: "r"(control) : "memory");
}

void cpu_reset(void)
{
    /* Actual reset entry reinitializes CPU/board state. No function at address
     * zero is called from C, where a null function pointer would be undefined. */
    __asm__ volatile("msr cpsr_c, #0xd3\n bx %0" :: "r"(0) : "memory");
    __builtin_unreachable();
}
