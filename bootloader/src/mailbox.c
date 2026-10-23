#include "service.h"
static uint32_t next_probe;

static bool frame(uint32_t offset,uint8_t *reply)
{
    uint8_t request[5]={REQUEST_FRAME}; put32(request+1,offset);
    for(unsigned retry=0;retry<4;++retry) {
        leds_tick(); usb_poll(); (void)time_ms();
        if(!rescue_write(request,sizeof(request))) continue;
        delay_ticks(0x2580);
        if(rescue_read(reply) && get32(reply)==offset &&
           get32(reply+28)==recovery_crc32(reply,28)) return true;
    }
    return false;
}

static void result(uint32_t status)
{
    uint8_t reply[5]={REPORT_RESULT}; put32(reply+1,status);
    (void)rescue_write(reply,sizeof(reply)); rescue_release_bus();
}

void mailbox_poll(void)
{
    uint32_t now=time_ms();
    if(service_busy() || (int32_t)(now-next_probe)<0) return;
    next_probe=now+100;
    uint8_t header[FRAME_BYTES];
    if(!frame(HEADER_REQUEST,header) || get32(header+4)!=BRIDGE_MAGIC || get32(header+8)!=1) return;
    unsigned offer=get32(header+24);
    if(offer==OFFER_COMMAND) {
        unsigned command=get32(header+20);
        bool accepted=command==COMMAND_STAY?service_stay():
            (command>=COMMAND_REBOOT_NORMAL && command<=COMMAND_REBOOT_APPLICATION &&
             service_reboot(command-COMMAND_REBOOT_NORMAL));
        result(accepted?0xc0:0x90);
        if(accepted && command!=COMMAND_STAY) service_reboot_ack();
        return;
    }
    if(offer!=OFFER_APPLICATION) return; /* Stage-0 maintenance is a distinct offer. */
#ifdef APPLICATION_BUILD
    /* Keep the offer armed across a software reset. Stage 0 ignores flag 2,
     * then the USB/I2C bootloader receives the application at its proper slot. */
    if(service_reboot(REBOOT_BOOTLOADER)) service_reboot_ack();
#else
    uint32_t length=get32(header+12), crc=get32(header+16);
    if(!update_begin(length,OWNER_I2C)) { result(0x90); return; }
    uint8_t packet[FRAME_BYTES];
    /* Stage transport first, including the complete-image CRC, before passing
     * bytes to the common flash state machine. */
    for(uint32_t offset=0;offset<length;offset+=FRAME_DATA) {
        if(!frame(offset,packet)) { update_abort(0x91); result(0x91); return; }
        unsigned count=length-offset; if(count>FRAME_DATA) count=FRAME_DATA;
        memcpy(STAGING+offset,packet+4,count);
    }
    if(recovery_crc32(STAGING,length)!=crc) { update_abort(0x90); result(0x90); return; }
    update_feed(STAGING,length);
    while(update_owner()) { leds_tick(); usb_poll(); update_poll(); }
    result(update_status());
#endif
}
