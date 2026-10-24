# Tiny Copy-key direction test — 2026-10-24

**All four revisions failed the physical test:** the user repeatedly reported
no visible scanner motion, including revision 4. The earlier app did move
forward. The changes below are a regression, not a successful reverse fix.
Counter deltas and stock's nominal coordinate scale do not establish physical
travel. The user rejected rollback as the task; see [homing](HOMING.md) for the current work.
The remaining sections preserve the experiment history, not the current design.

**Revision 4 (failed):** an insufficient-pulse hypothesis led to another increase.
Fresh stock execution copies `38400` from
configuration `0x18ccb0`, returns it through `f7174`, and runs real line-pitch
arithmetic/division at `fa894`: 300/600/1200 dpi produce 128/64/32 counts per
line. This supports 38,400 counts/inch; it does not calibrate the hardware.
64 counts is nominally 0.0423 mm, and the revision-2 changes were 2–3 microns.
Evidence: [stock execution](tiny_jog_v4/coordinate_scale.json),
[disassembly](tiny_jog_v4/coordinate_scale.txt). Reproduce using
`fw_dump/gpt/coordinate_scale_recheck.py` in `canonemu` from the workspace root.

The app now targets **512 counts** (~0.339 mm nominally) at the same **±120**
drive strength, with a **150 ms total deadline** and **25 ms progress watchdog**.
Only four new counts in the requested direction refresh the watchdog;
oscillation cannot keep the drive active indefinitely. Other aborts, Cancel,
one action per press, cooldown and management polling remain. This is still a
bounded diagnostic move, not a calibrated position controller or home search.

Image: 22,496 bytes, SHA-256
`9fe165d8b488acfd38cb33fd474cf1948cb4cbb085eaa47d0003770dac3afc20`.
All four app test scripts passed before upload, including **17 motor cases**:
frozen feedback stops at 25 ms, advancing feedback still reaches the independent
150 ms deadline, jitter cannot postpone a stall, and a stalled moving motor
stops. Both ±512-count endpoints are tested. Evidence:
[motor](tiny_jog_v4/validation_motor.json),
[management](tiny_jog_v4/validation_app.json),
[manifest](tiny_jog_v4/manifest.json).
Installed with `C0` and valid application INFO via the guarded USB app path;
[upload result](tiny_jog_v4/flash.txt) and
[physical boot log](tiny_jog_v4/boot.log) confirm revision 4. No automatic
motor command was sent during installation. The user subsequently reported
**no moving at all**. The earlier claim that the pulses were simply too small
was not established, especially given the original app's smaller working pulse.

**Revision 3:** after revision 2 still produced no user-visible motion, retain
the ±120 strength but extend the deadline from 8 to 32 ms and reduce the
position guard from 256 to 64 counts. This remains a test, not calibrated
distance control. All four emulator scripts passed before upload, including
14 motor cases with both ±64-count stops. Image: 22,368 bytes, SHA-256
`696c4612b35f69a2dc82c6f8cf426f85b3cc004d6386565f74eed38535b33746`.
Evidence: [motor](tiny_jog_v3/validation_motor.json),
[management](tiny_jog_v3/validation_app.json),
[manifest](tiny_jog_v3/manifest.json).
Installed through guarded USB app upload with `C0`, valid application INFO
and revision-3 marker: [flash](tiny_jog_v3/flash.txt),
[boot log](tiny_jog_v3/boot.log). User again reported **no visible motion**.
Its [physical log](tiny_jog_v3/button_test.log) records +46 counts at the
32,013 us deadline, followed by −64 counts at the 22,942 us travel stop; the
next start was seven counts beyond the previous stop (coast or settling).
A fresh [app/stock setup audit](tiny_jog_v3/stock_setup_audit.json) found no
differences in the selected route, clock, P-init and output registers. That is
a scoped comparison, not proof of complete hardware equivalence.

**Revision 1 below is historical.** The user subsequently pressed the keys
separately and reported no visible motion from either one. The physical
[failure log](tiny_jog/no_motion.log) confirms forward and backward commands,
8 ms deadline stops, position `0x700000` unchanged, and P+304=`3`. Setup readback
matches the intended routes, P+0=`0x200d`, seeded position and P+148/P+308=`1`.
This rules out missing button dispatch and the setup-failure/position-range
abort paths; it does not establish the electrical cause of the lack of motion.

Revision 2 keeps the 8 ms deadline, 256-count limit and all input/recovery guards.
It changes only pulse strength from ±60 to ±120 (ramp ±30/60/90/120, ending at
1880/2120) and adds elapsed microseconds plus C+0/C+8 readback at stop. The
readback takes two MMIO reads before output disable; formatting remains after
disable. Insufficient starting force is a test hypothesis, not a proven cause.
Stock's observed initial positive-home-backoff compare was 2166, so this is
still a smaller command than that trace; electrical torque is not calibrated.

