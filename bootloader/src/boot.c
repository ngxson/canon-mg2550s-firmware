#include "fw.h"
struct setting { uint32_t address, value; };
#include "board_values.h"
extern uint32_t __ram_load[], __ram_start[], __ram_end[], __bss_start[], __bss_end[];
void cpu_quiesce(void);

static BOOT void settings(const struct setting *s, size_t count)
{ for(size_t i=0;i<count;++i) wr(s[i].address,s[i].value); }

static BOOT void boot_delay(uint32_t count)
{
    wr(0xc0c8200c,0); wr(0xc0c82008,0); wr(0xc0c82010,0); wr(0xc0c8200c,1);
    while(rd(0xc0c82008)<count) {}
}

static BOOT void boot_spi(uint32_t opcode, uint32_t shape, uint32_t value, bool writing)
{
    wr(SPI+0x10,2); wr(SPI+0x40,opcode); wr(SPI+0x44,writing?1:0);
    wr(SPI+0x48,shape); wr(SPI+0x4c,value); wr(SPI+0x14,0); wr(SPI+0x54,1);
    while((rd(SPI+0x14)&3)!=2) {}
    wr(SPI+0x14,0);
}

BOOT void boot_c(void)
{
    cpu_quiesce();
    modify(0xc0101000,3,0);
    settings(early_pads,ARRAY_SIZE(early_pads));
    while(rd(0xc0c50218)&1) {}
    boot_delay(0xea600);
    settings(flash_pads,ARRAY_SIZE(flash_pads));

    /* This board specifically has EF 40 15 (W25Q16). Configure the observed
     * two-byte status write/quad-read transaction, without embedding a driver. */
    boot_spi(0x9f,0x300,0,false);
    if ((rd(SPI+0x50)&0xffffff)!=0x1540ef) for (;;) {}
    boot_spi(6,0,0,false); boot_spi(1,2,2,true);
    wr(SPI+0x68,0x32bbeb); wr(SPI+0x6c,0x311);
    wr(SPI+0x38,0xa); wr(SPI+0x3c,6); wr(SPI+0x28,1);

    modify(0xc0101000,3,0);
    wr(0xc0100000,0x10001); boot_delay(0x2580);
    wr(0xc0100004,0x2143); boot_delay(0x2580);
    modify(0xc0100000,0x10001,0);
    do { boot_delay(0x17700); } while((rd(0xc0100000)&0x80008000)!=0x80008000);
    modify(0xc0101000,0,2);
    settings(clock_stage1,ARRAY_SIZE(clock_stage1)); boot_delay(0x46500);
    settings(clock_stage2,ARRAY_SIZE(clock_stage2));
    settings(memory_bus,ARRAY_SIZE(memory_bus));
    while(!(rd(0xc0c82404)&1)) {}
    settings(board_pins,ARRAY_SIZE(board_pins));
    wr(0xc0c89000,1); wr(0xc0c89034,1); wr(0xc0c89040,1);

    /* No library or external-RAM stack is needed until this copy completes.
     * Volatile prevents replacement by a RAM-resident memcpy/memset call. */
    volatile uint32_t *dest=__ram_start;
    const volatile uint32_t *src=__ram_load;
    while(dest<__ram_end) *dest++=*src++;
    dest=__bss_start;
    while(dest<__bss_end) *dest++=0;
}
