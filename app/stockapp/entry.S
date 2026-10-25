/* CPU-only assembly: privileged registration, banked stacks, stock handoff.
 * Driver/RTOS code remains proprietary stock; this is an observation candidate.
 */
.syntax unified
.cpu arm946e-s
.arm
.section .text.entry,"ax",%progbits
.global shim_entry
shim_entry:
    msr cpsr_c, #0xd3
    ldr sp, =0x207ff000
    bl stockapp_register
    cmp r0, #1
    bne registration_failed
    msr cpsr_c, #0xd1
    ldr sp, =0x187dbffc
    msr cpsr_c, #0xd2
    ldr sp, =0x187dbefc
    msr cpsr_c, #0xd3
    ldr sp, =0xe0000efc
    msr cpsr_c, #0x10
    ldr sp, =0x187dbdfc
    ldr r0, =0x11960
    bx r0
registration_failed:
    b registration_failed
    .ltorg
