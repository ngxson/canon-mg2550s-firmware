/* C driver for the ASIC SPI command engine and its DMA programming engine. */
#include "fw.h"

/* Stage 0 replaces stage 2 inside the reserved first 64 KiB, so it uses
 * W25Q16CV sector erase (20h). Normal app updates retain block erase (D8h). */
#ifdef STAGE0_BUILD
#define ERASE_BYTES 0x1000u
#define ERASE_OPCODE 0x20u
#else
#define ERASE_BYTES 0x10000u
#define ERASE_OPCODE 0xd8u
#endif

static uint32_t wait_done(uint32_t address)
{
    uint32_t budget=100000000;
    uint32_t value;
    do { leds_tick(); value=rd(address); } while(!value && --budget);
    return value;
}

static bool command(uint32_t opcode, uint32_t shape, uint32_t value, bool writing,
                    uint32_t *reply)
{
    wr(SPI+0x34,0x101); wr(SPI+0x14,0); wr(SPI+0x10,2);
    wr(SPI+0x40,opcode); wr(SPI+0x48,shape); wr(SPI+0x4c,value);
    wr(SPI+0x44,writing?1:0); wr(SPI+0x14,0); wr(SPI+0x54,1);
    uint32_t status=wait_done(SPI+0x18);
    /* Reference 0x20003848 reads the reply BEFORE restoring mapped mode.
     * The receive register is not assumed to survive that mode transition.
     * Reference manual commands wait for nonzero completion; the exact ==1
     * success check is specific to its DMA programming path. */
    if(reply) *reply=rd(SPI+0x50);
    wr(SPI+0x10,0); wr(SPI+0x14,0); wr(SPI+0x34,0x100);
    return status!=0;
}

bool flash_identify(void)
{
    uint32_t id=0;
    return command(0x9f,0x300,0,false,&id) && (id&0xffffff)==0x1540ef;
}

bool flash_erase(uint32_t address)
{
    if ((address&(ERASE_BYTES-1)) || address>FLASH_SIZE-ERASE_BYTES) return false;
    if (!command(6,0,0,true,NULL) || !command(ERASE_OPCODE,3,address,true,NULL)) return false;
    /* Uncached memory-mapped flash read-back; every word, not just the first. */
    for(uint32_t i=0;i<ERASE_BYTES;i+=4) if(rd(address+i)!=0xffffffff) return false;
    return true;
}

bool flash_program(uint32_t address, const uint8_t *data)
{
    if ((address&0xfff) || address>FLASH_SIZE-0x1000 || ((uintptr_t)data&3)) return false;
    wr(DMA,(uintptr_t)data); wr(DMA+4,0x1000);
    wr(SPI+0x3c,5); wr(SPI+0x34,0x101); wr(SPI+0x14,0); wr(SPI+0x10,5);
    wr(SPI+0x5c,address); wr(SPI+0x60,0x1000);
    wr(DMA+0x10,0x3d); wr(SPI+0x14,0); wr(SPI+0x58,1);
    uint32_t spi_status=wait_done(SPI+0x18), dma_status=wait_done(DMA+0x14);
    bool ok=spi_status==1 && dma_status==1;
    wr(SPI+0x10,0); wr(SPI+0x14,0); wr(SPI+0x34,0x100); wr(SPI+0x3c,6);
    if(!ok) return false;
    const uint32_t *words=(const uint32_t *)data;
    for(uint32_t i=0;i<0x1000;i+=4) if(rd(address+i)!=words[i/4]) return false;
    return true;
}
