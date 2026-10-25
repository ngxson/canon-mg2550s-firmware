/* Native application bootstrap, an explicit intermediate stock dependency.
 * The flash-resident native path switches to internal SRAM stack before RAM
 * testing/clearing, then configures MPU/cache and its own banked stacks. The
 * discarded source-loader RAM and the app payload copy may be cleared safely:
 * no code here returns to them. Only the native boot-block checksum call is
 * replaced by the builder; the source loader already checked this MGAP image.
 */
__attribute__((noreturn)) void stockapp_native_startup(void)
{
    ((void (*)(void))0x11070u)();
    ((void (*)(void))0x11008u)();
    for (;;) {}
}
