/* Experimental stock handoff: the only stock-specific addresses live here.
 * No firmware flash write and no bootloader source dependency on Canon code.
 */
#include "vectors.h"
extern const volatile struct boot_vector_api mg_boot_vector_api;

bool stockapp_register(void)
{
    const volatile struct boot_vector_api *api=&mg_boot_vector_api;
    static const uint32_t targets[BOOT_VECTOR_COUNT]={
        0x11800,0x11804,0x11808,0x1180c,0x11948,0x11810,0x11000
    };
    if(api->magic!=BOOT_VECTOR_API_MAGIC || api->version!=BOOT_VECTOR_API_VERSION ||
       api->bytes!=sizeof(*api) || api->state_address!=BOOT_VECTOR_STATE_ADDRESS)
        return false;
    uint32_t entry=(uint32_t)(uintptr_t)api->install;
    if(entry<0x200u || entry>=0x1800u || (entry&3u)) return false;
    return api->install(targets);
}
