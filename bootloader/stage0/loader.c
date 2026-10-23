/* Independent reset-time recovery, executed before any USB/application code. */
#include "fw.h"
#include "protocol.h"
bool rescue_read(uint8_t *data);
bool rescue_write(const uint8_t *data,size_t length);
void rescue_release_bus(void);
bool app_flash_erase(uint32_t address);
bool app_flash_program(uint32_t address,const uint8_t *data);

static bool frame(uint32_t offset,uint8_t *reply)
{
    uint8_t request[5]={REQUEST_FRAME}; put32(request+1,offset);
    for(unsigned retry=0;retry<4;++retry) {
        leds_tick();
        if(!rescue_write(request,sizeof(request))) continue;
        delay_ticks(0x2580);
        if(!rescue_read(reply)) continue;
        if(get32(reply)==offset && get32(reply+28)==recovery_crc32(reply,28)) return true;
    }
    return false;
}

static void result(uint32_t status)
{
    uint8_t request[5]={REPORT_RESULT}; put32(request+1,status);
    (void)rescue_write(request,sizeof(request));
    rescue_release_bus();
}

/* No operation accepts a caller-controlled destination: only stage 2's slot.
 * Erase 4 KiB sectors so neither the first stage nor main app is touched. */
static uint32_t program_application(uint32_t length)
{
    uint32_t rounded=(length+0xfff)&~0xfffu;
    memset(STAGING+length,0xff,rounded-length);
    if(!flash_identify()) return 0xa1;
    for(uint32_t offset=0;offset<rounded;offset+=0x1000) {
        leds_tick();
        if(!app_flash_erase(APP_FLASH+offset)) return 0xa2;
    }
    /* Commit the first block (which contains the valid header) last. A reset
     * during any earlier erase/program leaves an invalid app, so loader stays. */
    for(uint32_t offset=0x1000;offset<rounded;offset+=0x1000) {
        leds_tick();
        if(!app_flash_program(APP_FLASH+offset,STAGING+offset)) return 0xa3;
    }
    if(!app_flash_program(APP_FLASH,STAGING)) return 0xa3;
    return app_valid((const uint8_t *)APP_FLASH,APP_CAPACITY)?0xc0:0xa4;
}

static bool receive_application(const uint8_t *header)
{
    uint32_t length=get32(header+12), checksum=get32(header+16);
    if(get32(header+4)!=BRIDGE_MAGIC || get32(header+8)!=1 ||
       get32(header+24)!=1 || length<APP_HEADER || length>APP_CAPACITY) {
        result(0x90); return false;
    }
    uint8_t packet[FRAME_BYTES];
    for(uint32_t offset=0;offset<length;offset+=FRAME_DATA) {
        if(!frame(offset,packet)) { result(0x91); return false; }
        unsigned n=length-offset; if(n>FRAME_DATA) n=FRAME_DATA;
        memcpy(STAGING+offset,packet+4,n);
    }
    if(recovery_crc32(STAGING,length)!=checksum || !app_valid(STAGING,length) ||
       get32(STAGING+8)+APP_HEADER!=length) { result(0x90); return false; }
    uint32_t status=program_application(length);
    result(status);
    return status==0xc0;
}

static __attribute__((noreturn)) void launch_application(void)
{
    const uint8_t *image=(const uint8_t *)APP_FLASH;
    rescue_release_bus();
    memcpy((void *)APP_RAM,image+APP_HEADER,get32(image+8));
    ((void (*)(void))APP_RAM)();
    for(;;) leds_tick();
}

void firmware_main(void)
{
    panel_init();
    bool valid=app_valid((const uint8_t *)APP_FLASH,APP_CAPACITY);
    uint8_t packet[FRAME_BYTES];
    for(;;) {
        leds_tick();
        bool armed=frame(HEADER_REQUEST,packet) && get32(packet+4)==BRIDGE_MAGIC &&
                   get32(packet+8)==1 && get32(packet+24)==1;
        if(armed) {
            /* A failed attempted rescue must not fall through to an old app. */
            valid=false;
            if(receive_application(packet)) launch_application();
        } else if(valid) launch_application();
        delay_ticks(0x2580);
    }
}
