ENTRY(app_entry)
/* APP_LOAD (RAM link/load address) and APP_RAM_END (bss bound) come from
 * build.py via --defsym: 0x20020000/0x20420000 standalone, the hole's RAM-copy
 * address for the hybrid build. */
SECTIONS {
    .text APP_LOAD : {
        KEEP(*(.app_entry)) *(.text*) *(.rodata*) *(.data*)
        . = ALIGN(32); __app_end = .;
    }
    .bss (NOLOAD) : {
        __bss_start = .; *(.bss*) *(COMMON)
        . = ALIGN(32); __ordinary_bss_end = .;
    }
    /* USB DMA must bypass the data cache (region 1 ends at 0x20400000).
     * Include this allocation in __bss_end: reset clears it and the hybrid
     * stock_ram_init preserves the entire application interval through it. */
    ASSERT(__ordinary_bss_end<=0x20400000,"application overlaps USB DMA RAM")
    .usb_dma 0x20400000 (NOLOAD) : {
        __usb_dma_start = .; *(.usb_dma)
        . = ALIGN(32); __usb_dma_end = .; __bss_end = .;
    }
    ASSERT(__bss_end<APP_RAM_END,"application overlaps the RAM bound")
    /DISCARD/ : { *(.comment) *(.note*) *(.ARM.exidx*) *(.ARM.extab*) }
}
