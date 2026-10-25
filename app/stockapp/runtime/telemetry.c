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
static const struct region regions[] = {
    {0x1804099c, 4},       /* USB state, mode, initialized, attached */
    {0x1803fcc4, 8},       /* error bytes, message and SETUP history indices */
    {0x1803fccc, 1024},    /* SETUP first four bytes, 256 entries */
    {0x180400cc, 1024},    /* SETUP last four bytes, 256 entries */
    {0x180404cc, 2},       /* filtered message ring: write index, processed count */
    {0x180404ce, 512},     /* all message IDs, 256 x u16 */
    {0x180406ce, 512},     /* filtered message IDs, 256 x u16 */
};
/* Little endian: STL1, ABI, maximum transfer, region count, then region pairs.
 * ABI 2 is 72 bytes, so the host reads it with wValue as the byte offset.
 * Addresses are diagnostic metadata, never accepted from the USB requester.
 */
static const uint32_t info[] = {
    0x314c5453, 2, 64, 7,
    0x1804099c, 4, 0x1803fcc4, 8,
    0x1803fccc, 1024, 0x180400cc, 1024,
    0x180404cc, 2, 0x180404ce, 512, 0x180406ce, 512,
};

/* 0: native passthrough, 2: native IN-data state, 3: native STALL state.
 * Unknown directions/recipients remain entirely native. D0/D1 are absent
 * from the pinned vendor lookup at 0x5199c, including completion dispatch.
 */
uint32_t stockapp_telemetry(struct control *c)
{
    if (c->type != 0xc0 || (c->request != 0xd0 && c->request != 0xd1))
        return 0;
    if (!c->length || c->length > 64 || c->buffer != (uint8_t *)0x100129e0)
        return 3;
    const volatile uint8_t *source;
    uint32_t length = c->length;
    if (c->request == 0xd0) {
        if (c->index || c->value >= sizeof(info)) return 3;
        source = (const uint8_t *)info + c->value;
        if (length > sizeof(info) - c->value) length = sizeof(info) - c->value;
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
