#include "protocol.h"

/* CRC-32/ISO-HDLC, same result as Python zlib.crc32. Integrity, not a signature.
 * The update form takes and returns the running register (start 0xffffffff,
 * invert at the end) so long images can be checked a bounded piece at a time. */
uint32_t recovery_crc32_update(uint32_t crc,const void *data,size_t length)
{
    const uint8_t *p=data;
    while(length--) {
        crc^=*p++;
        for(unsigned i=0;i<8;++i) crc=(crc>>1)^((0u-(crc&1u))&0xedb88320u);
    }
    return crc;
}

uint32_t recovery_crc32(const void *data,size_t length)
{ return ~recovery_crc32_update(0xffffffff,data,length); }

/* Header fields and header CRC only: the payload length, or 0 if invalid. */
uint32_t app_header_length(const uint8_t *p,size_t available)
{
    if(available<APP_HEADER || get32(p)!=APP_MAGIC || get32(p+4)!=APP_VERSION) return 0;
    uint32_t length=get32(p+8);
    if(!length || (length&3) || length>APP_CAPACITY-APP_HEADER || length>available-APP_HEADER) return 0;
    if(get32(p+12)!=APP_RAM || get32(p+16)!=0 || get32(p+24)!=0) return 0;
    if(get32(p+28)!=recovery_crc32(p,28)) return 0;
    return length;
}

bool app_valid(const uint8_t *p,size_t available)
{
    uint32_t length=app_header_length(p,available);
    return length && get32(p+20)==recovery_crc32(p+APP_HEADER,length);
}
