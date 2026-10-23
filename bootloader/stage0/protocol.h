/* Shared, source-defined recovery wire format. All integers little-endian. */
#ifndef RECOVERY_PROTOCOL_H
#define RECOVERY_PROTOCOL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define RESCUE_ADDRESS 0x42u
#define APP_VERSION 2u
#define APP_FLASH 0x4000u
#define APP_CAPACITY (0x10000u-APP_FLASH)
#define APP_RAM 0x20010000u
#define APP_HEADER 32u
#define APP_MAGIC 0x5041474du /* MGAP */
#define BRIDGE_MAGIC 0x31435352u /* RSC1 */
#define FRAME_BYTES 32u
#define FRAME_DATA 24u
#define HEADER_REQUEST 0xffffffffu
#define REQUEST_FRAME 0x52u
#define REPORT_RESULT 0x44u
static inline uint32_t get32(const uint8_t *p)
{ return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24); }
static inline void put32(uint8_t *p,uint32_t x)
{ for(unsigned i=0;i<4;++i) p[i]=(uint8_t)(x>>(i*8)); }
uint32_t recovery_crc32(const void *data,size_t length);
bool app_valid(const uint8_t *image,size_t available);
#endif
