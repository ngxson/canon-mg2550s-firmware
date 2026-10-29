/* Experimental read-only observation of the pinned MG2500 2.050 image.
 * No allocator, persistent storage, peripheral reads, or new SRAM reservation.
 * The stock SETUP worker serializes EP0 and resets its buffer at 0x51b02.
 * Its GET_DEVICE_ID handler (0x51e8c) already copies into this same buffer.
 * Snapshots can change between reads; they are not atomic across regions.
 */
#include <stdint.h>
#include <stddef.h>

struct control {
    uint32_t vtable;
    uint8_t type, request;
    uint16_t value, index, length;
    uint32_t remaining;
    uint8_t *buffer;
};
_Static_assert(offsetof(struct control, remaining)==12, "stock control layout");
_Static_assert(offsetof(struct control, buffer)==16, "stock control layout");

struct region { uint32_t address, length; };
/* Regions 4-6 are the stock message recorder at 0x50a76-0x50bca: every
 * dispatched message ID (u16 at +0x1c of the message) is stored at
 * 0x180404ce[0x1803fcc5++]; IDs outside a fixed housekeeping list
 * (0xf002/f006/f00c/f00f/f010/f01f-f023/f04f-f052) also go to
 * 0x180406ce[0x180404cc++]. 0x1803fcc6 and 0x180404cd count processed
 * messages. Region 1 already carries 0x1803fcc5/6.
 */
/* Regions 7-11 (ABI 3) are plain-RAM engine/status state identified by the
 * answered RE brief 12 (fw_dump/gpt/BRIEF_message_ids.md): the reported-status
 * cache read by the ivec status formatter (DJS category nibble at +0x0f,
 * substate u16 at +0x10 bits 4-8, PID at +0x13..+0x15, CC override at
 * +0x124; readers 0x3e66c/0x3ef5a, writer 0x39018), the print geometry in
 * 1/600 inch (active and requested area/paper; writers 0xac21a/0xac75c/
 * 0xaacaa), the ASF state byte (0xdc024), the LF position word (0xd9322) and
 * the saved paper-edge coordinate plus edge-state byte (0xd72b6/0xd7250).
 * None of these are MMIO.
 */
static const struct region regions[] = {
    {0x1804099c, 4},       /* USB state, mode, initialized, attached */
    {0x1803fcc4, 8},       /* error bytes, message and SETUP history indices */
    {0x1803fccc, 1024},    /* SETUP first four bytes, 256 entries */
    {0x180400cc, 1024},    /* SETUP last four bytes, 256 entries */
    {0x180404cc, 2},       /* filtered message ring: write index, processed count */
    {0x180404ce, 512},     /* all message IDs, 256 x u16 */
    {0x180406ce, 512},     /* filtered message IDs, 256 x u16 */
    {0x1803a790, 0x128},   /* reported-status cache: DJS/PID/BST inputs, CC override */
    {0x1806c974, 28},      /* print geometry, 1/600 inch */
    {0x180c6f54, 1},       /* ASF state */
    {0x180d5790, 4},       /* LF position word */
    {0x1879b98c, 5},       /* saved edge coordinate, edge state */
};
/* Little endian: STL1, ABI, maximum transfer, region count, then region pairs.
 * ABI 3 is 112 bytes, so the host reads it with wValue as the byte offset.
 * Addresses are diagnostic metadata, never accepted from the USB requester.
 */
static const uint32_t info[] = {
    0x314c5453, 3, 64, 12,
    0x1804099c, 4, 0x1803fcc4, 8,
    0x1803fccc, 1024, 0x180400cc, 1024,
    0x180404cc, 2, 0x180404ce, 512, 0x180406ce, 512,
    0x1803a790, 0x128, 0x1806c974, 28, 0x180c6f54, 1,
    0x180d5790, 4, 0x1879b98c, 5,
};

