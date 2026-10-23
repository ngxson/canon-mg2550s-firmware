#include "protocol.h"

/* CRC-32/ISO-HDLC, same result as Python zlib.crc32. Integrity, not a signature. */
uint32_t recovery_crc32(const void *data,size_t length)
{
    const uint8_t *p=data;
    uint32_t crc=0xffffffff;
    while(length--) {
        crc^=*p++;
        for(unsigned i=0;i<8;++i) crc=(crc>>1)^((0u-(crc&1u))&0xedb88320u);
    }
    return ~crc;
}

bool app_valid(const uint8_t *p,size_t available)
{
    if(available<APP_HEADER || get32(p)!=APP_MAGIC || get32(p+4)!=APP_VERSION) return false;
    uint32_t length=get32(p+8);
    if(!length || (length&3) || length>APP_CAPACITY-APP_HEADER || length>available-APP_HEADER) return false;
    if(get32(p+12)!=APP_RAM || get32(p+16)!=0 || get32(p+24)!=0) return false;
    if(get32(p+28)!=recovery_crc32(p,28)) return false;
    return get32(p+20)==recovery_crc32(p+APP_HEADER,length);
}
