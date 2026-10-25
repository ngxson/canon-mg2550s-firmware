#include <string.h>
#include "wiring.h"

void wiring_start(struct wiring *w, int64_t now)
{
    *w=(struct wiring){.phase=WIRING_PROBING,.deadline=now+WIRING_PROBE_US};
}

bool wiring_expire(struct wiring *w, int64_t now)
{
    if(w->phase!=WIRING_PROBING || now<w->deadline) return false;
    if(w->exchanges>=2) {
        /* Stage 0 does not implement commands. Repeated exact header requests
         * followed by reads establish routing, not bidirectional CRC success. */
        w->phase=WIRING_VERIFIED; w->proof=WIRING_HEADER_READ;
    } else if(!w->orientation) {
        *w=(struct wiring){.phase=WIRING_PROBING,.orientation=1,
                           .deadline=now+WIRING_PROBE_US};
        return true;
    } else w->phase=WIRING_FAILED;
    return false;
}

void wiring_observe(struct wiring *w, enum wiring_event event, uint32_t value)
{
    if(w->phase!=WIRING_PROBING) return;
    if(event==WIRING_HEADER) w->awaiting_read=true;
    else if(event==WIRING_READ) {
        if(w->awaiting_read) ++w->exchanges;
        w->awaiting_read=false;
    } else if(event==WIRING_RESULT) {
        /* Invalid command zero is rejected in both app and stage 2. A C0
         * result is NOT proof: this probe can never legitimately succeed. */
        if(value==0x90 && w->exchanges) {
            w->phase=WIRING_VERIFIED; w->proof=WIRING_REJECT90;
        }
        w->awaiting_read=false;
    } else if(event!=WIRING_POINTER) w->awaiting_read=false;
}

enum wiring_event wiring_decode(const uint8_t *bytes, unsigned length)
{
    if(length==1 && bytes[0]==0) return WIRING_POINTER;
    if(length!=5) return WIRING_BAD;
    if(bytes[0]==REQUEST_FRAME && get32(bytes+1)==HEADER_REQUEST) return WIRING_HEADER;
    if(bytes[0]==REPORT_RESULT) return WIRING_RESULT;
    return WIRING_BAD;
}

void wiring_probe_reply(uint8_t reply[FRAME_BYTES])
{
    memset(reply,0,FRAME_BYTES);
    put32(reply,HEADER_REQUEST); put32(reply+4,BRIDGE_MAGIC); put32(reply+8,1);
    /* Zero size, CRC and command: no image offer, no valid stay/reboot command. */
    put32(reply+24,OFFER_COMMAND);
    put32(reply+28,recovery_crc32(reply,28));
}
