/* Emulator experiment only. No exception-vector solution is installed here.
 * This assembly is restricted to banked stacks, CPSR and the stock handoff.
 * The app header requires entry zero; build.py inserts a branch to this shim.
 * Stock code remains at its original flash addresses.
 */
.syntax unified
.cpu arm946e-s
.arm
.section .text.entry,"ax",%progbits
.global shim_entry
shim_entry:
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
    .ltorg
