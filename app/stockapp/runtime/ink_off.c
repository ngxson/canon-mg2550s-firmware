/* "Ignore low ink" persistent override -- vendor USB request 0xd5 (build flag
 * ink_off). Replicates stock's own persist routine 0x9d220 exactly, i.e. what
 * holding Stop/Cancel on a low-ink error does (BRIEF_ink_off.md).
 *
 * Per tank, stock sets two field pairs through the registry write path and then
 * commits:
 *   INK_OFF_BK = 0x410510a4   (EEPROM 0x22c bit 3)  group-5
 *   INK_OFF_CL = 0x410510a5   (EEPROM 0x22c bit 4)  group-5
 *   0x410310a2 / 0x410310a3                           group-3 record mirror the
 *                                                     monitor reloads at boot
 *   then commit 0x112020(1) and 0x112020(9).
 *
 * value 1 = monitoring OFF ("ignored", lamp/nag/print-gate suppressed),
 * value 0 = re-enable monitoring. This disables only ink-LEVEL monitoring; it
 * does NOT disable cartridge-presence detection (noInkTankErr etc. stay live).
 *
 * 0x22c is bit-packed with unrelated flags, so we MUST write through the field
 * API (it read-modify-writes); never poke the raw byte. The cleanest result
 * needs a reboot after setting so the monitor re-syncs "monitoring off" from
 * the persisted bits (an already-latched warning may otherwise keep flashing).
 *
 * Write path (BRIEF_counter_write.md): 0x10b1bc(id, value, 1, persist=1) -> 0
 * on success; flush 0x112020(arg). (Stock uses the vtable setter 0x111ed4 for
 * the same effect; 0x10b1bc is the path we have already verified on hardware
 * and is independent of any ink setter hook.)
 *
 * The host selects tanks with wValue (bit0 = BK, bit1 = CL; 0 => both) and the
 * value with wIndex (0 => set/ignore, non-zero => clear/re-enable).
 */
#include <stdint.h>

typedef int (*set_fn)(uint32_t id, uint32_t value, uint32_t one, uint32_t persist);
typedef int (*commit_fn)(uint32_t arg);
#define SET_FIELD ((set_fn)0x10b1bdu)      /* 0x10b1bc | 1 = Thumb */
#define COMMIT    ((commit_fn)0x112021u)   /* 0x112020 | 1 = Thumb */

struct pair { uint32_t mirror, ink_off; };   /* group-3 record mirror, group-5 INK_OFF */
static const struct pair tanks[] = {
    { 0x410310a2u, 0x410510a4u },   /* BK */
    { 0x410310a3u, 0x410510a5u },   /* CL */
};

/* mask: bit0 = BK, bit1 = CL (0 => both). value: 1 = ignore (monitoring off),
 * 0 = re-enable. Returns the number of field writes that reported success. */
uint32_t stockapp_set_ink_off(uint32_t mask, uint32_t value)
{
#ifdef INK_OFF_READY
    uint32_t n = 0;
    value = value ? 1u : 0u;
    if (!(mask & 3u)) mask = 3u;
    for (unsigned i = 0; i < 2; ++i) {
        if (!(mask & (1u << i))) continue;
        if (SET_FIELD(tanks[i].mirror,  value, 1u, 1u) == 0) ++n;
        if (SET_FIELD(tanks[i].ink_off, value, 1u, 1u) == 0) ++n;
    }
    COMMIT(1u);
    COMMIT(9u);     /* stock issues both */
    return n;
#else
    (void)tanks; (void)mask; (void)value;
    return 0xEEu;   /* not configured */
#endif
}
