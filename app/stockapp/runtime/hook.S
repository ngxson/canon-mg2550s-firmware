/* Interworking/register preservation and replay of two displaced instructions.
 * Request policy lives in management.c. Called after the stock function saved
 * its caller LR and callee-saved registers at 0x126b0. */
.syntax unified
.cpu arm946e-s
.thumb
.section .text,"ax",%progbits
.align 2
.global stockapp_event_hook
.thumb_func
stockapp_event_hook:
    push {r0-r3,r5,lr}
    movs r0, r3
    ldr r5, =stockapp_management_event
    blx r5
    cmp r0, #0
    bne management_rescue
    pop {r0-r3,r5}
    pop {r4}
    mov lr, r4
    /* Original 0x126b2..0x126b5; SP is again the stock prologue's SP. */
    movs r4, r2
    ldr r2, [sp, #24]
    bx lr
management_rescue:
    svc #0xfd
    b .
    .ltorg

#ifdef STOCK_AUTO_POWER
.align 2
.global stockapp_power_key
.thumb_func
stockapp_power_key:
    push {r2-r4,lr}
    mov r4, sp
    mov r0, sp
    movs r2, #7
    bics r0, r2
    mov sp, r0
    ldr r3, =stockapp_power_sample
    blx r3
    movs r1, r0
    mov sp, r4
    pop {r2-r4,pc}
    .ltorg
#endif
