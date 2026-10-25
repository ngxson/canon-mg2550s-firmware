/* Keep stock's USB input/parser tasks running, but reinterpret its explicit
 * firmware-update request as entry into OUR held rescue. No stock flash writer
 * is entered. This is an intermediate dependency on the stock USB/BJL parser. */
#include <stdint.h>
#include <stdbool.h>
bool stockapp_management_event(uint32_t event)
{
    return event==0x30007u; /* @FROMUPMODE / XML FRomUpMode */
}
