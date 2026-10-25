/* CPU-only setup and exception handling. Policy/state are in control.c. */
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
#if defined(STOCK_NATIVE_STARTUP)
    bl stockapp_native_startup
    b .
#elif defined(STOCK_NATIVE_CPU)
    bl stockapp_native_cpu
#endif
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
    mov r0, #0x20
    mov r1, #0
    mov r2, #0
    b smoke_rescue
    .ltorg

.section .text,"ax",%progbits
#ifdef STOCK_IRQ_SNAPSHOT
.global stockapp_irq
stockapp_irq:
    stmdb sp!, {r0-r4,r12,lr}
    mrs r4, cpsr
    bl stockapp_irq_due
    cmp r0, #0
    bne irq_snapshot
    msr cpsr_f, r4
    ldmia sp!, {r0-r4,r12,lr}
    ldr pc, =0x11810
irq_snapshot:
    mov r0, #0xfc
    mov r1, lr
    /* Original exception LR is saved above; BL overwrote the live LR. */
    ldr r1, [sp, #24]
    mrs r2, spsr
    b smoke_rescue
    .ltorg
#endif
.global smoke_svc
smoke_svc:
    stmdb sp!, {r0-r4,r12,lr}
    mrs r4, cpsr
    mrs r2, spsr
    tst r2, #0x20
    ldrhne r0, [lr, #-2]
    bicne r0, r0, #0xff00
    ldreq r0, [lr, #-4]
    biceq r0, r0, #0xff000000
    cmp r0, #0xfe
    beq caught_svc
#ifdef STOCK_RUNTIME
    cmp r0, #0xfd
    beq caught_svc
#endif
    cmp r0, #7
    beq caught_svc
    cmp r0, #8
    beq caught_svc
    msr cpsr_f, r4
    ldmia sp!, {r0-r4,r12,lr}
    ldr pc, =0x11804
caught_svc:
    mov r1, lr
    b smoke_rescue

.macro FAULT name, reason
.global \name
\name:
    mov r0, #\reason
    mov r1, lr
    mrs r2, spsr
    b smoke_rescue
.endm
FAULT smoke_undef, 0x11
FAULT smoke_pabort, 0x12
FAULT smoke_dabort, 0x13
FAULT smoke_reserved, 0x14
FAULT smoke_fiq, 0x15

.global smoke_rescue
smoke_rescue:
    msr cpsr_c, #0xd3
    ldr sp, =0xe0000f00
    bl stockapp_prepare_rescue
    mov r0, #0
    mcr p15, 0, r0, c7, c10, 4
    bx r0
    .ltorg
