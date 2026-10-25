/* Bounded stock-runtime startup observation. No stock updater or flash writer.
 * All callbacks execute from the app's flash slot so stock RAM scatter loading
 * cannot overwrite the recovery code. The fixed SRAM record survives reset.
 */
#include "vectors.h"
#define WORD(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define RECORD 0xe00008d0u
#define MAGIC 0x31504d53u /* SMP1 */
extern const volatile struct boot_vector_api mg_boot_vector_api;
void smoke_svc(void), smoke_undef(void), smoke_pabort(void), smoke_dabort(void);
void smoke_reserved(void), smoke_fiq(void);
#ifdef STOCK_IRQ_SNAPSHOT
void stockapp_irq(void);
unsigned stockapp_irq_due(void)
{ return ++WORD(0xe00008f0u)>=STOCK_IRQ_SNAPSHOT; }
#endif

static void record(uint32_t stage, uint32_t reason, uint32_t lr, uint32_t psr)
{
    WORD(RECORD)=0;
    WORD(RECORD+4)=~MAGIC;
    WORD(RECORD+8)=stage; WORD(RECORD+12)=~stage;
    WORD(RECORD+16)=reason; WORD(RECORD+20)=~reason;
    WORD(RECORD+24)=lr; WORD(RECORD+28)=psr;
    WORD(RECORD)=MAGIC;
}

bool stockapp_register(void)
{
    record(1,0,0,0);
#ifdef STOCK_AUTO_POWER
    WORD(0xe00008f4u)=0;
#endif
#ifdef STOCK_IRQ_SNAPSHOT
    WORD(0xe00008f0u)=0;
#endif
    const volatile struct boot_vector_api *api=&mg_boot_vector_api;
    if(api->magic!=BOOT_VECTOR_API_MAGIC || api->version!=1 || api->bytes!=24 ||
       api->state_address!=BOOT_VECTOR_STATE_ADDRESS) return false;
    uint32_t entry=(uint32_t)(uintptr_t)api->install;
    if(entry<0x200u || entry>=0x1800u || (entry&3u)) return false;
    const uint32_t targets[BOOT_VECTOR_COUNT]={
        (uint32_t)(uintptr_t)smoke_undef, (uint32_t)(uintptr_t)smoke_svc,
        (uint32_t)(uintptr_t)smoke_pabort, (uint32_t)(uintptr_t)smoke_dabort,
        (uint32_t)(uintptr_t)smoke_reserved,
#ifdef STOCK_IRQ_SNAPSHOT
        (uint32_t)(uintptr_t)stockapp_irq,
#else
        0x11810,
#endif
        (uint32_t)(uintptr_t)smoke_fiq
    };
    return api->install(targets);
}

void stockapp_prepare_rescue(uint32_t reason,uint32_t lr,uint32_t psr)
{
#ifdef STOCK_IRQ_SNAPSHOT
    /* Compact record: preserve interrupted LR and capture both physical USB
     * gates before disconnecting. PHY low 17 bits; core DCTL low 15 bits. */
    if(reason==0xfcu) {
        lr=WORD(0x1804099cu); /* USB object state, mode, initialized, attached */
        psr=(WORD(0xc0c88024u)&0x1ffffu)|((WORD(0xc0780804u)&0x7fu)<<17)
            |((WORD(0x1803fcc4u)&0xffu)<<24); /* native USB initialization error */
    }
#endif
    record(2,reason,lr,psr);
    /* Source bootloader's checked, one-shot request for held rescue mode. */
    WORD(0xe0000800)=0;
    WORD(0xe0000804)=1;
    WORD(0xe0000808)=~0x4d474252u;
    WORD(0xe000080c)=~1u;
    WORD(0xe0000800)=0x4d474252u;
    /* Same disconnect bits as the source USB driver; no FIFO or DMA work. */
    WORD(0xc0780804)=WORD(0xc0780804)|2u;
    WORD(0xc0c88024)=WORD(0xc0c88024)&~0x10000u;
}
