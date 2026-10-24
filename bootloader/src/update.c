#include "service.h"

static unsigned owner;
static uint8_t status;
static bool app_verified;
#ifndef APPLICATION_BUILD
static uint32_t expected, received, rounded, erase_offset, program_offset, last_activity;
static bool dangerous, reset_blocked;
static uint32_t check_offset, check_crc, sector;
enum update_phase { IDLE, RECEIVE, CHECK_APP, ERASE, PROGRAM, COMMIT, VERIFY_APP,
                    CHECK_RAW, ERASE_RAW, PROGRAM_RAW, VERIFY_RAW };
static enum update_phase phase;
bool raw_flash_erase(uint32_t address);
bool raw_flash_program(uint32_t address,const uint8_t *data);

/* Bounded CRC work keeps USB status and LED polling responsive for 2 MiB.
 * At most 4 KiB per main-loop pass: a whole-image CRC in one pass blocked USB
 * for longer than the host's control-transfer timeout on larger apps. */
static void crc_chunk(const uint8_t *data,uint32_t length)
{
    check_crc=recovery_crc32_update(check_crc,data,length);
    check_offset+=length;
}

/* Continue the payload CRC of the image at `image`; true once finished. */
static bool app_crc_step(const uint8_t *image)
{
    uint32_t length=get32(image+8), left=length-check_offset;
    crc_chunk(image+APP_HEADER+check_offset,left<0x1000u?left:0x1000u);
    return check_offset>=length;
}
#endif

void update_reset(void)
{
#ifndef APPLICATION_BUILD
    /* A class reset cannot hide a failed destructive write. Retry it in RAM. */
    if(reset_blocked) return;
#endif
    owner=0; status=0x80;
#ifndef APPLICATION_BUILD
    phase=IDLE;
#endif
}
unsigned update_owner(void) { return owner; }
/* The last operation was an application update whose flash copy passed the
 * full CRC check. Lets status queries skip recomputing it. */
bool update_app_verified(void) { return app_verified; }
bool update_can_reboot(void)
{
#ifdef APPLICATION_BUILD
    return true;
#else
    return !reset_blocked;
#endif
}
uint8_t update_status(void) { return status; }
void update_status_ack(void) {} /* Reboot is an explicit, acknowledged command. */
bool update_receiving(void)
{
#ifdef APPLICATION_BUILD
    return false;
#else
    return phase==RECEIVE;
#endif
}
void update_abort(uint8_t result)
{
    status=result; owner=0;
#ifndef APPLICATION_BUILD
    phase=IDLE;
#endif
}

bool update_begin(uint32_t length,unsigned transport)
{
#ifdef APPLICATION_BUILD
    (void)length; (void)transport; return false;
#else
    if(owner || reset_blocked || !service_is_bootloader() || length<APP_HEADER || length>APP_CAPACITY) return false;
    dangerous=false; app_verified=false;
    service_hold(); expected=length; received=0; owner=transport;
    status=0x80; phase=RECEIVE; last_activity=time_ms(); return true;
#endif
}

bool update_begin_dangerous(uint32_t length,unsigned transport)
{
#ifdef APPLICATION_BUILD
    (void)length; (void)transport; return false;
#else
    if(owner || !service_is_bootloader() || transport!=OWNER_USB ||
       length!=FLASH_SIZE+DANGEROUS_HEADER) return false;
    service_hold(); dangerous=true; app_verified=false; expected=length; received=0; owner=transport;
    status=0x80; phase=RECEIVE; last_activity=time_ms(); return true;
#endif
}

