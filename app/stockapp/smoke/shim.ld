ENTRY(shim_entry)
mg_boot_vector_api = 0x40;
SECTIONS {
    .entry 0x200200f0 : AT(0x10110) { KEEP(*(.text.entry)) }
    ASSERT(SIZEOF(.entry)<=0x100,"entry and ARM veneers exceed reserved gap")
    .text 0x10210 : { *(.text*) *(.rodata*) }
    ASSERT(.<=0x10d00,"smoke callbacks overlap preserved delay helpers")
    /DISCARD/ : { *(.comment) *(.note*) *(.ARM.exidx*) *(.ARM.extab*) }
}
