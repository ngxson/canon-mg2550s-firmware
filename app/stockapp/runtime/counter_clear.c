/* Clear usage counters on demand -- vendor USB request 0xd3 (build flag
 * clear_counters). Write ABI corrected per fw_dump/gpt/BRIEF_counter_write.md.
 *
 * ROOT CAUSE of the first HW attempt (0 written, EEPROM unchanged): the entry
 * used before, 0x10b2c0, is a READ wrapper, and its prologue rejects an odd
 * `count` with an immediate -17 (0xffffffef) -- our count=1 never even took the
 * lock. The real scalar WRITE entry is 0x10b1bc, value-based, no preconditions:
 *
 *   int 0x10b1bc(id[r0], value[r1], 1[r2], persist[r3]) -> 0 ok
 *
 * - all args are values (no pointers); the field WIDTH comes from the
 *   descriptor, NOT the caller.
 * - r3 = 1 persists into the 24C08 working cache at 0x1878e200+offset
 *   (r3 = 0 would write only the volatile array). The cache updates
 *   immediately, so a 0xd2 dump reflects the clear right away.
 * - it locks (0x109bd2 / 0x109bc8) and finds the field (0x10cc04) internally.
 *
 * After the writes we request the deferred physical flush the way stock's own
 * counter incrementer does (0xcb7e8 calls 0x112020(1) after writing): the flush
 * writes the primary AND the group's redundant copy to the 24C08. There is no
 * separate checksum byte; the redundant copy is the integrity mechanism
 * (validated/repaired by 0x10d8c4). 0x112020 takes a small int (1), not an id.
 *
 * The list is per-id and deliberately EXCLUDES the error history
 * (0x1050281 @0x230) and event log (0x105030a @0x2fc) adjacent to the counters
 * -- the event log is preserved by construction. inkLevel is NOT cleared here
 * (0 = empty; "report full" is the ink clamp). A bad write is recoverable from
 * eeprom_backup_initial.bin via the I2C bridge.
 *
 * Caveat (BRIEF_counter_write.md sec 5): the write-inhibit flag u16 0x1878ff10 --
 * if non-zero, 0x10d5dc silently skips the cache write. It is 0 in normal
 * operation; if a clear returns success but a 0xd2 dump does not change, read
 * that word first.
 *
 * The host selects a single counter by index (wValue), or 0xffff to clear all.
 */
#include <stdint.h>

/* Scalar field write (VERIFIED, with stock callers at 0x26704/0x26bfa). */
typedef int (*reg_write_fn)(uint32_t id, uint32_t value, uint32_t one, uint32_t persist);
#define REG_WRITE ((reg_write_fn)0x10b1bdu)   /* 0x10b1bc | 1 = Thumb */

/* Deferred NVM flush request (as stock's incrementer 0xcb7e8 does). */
typedef int (*commit_fn)(uint32_t arg);
#define REG_COMMIT ((commit_fn)0x112021u)      /* 0x112020 | 1 = Thumb */

struct counter { uint32_t id; uint16_t width; };  /* width informational now */

/* Verified counter map (BRIEF_counters.md sec 1). Width is from the descriptor at
 * write time; kept here only to document each field's size. */
static const struct counter counters[] = {
    { 0x0105029au, 16 },   /* ASF PAGE All  (printed-sheet total) */
    { 0x0105029bu, 16 },   /* ASF PAGE PP */
    { 0x0105038bu, 16 },   /* ASF Photo1 */
    { 0x01050360u, 16 },   /* ASF Photo3 */
    { 0x0105029fu, 16 },   /* ASF EV */
    { 0x0105037fu, 16 },   /* TPAGE COPY */
    { 0x01050470u, 16 },   /* page counter */
    { 0x010502f2u, 16 },   /* SC TTL (scan total) */
    { 0x010502f4u, 16 },   /* SC PC */
    { 0x010502f5u, 16 },   /* SC COPY */
    { 0x0105028eu, 16 },   /* P_ON  (soft power-on count) */
    { 0x0105028du, 16 },   /* hard-boot counter */
    { 0x0105024du, 16 },   /* CH_BK (BK cartridge inserts) */
    { 0x0105024eu, 16 },   /* CH_CL (CL cartridge inserts) */
    { 0x01050292u, 32 },   /* LPT   (last print time) */
};
#define NCOUNTERS ((unsigned)(sizeof(counters) / sizeof(counters[0])))

/* selector: a counter index, or 0xffff = clear all. Returns the number of
 * counters whose write reported success. */
uint32_t stockapp_clear_counters(uint32_t selector)
{
#ifdef COUNTER_CLEAR_READY
    uint32_t cleared = 0;
    for (unsigned i = 0; i < NCOUNTERS; ++i) {
        if (selector != 0xffffu && selector != i) continue;
        if (REG_WRITE(counters[i].id, 0u, 1u, 1u) == 0)   /* value 0, persist */
            ++cleared;
    }
    if (cleared)
        REG_COMMIT(1u);     /* request the deferred 24C08 flush */
    return cleared;
#else
    (void)counters; (void)selector;
    return 0xEEu;   /* not configured: nothing written */
#endif
}
