#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "wiring.h"

static void exchange(struct wiring *w)
{
    wiring_observe(w,WIRING_HEADER,0);
    wiring_observe(w,WIRING_POINTER,0);
    wiring_observe(w,WIRING_READ,0);
}

int main(void)
{
    struct wiring w={0};
    assert(w.phase!=WIRING_VERIFIED);
    wiring_start(&w,100);
    assert(!wiring_expire(&w,100+WIRING_PROBE_US-1));
    assert(wiring_expire(&w,100+WIRING_PROBE_US));
    assert(w.orientation==1 && !w.exchanges);
    assert(!wiring_expire(&w,100+2*WIRING_PROBE_US));
    assert(w.phase==WIRING_FAILED);
    puts("PASS silence rejects both orientations");

    for(unsigned orientation=0;orientation<2;++orientation) {
        wiring_start(&w,0);
        if(orientation) assert(wiring_expire(&w,WIRING_PROBE_US));
        exchange(&w); wiring_observe(&w,WIRING_RESULT,0x90);
        assert(w.phase==WIRING_VERIFIED && w.proof==WIRING_REJECT90);
        assert(w.orientation==orientation);
    }
    puts("PASS both orientations with rejected-command handshake");

    wiring_start(&w,0); wiring_observe(&w,WIRING_RESULT,0x90);
    wiring_observe(&w,WIRING_READ,0); wiring_observe(&w,WIRING_RESULT,0x90);
    assert(w.phase==WIRING_PROBING && !w.exchanges);
    exchange(&w); wiring_observe(&w,WIRING_RESULT,0xc0);
    assert(w.phase==WIRING_PROBING);
    assert(wiring_expire(&w,WIRING_PROBE_US));
    wiring_observe(&w,WIRING_RESULT,0x90);
    assert(w.phase==WIRING_PROBING && !w.exchanges);
    puts("PASS unsolicited reads/results and stale first-orientation proof rejected");

    wiring_start(&w,0); exchange(&w); exchange(&w);
    assert(w.phase==WIRING_PROBING);
    assert(!wiring_expire(&w,WIRING_PROBE_US));
    assert(w.phase==WIRING_VERIFIED && w.proof==WIRING_HEADER_READ);
    wiring_start(&w,10000000);
    assert(w.phase==WIRING_PROBING && !w.exchanges && !w.proof && !w.orientation);
    puts("PASS stage-0 routing proof and fresh proof after reconnect");

    wiring_start(&w,0);
    for(unsigned i=0;i<100;++i) {
        wiring_observe(&w,WIRING_HEADER,0); wiring_observe(&w,WIRING_BAD,0);
        wiring_observe(&w,WIRING_READ,0);
    }
    assert(!w.exchanges);
    uint8_t request[5]={REQUEST_FRAME,255,255,255,255};
    assert(wiring_decode(request,5)==WIRING_HEADER);
    for(unsigned i=0;i<5;++i) {
        for(unsigned b=0;b<8;++b) {
            request[i]^=1u<<b;
            assert(wiring_decode(request,5)!=WIRING_HEADER);
            request[i]^=1u<<b;
        }
    }
    for(unsigned i=0;i<5;++i) assert(wiring_decode(request,i)==WIRING_BAD);
    puts("PASS malformed traffic cannot establish a header/read exchange");

    uint8_t reply[FRAME_BYTES]; wiring_probe_reply(reply);
    assert(get32(reply)==HEADER_REQUEST && get32(reply+4)==BRIDGE_MAGIC);
    assert(get32(reply+8)==1 && get32(reply+12)==0 && get32(reply+16)==0);
    assert(get32(reply+20)==0 && get32(reply+24)==OFFER_COMMAND);
    assert(get32(reply+28)==recovery_crc32(reply,28));
    puts("PASS probe offers no image or valid management command");
}
