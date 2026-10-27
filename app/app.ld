ENTRY(app_entry)
/* APP_LOAD (RAM link/load address) and APP_RAM_END (bss bound) come from
 * build.py via --defsym: 0x20020000/0x20200000 standalone, the hole's RAM-copy
 * address for the hybrid build. */
SECTIONS {
    .text APP_LOAD : {
        KEEP(*(.app_entry)) *(.text*) *(.rodata*) *(.data*)
        . = ALIGN(32); __app_end = .;
    }
    .bss (NOLOAD) : {
        __bss_start = .; *(.bss*) *(COMMON)
        . = ALIGN(32); __bss_end = .;
    }
    ASSERT(__bss_end<APP_RAM_END,"application overlaps the RAM bound")
    /DISCARD/ : { *(.comment) *(.note*) *(.ARM.exidx*) *(.ARM.extab*) }
}
