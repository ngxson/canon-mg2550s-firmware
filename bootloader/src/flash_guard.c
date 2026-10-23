#include "fw.h"
#include "protocol.h"
bool raw_flash_erase(uint32_t address);
bool raw_flash_program(uint32_t address,const uint8_t *data);
bool app_flash_erase(uint32_t address)
{ return address>=APP_FLASH && address<=FLASH_SIZE-0x10000 && !(address&0xffff) && raw_flash_erase(address); }
bool app_flash_program(uint32_t address,const uint8_t *data)
{ return address>=APP_FLASH && address<=FLASH_SIZE-0x1000 && !(address&0xfff) && raw_flash_program(address,data); }