void update_feed(const uint8_t *data,size_t length)
{
#ifdef APPLICATION_BUILD
    (void)data; (void)length;
#else
    if(phase!=RECEIVE) return;
    if(length>expected-received) { update_abort(0x90); return; }
    memcpy(STAGING+received,data,length); received+=length; last_activity=time_ms();
    if(received!=expected) return;
    if(dangerous) {
        /* Explicit full-chip target, overwrite acknowledgement and two CRCs.
         * No image signature or executable format is required: stock is valid. */
        if(get32(STAGING)!=DANGEROUS_MAGIC || get32(STAGING+4)!=1 ||
           get32(STAGING+8)!=FLASH_SIZE || get32(STAGING+12)!=0 ||
           get32(STAGING+16)!=1 || get32(STAGING+24)!=0 ||
           get32(STAGING+28)!=recovery_crc32(STAGING,28)) {
            update_abort(0x90); return;
        }
        check_offset=0; check_crc=0xffffffff; phase=CHECK_RAW; return;
    }
    /* Header now; the payload CRC runs in update_poll, a piece at a time. */
    uint32_t payload=app_header_length(STAGING,expected);
    if(!payload || payload+APP_HEADER!=expected) { update_abort(0x90); return; }
    check_offset=0; check_crc=0xffffffff; phase=CHECK_APP;
#endif
}

void update_poll(void)
{
#ifndef APPLICATION_BUILD
    if(phase==CHECK_RAW || phase==VERIFY_RAW) {
        bool verifying=phase==VERIFY_RAW;
        const uint8_t *source=verifying?(const uint8_t *)(uintptr_t)check_offset:
            STAGING+DANGEROUS_HEADER+check_offset;
        crc_chunk(source,0x1000);
        if(check_offset<FLASH_SIZE) return;
        if(~check_crc!=get32(STAGING+20)) { update_abort(verifying?0xa4:0x90); return; }
        if(verifying) { reset_blocked=false; update_abort(0xc0); return; }
        if(!flash_identify()) { update_abort(0xa1); return; }
        sector=0x10000; phase=ERASE_RAW;
        return;
    }
    if(phase==ERASE_RAW) {
        reset_blocked=true;
        if(!raw_flash_erase(sector)) { update_abort(0xa2); return; }
        /* Rewrite the reset block last, and its vector-containing page last. */
        program_offset=sector?sector:0x1000; phase=PROGRAM_RAW;
        return;
    }
    if(phase==PROGRAM_RAW) {
        if(!raw_flash_program(program_offset,STAGING+DANGEROUS_HEADER+program_offset)) {
            update_abort(0xa3); return;
        }
        if(!sector && !program_offset) {
            check_offset=0; check_crc=0xffffffff; phase=VERIFY_RAW; return;
        }
        program_offset+=0x1000;
        if(program_offset==sector+0x10000) {
            if(!sector) program_offset=0;
            else { sector+=0x10000; if(sector==FLASH_SIZE) sector=0; phase=ERASE_RAW; }
        }
        return;
    }
    if(phase==RECEIVE && (uint32_t)(time_ms()-last_activity)>=30000) {
        update_abort(0x91); return;
    }
    if(phase==CHECK_APP) {
        if(!app_crc_step(STAGING)) return;
        if(~check_crc!=get32(STAGING+20)) { update_abort(0x90); return; }
        if(!flash_identify()) { update_abort(0xa1); return; }
        rounded=(expected+0xfff)&~0xfffu;
        memset(STAGING+expected,0xff,rounded-expected);
        erase_offset=0; program_offset=0x1000; phase=ERASE;
    } else if(phase==VERIFY_APP) {
        if(!app_crc_step((const uint8_t *)APP_FLASH)) return;
        app_verified=~check_crc==get32((const uint8_t *)APP_FLASH+20);
        update_abort(app_verified?0xc0:0xa4);
    } else if(phase==ERASE) {
        if(!flash_erase(APP_FLASH+erase_offset)) { update_abort(0xa2); return; }
        erase_offset+=0x10000;
        if(erase_offset>=rounded) phase=PROGRAM;
    } else if(phase==PROGRAM) {
        if(program_offset<rounded) {
            if(!flash_program(APP_FLASH+program_offset,STAGING+program_offset)) { update_abort(0xa3); return; }
            program_offset+=0x1000;
        } else phase=COMMIT;
    } else if(phase==COMMIT) {
        if(!flash_program(APP_FLASH,STAGING)) { update_abort(0xa3); return; }
        if(app_header_length((const uint8_t *)APP_FLASH,APP_CAPACITY)+APP_HEADER!=expected) {
            update_abort(0xa4); return;
        }
        check_offset=0; check_crc=0xffffffff; phase=VERIFY_APP;
    }
#endif
}
