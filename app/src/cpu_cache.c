/* ARM946E-S instruction cache control.
 *
 * The bootloader hands the app over with the protection unit and both caches
 * off (bootloader/src/cpu.c, cpu_quiesce; CP15 control reads 0x78 on this
 * board, ARM946E-S id 0x41059461, no TCM). DMA engines (LDMAC pixel rows, the
 * USB core, the print engines) write and read DRAM behind the CPU. Their
 * mutable buffers must remain outside the data-cacheable regions below.
 * At boot only instruction fetches are cached; no DMA ever
 * produces code, and the app image is copied by the bootloader before the
 * cache is enabled, so the cache cannot hold stale instructions.
 *
 * On the ARM946E-S the caches only work with the protection unit enabled, so
 * one region (number 0, base 0, size 4 GiB, full access) covers everything:
 * instruction-cacheable, data not cacheable, not bufferable. This is CP15
 * register access, which C cannot express; it is the only assembly in the
 * app besides the stock-call bridge (AGENTS.md). Switched at runtime through
 * USB 0x8c so a wrong configuration is observable (bench numbers in the log)
 * and recoverable without a reflash. */
#include "app.h"

#define CP15_CONTROL_MPU (1u << 0)
#define CP15_CONTROL_DCACHE (1u << 2)
#define CP15_CONTROL_ICACHE (1u << 12)

static uint32_t control_read(void)
{
    uint32_t v;
    __asm__ volatile("mrc p15, 0, %0, c1, c0, 0" : "=r"(v));
    return v;
}

uint32_t cpu_icache_state(void) { return control_read(); }

/* Data cache, write-through, for the app's own DRAM only (2026-10-27: a 300 dpi
 * colour row took ~90 ms with every load going to DRAM/flash). Region 1 =
 * 0x20000000 + 4 MiB (code, data, bss, tables; the scan ring at 0x20420000 and
 * USB buffers at 0x20400000 and stock's 0x18xxxxxx alias stay outside, so DMA rows and stock's writes are
 * never cached), region 2 = 0x20780000 + 512 KiB (stack, exception record).
 * Cacheable with the bufferable attribute clear = write-through, so
 * writes reach memory without dirty cache lines; drain the write buffer on
 * transitions before invalidating. Enabled
 * by the page scan while it converts rows and switched off at its end. */
bool cpu_dcache_set(bool on)
{
    uint32_t control = control_read(), zero = 0;
    if (!(control & CP15_CONTROL_ICACHE)) return false;         /* needs the protection unit (I-cache switch) */
    if (!!(control & CP15_CONTROL_DCACHE) == on) return true;
    __asm__ volatile("mcr p15, 0, %0, c7, c10, 4" :: "r"(zero) : "memory");
    if (on) {
        /* The MPU is already live. Establish permissions and attributes
         * BEFORE enabling overriding regions, including the current code
         * and stack. The opposite order can fault between MCRs, depending
         * on the instruction-cache/prefetch state and function alignment. */
        __asm__ volatile(
            "mcr p15, 0, %5, c5, c0, 2\n"      /* data access permission, extended: regions 0..2 full */
            "mcr p15, 0, %5, c5, c0, 3\n"      /* instruction access permission, extended: regions 0..2 full */
            "mcr p15, 0, %2, c2, c0, 1\n"      /* instruction cacheable: regions 0..2 */
            "mcr p15, 0, %3, c2, c0, 0\n"      /* data cacheable: regions 1, 2 */
            "mcr p15, 0, %4, c3, c0, 0\n"      /* write buffer: none (write-through) */
            "mcr p15, 0, %0, c6, c1, 0\n"      /* region 1: 0x20000000, 4 MiB, enabled */
            "mcr p15, 0, %1, c6, c2, 0\n"      /* region 2: 0x20780000, 512 KiB, enabled */
            "mcr p15, 0, %4, c7, c6, 0\n"      /* invalidate the data cache */
            :: "r"(0x20000000u | 0x2bu), "r"(0x20780000u | 0x25u), "r"(7u), "r"(6u), "r"(zero), "r"(0x333u) : "memory");
        control |= CP15_CONTROL_DCACHE;
    } else {
        control &= ~CP15_CONTROL_DCACHE;
    }
    __asm__ volatile("mcr p15, 0, %0, c1, c0, 0\n"
                     "mcr p15, 0, %1, c7, c6, 0\n" :: "r"(control), "r"(zero) : "memory");
    return true;
}

bool cpu_icache_set(bool on)
{
    uint32_t control = control_read(), zero = 0;
    if (control & CP15_CONTROL_DCACHE) return false;            /* never with a data cache someone else enabled */
    if (on) {
        /* Region 0: base 0, size encoding 0x1f = 4 GiB, enabled. Instruction
         * cacheable bit for region 0 only; data cacheable and write-buffer
         * bits all clear; access permission 3 (full) for region 0 in both the
         * instruction and data permission registers. */
        __asm__ volatile(
            /* An earlier scan or RAM-to-RAM app restart can leave enabled
             * regions behind. Higher-numbered regions override region 0;
             * clearing their permissions alone would fault on app/stack RAM. */
            "mcr p15, 0, %2, c6, c1, 0\n"
            "mcr p15, 0, %2, c6, c2, 0\n"
            "mcr p15, 0, %2, c6, c3, 0\n"
            "mcr p15, 0, %2, c6, c4, 0\n"
            "mcr p15, 0, %2, c6, c5, 0\n"
            "mcr p15, 0, %2, c6, c6, 0\n"
            "mcr p15, 0, %2, c6, c7, 0\n"
            "mcr p15, 0, %0, c6, c0, 0\n"      /* region 0 base/size/enable */
            "mcr p15, 0, %1, c2, c0, 1\n"      /* instruction cacheable: region 0 */
            "mcr p15, 0, %2, c2, c0, 0\n"      /* data cacheable: none */
            "mcr p15, 0, %2, c3, c0, 0\n"      /* write buffer: none */
            "mcr p15, 0, %3, c5, c0, 2\n"      /* data access permission, extended: region 0 full */
            "mcr p15, 0, %3, c5, c0, 3\n"      /* instruction access permission, extended: region 0 full */
            "mcr p15, 0, %2, c7, c5, 0\n"      /* invalidate the instruction cache */
            :: "r"(0x3fu), "r"(1u), "r"(zero), "r"(3u) : "memory");
        control |= CP15_CONTROL_MPU | CP15_CONTROL_ICACHE;
    } else {
        control &= ~(CP15_CONTROL_MPU | CP15_CONTROL_ICACHE);
    }
    __asm__ volatile("mcr p15, 0, %0, c1, c0, 0\n"
                     "mcr p15, 0, %1, c7, c5, 0\n" :: "r"(control), "r"(zero) : "memory");
    return true;
}

/* Instructions retired by a tight loop in 10 ms of the 96 MHz free-running
 * clock: `subs; bne` per iteration, counted as two instructions. */
uint32_t cpu_bench(void)
{
    uint32_t t0 = app_clock_ticks(), n = 0;
    while (app_clock_ticks() - t0 < 10u * CLOCK_TICKS_PER_MS) {
        uint32_t k = 1000;
        __asm__ volatile("1: subs %0, %0, #1\n bne 1b" : "+r"(k) :: "cc");
        n += 2000u;
    }
    return n;
}
