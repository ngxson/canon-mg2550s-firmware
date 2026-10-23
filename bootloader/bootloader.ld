ENTRY(app_entry)
SECTIONS {
    .text 0x20010000 : {
        KEEP(*(.app_entry)) *(.text*) *(.rodata*) *(.data*)
        . = ALIGN(32); __app_end = .;
    }
    .bss (NOLOAD) : {
        __bss_start = .; *(.bss*) *(COMMON)
        . = ALIGN(32); __bss_end = .;
    }
    ASSERT(__bss_end<0x20020000,"bootloader overlaps main application")
    /DISCARD/ : { *(.comment) *(.note*) *(.ARM.exidx*) *(.ARM.extab*) }
}
