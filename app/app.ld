ENTRY(app_entry)
SECTIONS {
    .text 0x20020000 : {
        KEEP(*(.app_entry)) *(.text*) *(.rodata*) *(.data*)
        . = ALIGN(32); __app_end = .;
    }
    .bss (NOLOAD) : {
        __bss_start = .; *(.bss*) *(COMMON)
        . = ALIGN(32); __bss_end = .;
    }
    ASSERT(__bss_end<0x20200000,"application overlaps staging")
    /DISCARD/ : { *(.comment) *(.note*) *(.ARM.exidx*) *(.ARM.extab*) }
}