Revision 2 image: 22,368 bytes, SHA-256
`50fdb8c2c93919738d33e5e8c377e636f67a8fcae2cab4bd1d2a42ba85418ecc`.
All four emulator scripts passed again before upload, including 12 motor cases;
both frozen-feedback pulses remain 8.0 modeled ms. Evidence:
[motor](tiny_jog_v2/validation_motor.json),
[management](tiny_jog_v2/validation_app.json),
[manifest](tiny_jog_v2/manifest.json).
The guarded USB app upload returned `C0`, then valid app INFO;
[flash result](tiny_jog_v2/flash.txt) and
[physical boot log](tiny_jog_v2/boot.log) confirm revision 2 is running.
Physical [revision-2 button log](tiny_jog_v2/button_test.log): two B/W commands
reported +3/+4 counts; two Colour commands reported −3/−4. Measured stop times
were 8,012–8,018 microseconds, C+0 read `0x102`, C+8 read 2120/1880. The user
explicitly reported **no visible motion** after this test. Counter changes
alone do not establish visible travel or a reliable encoder. Revision 2 is
superseded by the slightly longer, smaller-count revision-3 experiment above.

## Revision 1 installation and design

Installed through the normal app-only USB path. Bootloader returned `C0`, then
the app enumerated with valid layout-2 INFO. Its physical USB debug log contains
the new Copy-key mapping. No movement command was sent during installation;
physical motion, polarity, distance and encoder behavior remain unverified for
this revision. Both bootloader stages remain protected by the normal uploader.

| Item | Value |
|---|---|
| Image | `app/build/application.bin`, 22,080 bytes |
| SHA-256 | `35dc5360dd68040fa8933be12eec32e89bfe6ed7cddda32f2d58529d0714e6eb` |
| Slot | Flash `0x10000`, RAM `0x20020000` |
| Copy B/W | Forward/right request, C+8 above 2000 |
| Copy Colour | Backward/toward-home request, C+8 below 2000 |
| Pulse | 4 ms ramp, 4 ms hold, maximum excursion 60 |
| Stops | 8 ms elapsed, 256 counts, opposite feedback, completion, Cancel, reboot |
| Repeat | One action per press, 300 ms cooldown; held-at-boot keys ignored |

The 8 ms deadline uses the free-running counter, independently of position
feedback. It is checked by the main loop, not a hardware interrupt. USB remains
serviced; scanner captures and I²C mailbox transactions run between pulses.
The 256-count threshold is an additional guard, not a calibrated distance.

## Stock observations and C implementation

Revision 1's `src/scanner_motor.c` replaced the original blocking jog (9 ms at
its defaults; the earlier description as a long jog was misleading). Stock
flat addresses used as evidence: shared setup `116da6/116eb0/11667a`, P-block
initialization `f7c08/f6982`, comparator arming `f8a5c`, output `f8638/f7696`,
stop `f9060`. The original stock image hash was
`4b032367cff5976aeb426c8723e08d3c431a38e622e245a614791a3e5067e533`.

The attempted stock-derived sequence restores `[0,4,5,6,7,11,10]` routes after the driver
serial transaction, pulses P bit `0x20` on the three motor blocks, writes
`c0c81090=0x3f`, seeds the relative position to `0x700000`, and sets P+148/P+308
to 1. P+0 is `0x200d`, matching the first stock home arm. Serial waits are
bounded and failure leaves outputs disabled. The 1 ms settling delays and tiny
pulse ramp are experimental app choices, not measurements of stock timing.
There are no calls into stock executable code, no stock code blobs, and no new
assembly. The electrical role of several setup registers is still unknown.

Both directions producing the same motion on the previous app is a physical
observation; the earlier claim that an encoder/servo was required to reverse
was not established. This revision tests the more complete setup, and does
not claim that restoring any single register fixes reverse.

## Validation and limits

All four scripts passed in `canonemu` before installation:

- `validate_motor.py`: 12 cases covering both keys without a probe-enable
  command, opposite output polarity, frozen feedback, held/boot-held keys,
  simultaneous keys, Cancel, injected travel/wrong/completion inputs, serial
  timeout, live USB INFO/reboot, I²C rescue reboot, invalid direction.
- `validate_app.py`: app boot/management, debug log, rescue reboot.
- `validate_buttons.py`: optional key logging, idle Cancel illumination,
  forward pulse termination.
- `validate_scanner.py`: existing 3,072-sample synthetic capture regression.

The motor test executes the built ARM image. Its counter advances 0.05 ms per
read, and modeled position stays frozen unless a case explicitly injects
feedback. Both frozen-feedback pulses lasted 8.0 modeled ms; Cancel stopped
after 0.1 modeled ms. It checks that other motor outputs are never enabled and
the first 64 KiB stays byte-identical. It is not a motor/electrical simulation
and cannot prove physical distance, direction, or real-time worst-case latency.

Evidence: [motor report](tiny_jog/validation_motor.json),
[app report](tiny_jog/validation_app.json),
[build manifest](tiny_jog/manifest.json),
[USB installation result](tiny_jog/flash.txt),
[physical boot log](tiny_jog/boot.log).

At the user's follow-up request, the printer was power-cycled and the same
tested image uploaded again via USB. Observed application disconnect at 2.295 s,
bootloader enumeration at 6.425 s, and app enumeration at 12.222 s after starting
the cycle observation. The subsequent app-only upload again returned `C0` and
valid app INFO; its debug log confirmed the mapping. Evidence:
[power-cycle observation](tiny_jog/power_cycle.json),
[USB reflash](tiny_jog/flash_after_power_cycle.txt),
[post-flash boot log](tiny_jog/boot_after_power_cycle_flash.log).
The Python HTTP client initially received 403; the curl request succeeded with
HTTP 200. No motor request was issued during this operation.

That proposed physical check was performed repeatedly and failed. Do not
increase effort or duration again on the assumption that counter changes prove
movement. Recover the earlier working behavior first.