/* EEPROM (24C08) working cache: a RAM copy of the 1 KiB NVM indexed by 24C08
 * byte offset (base verified in emulator: 950/1024 bytes equal the input image
 * before boot-time counter updates; field 0x160 etc. land at base+offset per
 * BRIEF_maintenance.md §5). This is where the usage counters live, so request
 * 0xd2 dumps it read-only for diagnosis (clear is a separate, write request,
 * pending BRIEF_counters.md). Nothing here writes NVM. */
#define EEPROM_CACHE 0x1878e200u
#define EEPROM_LENGTH 1024u

/* 0: native passthrough, 2: native IN-data state, 3: native STALL state.
 * Unknown directions/recipients remain entirely native. D0/D1/D2 are absent
 * from the pinned vendor lookup at 0x5199c, including completion dispatch.
 */
#ifdef STOCK_CLEAR_COUNTERS
/* "clear counters" trigger, armed via build flag clear_counters (OFF by
 * default, no staged candidate enables it). Returns one status byte: the
 * number of counters cleared, or 0xEE if the build is not yet configured to
 * write (see counter_clear.c). A WRITE command; kept off until the safe,
 * checksum-aware path from BRIEF_counters.md is wired in. */
extern uint32_t stockapp_clear_counters(uint32_t selector);
#endif
#ifdef STOCK_INK_OFF
/* "ignore low ink" override, request 0xd5 (BRIEF_ink_off.md): set/clear
 * INK_OFF_BK/CL (+ group-3 mirror) like hold-Cancel. wValue = tank mask
 * (bit0 BK, bit1 CL, 0=both), wIndex = 0 set / non-zero clear. A WRITE. */
extern uint32_t stockapp_set_ink_off(uint32_t mask, uint32_t value);
#endif

uint32_t stockapp_telemetry(struct control *c)
{
    if (c->type != 0xc0 ||
        (c->request != 0xd0 && c->request != 0xd1 && c->request != 0xd2
#ifdef STOCK_CLEAR_COUNTERS
         && c->request != 0xd3
#endif
#ifdef STOCK_INK_OFF
         && c->request != 0xd5
#endif
         ))
        return 0;
    if (!c->length || c->length > 64 || c->buffer != (uint8_t *)0x100129e0)
        return 3;
    const volatile uint8_t *source;
    uint32_t length = c->length;
#ifdef STOCK_CLEAR_COUNTERS
    if (c->request == 0xd3) {
        if (c->index) return 3;                   /* wValue = counter index, 0xffff = all */
        c->buffer[0] = (uint8_t)stockapp_clear_counters(c->value);
        c->remaining = 1;
        return 2;
    }
#endif
#ifdef STOCK_INK_OFF
    if (c->request == 0xd5) {
        /* wValue = tank mask (bit0 BK, bit1 CL, 0=both), wIndex = 0 set / !=0 clear */
        c->buffer[0] = (uint8_t)stockapp_set_ink_off(c->value, c->index ? 0u : 1u);
        c->remaining = 1;
        return 2;
    }
#endif
    if (c->request == 0xd0) {
        if (c->index || c->value >= sizeof(info)) return 3;
        source = (const uint8_t *)info + c->value;
        if (length > sizeof(info) - c->value) length = sizeof(info) - c->value;
    } else if (c->request == 0xd2) {
        /* dump EEPROM working cache: wValue = 24C08 byte offset */
        if (c->index || c->value >= EEPROM_LENGTH) return 3;
        source = (const volatile uint8_t *)(EEPROM_CACHE + c->value);
        if (length > EEPROM_LENGTH - c->value) length = EEPROM_LENGTH - c->value;
    } else {
        if (c->index >= sizeof(regions)/sizeof(regions[0])) return 3;
        const struct region *r = &regions[c->index];
        if (c->value >= r->length || length > r->length - c->value) return 3;
        source = (const volatile uint8_t *)(r->address + c->value);
    }
    for (uint32_t i=0; i<length; ++i) c->buffer[i] = source[i];
    c->remaining = length;
    return 2;
}
