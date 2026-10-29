/* Ink / paper field interception for app/patch (requests #1, #2, paper-count).
 *
 * Corrected per fw_dump/gpt/BRIEF_ink_status.md (answer):
 *   - inkLevel_0..3 are ids 0x0103109c..0x0103109f (module 1 / group 3),
 *     NOT module 2 / group 0x1c.  inkLevel is stored/reported as unsigned
 *     percent, so "full" = 100 (INK_FULL).
 *   - The report/status code reads inkLevel through a SECOND field API
 *     (registry object 0x1878edf4 -> accessor 0x10d246), not the vtable
 *     helpers 0x111eea/0x111ed4 hooked here.  So these vtable veneers only
 *     cover vtable-family consumers; the service report IL(...) is clamped
 *     directly at its formatter by il_clamp_veneer (STOCK_INK_REPORT), and the
 *     CTK: / MarkerSupplyAttention status is handled by byte patches in
 *     build.py.  Write-suppression here is a hypothesis pending a print-time
 *     check of the real decrement path.
 *
 * All code runs at virtual == flash-offset (shim.ld .text @ 0x10210), the same
 * space stock runs in, so the absolute stock addresses below are real runtime
 * addresses.  INK_FULL is a build-time value (default 100).
 */
.syntax unified
.cpu arm946e-s
.thumb

#ifndef INK_FULL
#define INK_FULL 100
#endif

#if defined(STOCK_INK_CLAMP) || defined(STOCK_INK_REPORT)

.section .text,"ax",%progbits
.align 2

/* inkLevel id range test helper is inlined below:
 *   ink  <=>  (id - 0x0103109c) <= 3  (unsigned)  i.e. id in 0x0103109c..9f */

#ifdef STOCK_INK_CLAMP
/* Installed over the vtable getter's `bl 0x111a14` at 0x111eee.
 * On entry: r0=r4=id (movs r4,r0 already ran), lr=0x111ef3, stack={r4,lr}. */
.global ink_getter_veneer
.thumb_func
ink_getter_veneer:
    ldr     r1, =0x0103109c
    subs    r2, r4, r1           @ r2 = id - base
    cmp     r2, #3               @ unsigned: in inkLevel_0..3 ?
    bhi     1f
    ldr     r0, =INK_FULL        @ ink-level read -> full
    pop     {r4, pc}             @ return to caller with r0 = INK_FULL
1:
    ldr     r3, =0x111a15        @ replay displaced bl 0x111a14 (thumb)
    blx     r3                   @ r0 = resolved field object (as original)
    ldr     r3, =0x111ef3        @ continue original getter at 0x111ef2
    bx      r3
    .ltorg

/* Installed over the vtable setter's `bl 0x111a14` at 0x111eda.
 * On entry: r5=id, r0=id, r4=value, lr=0x111edf, stack={r4,r5,r6,lr}. */
.global ink_setter_veneer
.thumb_func
ink_setter_veneer:
    ldr     r1, =0x0103109c
    subs    r2, r5, r1
    cmp     r2, #3               @ inkLevel_0..3 write ?
    bls     3f                   @ yes -> drop
#ifdef STOCK_PAGE_COUNT
    ldr     r1, =0x0105029a      @ ASF PAGE "All" printed-sheet counter
    cmp     r5, r1
    beq     3f                   @ page-count write -> drop
#endif
    b       2f
3:
    pop     {r4, r5, r6, pc}     @ drop write, return to caller
2:
    ldr     r3, =0x111a15        @ replay displaced bl 0x111a14
    blx     r3
    ldr     r3, =0x111edf        @ continue original setter at 0x111ede
    bx      r3
    .ltorg
#endif /* STOCK_INK_CLAMP */

#ifdef STOCK_INK_REPORT
/* Installed over `movs r3,#1; movs r2,#0x50` at 0xbc7f2 in the IL(...) service
 * formatter 0xbc7b6.  At this point r5 = sp+0x20 = the 4 per-tank value bytes
 * just read; force them all to 100 (percent), then restore the two displaced
 * immediates the following `bl 0xbf5d0` needs.  r0 is dead here (reloaded at
 * 0xbc7f6), lr is not live across the call. */
.global il_clamp_veneer
.thumb_func
il_clamp_veneer:
    movs    r0, #INK_FULL
    strb    r0, [r5, #0]
    strb    r0, [r5, #1]
    strb    r0, [r5, #2]
    strb    r0, [r5, #3]
    movs    r3, #1               @ restore displaced `movs r3,#1`
    movs    r2, #0x50            @ restore displaced `movs r2,#0x50`
    bx      lr
    .ltorg
#endif /* STOCK_INK_REPORT */

#endif /* STOCK_INK_CLAMP || STOCK_INK_REPORT */
