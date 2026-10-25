ENTRY(vectors)
SECTIONS {
    .vectors 0 : { KEEP(*(.vectors)) }
    .vector_api 0x40 : { KEEP(*(.vector_api)) }
    ASSERT(SIZEOF(.vector_api)==24,"exception ABI table size changed")
    .reset 0x100 : { KEEP(*(.reset)) }
    ASSERT(SIZEOF(.reset)<=0x8c,"reset overlaps compatibility entry")
    .warm_recovery 0x18c : { KEEP(*(.warm_recovery)) }
    .warm_normal 0x1ac : { KEEP(*(.warm_normal)) }
    .warm_cache 0x1bc : { KEEP(*(.warm_cache)) }
    .boot 0x200 : { *(.boot.text*) *(.boot.rodata*) }
    ASSERT(.<=0x1800,"boot overlaps compatibility entry")
    .updater_exit 0x1800 : { KEEP(*(.updater_exit)) }
    .text 0x20000000 : AT(0x2000) {
        __ram_start = .; *(.text*) *(.rodata*) *(.data*)
        . = ALIGN(32); __ram_end = .;
    }
    __ram_load = LOADADDR(.text);
    ASSERT(LOADADDR(.text)+SIZEOF(.text)<=0x4000,"recovery loader exceeds reserved 16 KiB")
    .bss (NOLOAD) : {
        __bss_start = .; *(.bss*) *(COMMON)
        . = ALIGN(32); __bss_end = .;
    }
    ASSERT(__bss_end<0x20010000,"recovery RAM overlaps application")
    /DISCARD/ : { *(.comment) *(.note*) *(.ARM.exidx*) *(.ARM.extab*) }
}
