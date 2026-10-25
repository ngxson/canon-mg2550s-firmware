ENTRY(shim_entry)
mg_boot_vector_api = 0x40;
SECTIONS {
    .text 0x200200f0 : { KEEP(*(.text.entry)) *(.text*) *(.rodata*) }
    /DISCARD/ : { *(.comment) *(.note*) *(.ARM.exidx*) *(.ARM.extab*) }
}
