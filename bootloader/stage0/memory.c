#include "fw.h"
void *memcpy(void *dest,const void *source,size_t n)
{ uint8_t *d=dest; const uint8_t *s=source; while(n--) *d++=*s++; return dest; }
void *memset(void *dest,int value,size_t n)
{ uint8_t *d=dest; while(n--) *d++=(uint8_t)value; return dest; }
size_t strlen(const char *s) { const char *p=s; while(*p) ++p; return (size_t)(p-s); }
void delay_ticks(uint32_t count)
{
    wr(0xc0c8200c,0); wr(0xc0c82008,0); wr(0xc0c82010,0); wr(0xc0c8200c,1);
    while(rd(0xc0c82008)<count) {}
}
