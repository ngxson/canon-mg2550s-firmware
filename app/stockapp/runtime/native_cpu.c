/* Intermediate dependency: reuse the pinned stock privileged CPU routines.
 * These run in SVC mode before the final User-mode handoff. The first clears
 * all MPU regions and caches; the second installs stock's region permissions,
 * cacheability and buffering attributes. Neither performs peripheral or flash
 * writes. The low vectors remain the source bootloader's registered wrappers.
 * Code is flash-resident; the bootloader stack is in the unrestricted,
 * uncached region 0 while stock RAM aliases receive their native attributes.
 * Replace these calls with documented source CP15 setup in the final firmware.
 */
void stockapp_native_cpu(void)
{
    ((void (*)(void))0x11070u)();
    ((void (*)(void))0x11150u)();
    ((void (*)(void))0x150110u)();
    ((void (*)(void))0x150144u)();
}
