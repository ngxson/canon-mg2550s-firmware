/* Validation remains in flash, so it does not depend on stage-0 RAM surviving
 * an application's startup/BSS initialization. Exception mechanics are in cpu.c.
 */
#include "fw.h"
#include "vectors.h"

#define STATE ((volatile struct boot_vector_state *)BOOT_VECTOR_STATE_ADDRESS)
#define APP_FLASH_BASE 0x10000u
#define APP_PAYLOAD_BASE 0x10020u
#define APP_RAM_BASE 0x20020000u
#define APP_PAYLOAD_MAX (0x200000u-APP_PAYLOAD_BASE)
extern void fault(void);

static BOOT bool span(uint32_t address, uint32_t bytes, uint32_t lo, uint32_t hi)
{ return address>=lo && address<hi && bytes<=hi-address; }

static BOOT bool readable_table(const uint32_t *p)
{
    uint32_t address=(uint32_t)(uintptr_t)p, bytes=BOOT_VECTOR_COUNT*4u;
    return !(address&3u) &&
        (span(address,bytes,APP_PAYLOAD_BASE,FLASH_SIZE) ||
         span(address,bytes,0x18000000u,0x18800000u) ||
         span(address,bytes,0x20000000u,0x20800000u) ||
         span(address,bytes,0xe0000000u,0xe0001000u));
}

static BOOT uint32_t payload_length(void)
{
    uint32_t n=rd(APP_FLASH_BASE+8);
    /* The loader already checked both CRCs before launching an app. Avoid a
     * full CRC pass on every IRQ; bounds/format are rechecked here. */
    if(rd(APP_FLASH_BASE)!=0x5041474du || rd(APP_FLASH_BASE+4)!=2u ||
       !n || (n&3u) || n>APP_PAYLOAD_MAX || rd(APP_FLASH_BASE+12)!=APP_RAM_BASE ||
       rd(APP_FLASH_BASE+16)!=0 || rd(APP_FLASH_BASE+24)!=0) return 0;
    return n;
}

static BOOT bool target_valid(uint32_t target, uint32_t length)
{
    if(!target) return true;
    if((target&3u) || !length) return false;
    return span(target,4,APP_PAYLOAD_BASE,APP_PAYLOAD_BASE+length) ||
           span(target,4,APP_RAM_BASE,APP_RAM_BASE+length);
}

BOOT void boot_vectors_clear(void)
{
    STATE->magic=0;
    STATE->magic_inverse=0;
    STATE->version=0;
    for(unsigned i=0;i<BOOT_VECTOR_COUNT;++i) {
        STATE->target[i]=0;
        STATE->inverse[i]=0;
    }
}

BOOT bool boot_vectors_install(const uint32_t targets[BOOT_VECTOR_COUNT])
{
    /* Failed replacement registration leaves forwarding disabled, never an
     * old/partly updated dispatch table. Masking is checked before table reads.
     * This is an owner-controlled ABI, not a sandbox against hostile code. */
    boot_vectors_clear();
    uint32_t status=cpu_status(), mode=status&0x1fu;
    if((status&0xe0u)!=0xc0u || mode==0x10u || !readable_table(targets)) return false;
    uint32_t length=payload_length();
    if(!length) return false;
    for(unsigned i=0;i<BOOT_VECTOR_COUNT;++i) {
        uint32_t target=targets[i];
        if(!target_valid(target,length)) return false;
        STATE->target[i]=target;
        STATE->inverse[i]=~target;
    }
    STATE->version=BOOT_VECTOR_API_VERSION;
    STATE->magic_inverse=~BOOT_VECTOR_API_MAGIC;
    STATE->magic=BOOT_VECTOR_API_MAGIC;
    return true;
}

BOOT uint32_t boot_vector_target(unsigned index)
{
    if(index>=BOOT_VECTOR_COUNT || STATE->magic!=BOOT_VECTOR_API_MAGIC ||
       STATE->magic_inverse!=~BOOT_VECTOR_API_MAGIC ||
       STATE->version!=BOOT_VECTOR_API_VERSION) return (uint32_t)(uintptr_t)fault;
    uint32_t target=STATE->target[index];
    if(!target || STATE->inverse[index]!=~target || !target_valid(target,payload_length()))
        return (uint32_t)(uintptr_t)fault;
    return target;
}

__attribute__((section(".vector_api"),used))
const struct boot_vector_api boot_vector_api={
    BOOT_VECTOR_API_MAGIC, BOOT_VECTOR_API_VERSION, sizeof(struct boot_vector_api),
    BOOT_VECTOR_STATE_ADDRESS, boot_vectors_install, boot_vectors_clear
};
