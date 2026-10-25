/* Thumb/ARM interworking and stock register ABI only; policy is C.
 * Replaces the BL 0x5199c at 0x51bf6, after normal SETUP acquisition.
 * Stock dispatch uses r6 as its result and accepts 15 as unknown vendor ID.
 * For a handled IN request its unknown-ID branch returns r6 unchanged.
 * Other requests tail-call the original lookup with original r0/r1/r2.
 */
.syntax unified
.cpu arm946e-s
.thumb
.section .text,"ax",%progbits
.align 2
.global stockapp_telemetry_hook
.thumb_func
stockapp_telemetry_hook:
    push {r0-r5,r7,lr}
    mov r4, sp
    mov r0, sp
    movs r1, #7
    bics r0, r1
    mov sp, r0
    ldr r0, [r4]
    ldr r3, =stockapp_telemetry
    blx r3
    cmp r0, #0
    beq passthrough
    movs r6, r0
    mov sp, r4
    pop {r0-r5,r7}
    pop {r3}
    mov lr, r3
    movs r0, #15
    bx lr
passthrough:
    mov sp, r4
    pop {r0-r5,r7}
    pop {r3}
    mov lr, r3
    ldr r3, =0x5199d
    bx r3
    .ltorg
