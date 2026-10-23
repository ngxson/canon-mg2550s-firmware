/* Printer MCU as master, ESP32 as slave. Controller evidence: stock RAM
 * 0x20006eb8..0x200071c4. No EEPROM address is used by this driver. */
#include "fw.h"
#include "protocol.h"
#define I2C 0xc0b20000u

static bool clear_with_timeout(uint32_t address,uint32_t bits)
{
    for(unsigned budget=0;budget<50000;++budget) {
        if(!(rd(address)&bits)) return true;
        leds_tick();
    }
    return false;
}

static bool setup(void)
{
    wr(I2C+0x1c,1);
    if(!clear_with_timeout(I2C+0x1c,1)) return false;
    wr(I2C+0x20,0x118); wr(I2C+0x30,0x16); wr(I2C+0x24,1);
    wr(I2C+0x108,0x140016); wr(I2C+0x130,6); wr(I2C+0x12c,0x8c);
    wr(I2C+0x100,0); wr(I2C+0x104,0x47e); wr(I2C+0x2c,1);
    return true;
}

static bool complete(void)
{
    for(unsigned budget=0;budget<50000;++budget) {
        uint32_t status=rd(I2C+0x100);
        /* Do not treat bit 6 as a transaction error: the reference checks it
         * only before draining RX (consistent with RX-empty). TX has no RX.
         * NACK/error fields remain undecoded; timeout and framed CRC are used. */
        if(status&1) return true;
        leds_tick();
    }
    return false;
}

bool rescue_write(const uint8_t *data,size_t length)
{
    if(!length || length>30 || !setup()) return false;
    wr(I2C+0x10c,RESCUE_ADDRESS); wr(I2C+0x114,0);
    for(size_t i=0;i<length;++i) {
        if(rd(I2C+0x100)&2) return false;
        wr(I2C+0x120,data[i]);
    }
    wr(I2C+0x118,1);
    return complete();
}

bool rescue_read(uint8_t *data)
{
    if(!setup()) return false;
    wr(I2C+0x10c,RESCUE_ADDRESS); wr(I2C+0x114,0);
    wr(I2C+0x120,0); /* Mailbox register pointer, ignored by the ESP32. */
    wr(I2C+0x110,FRAME_BYTES-1); wr(I2C+0x114,1); wr(I2C+0x118,1);
    if(!complete()) return false;
    for(unsigned i=0;i<FRAME_BYTES;++i) {
        if(rd(I2C+0x100)&0x40) return false;
        data[i]=(uint8_t)rd(I2C+0x11c);
    }
    return true;
}

void rescue_release_bus(void)
{
    /* Reset leaves no command pending. No GPIO push-pull drive is configured. */
    wr(I2C+0x1c,1); (void)clear_with_timeout(I2C+0x1c,1);
}
