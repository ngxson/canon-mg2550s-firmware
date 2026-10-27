# Continuous colour scanner repair — 2026-10-27

**Installed and physically verified at 75, 150 and 300 dpi.** Three full-page
RGB scans completed consecutively after reboot using ordinary eSCL requests,
without a cache debug command. Each returned home in DONE with scanner power,
motor drive and acquisition gate zero. Final USB management was responsive
and both RAM canary arrays were intact.

| DPI | RGB pixels | Image download | Including return home |
|---|---|---|---|
| 75 | 620 × 876 | 12.87 s | 22.44 s |
| 150 | 1240 × 1753 | 19.12 s | 28.69 s |
| 300 | 2480 × 3507 | 91.15 s | 100.72 s |

The 300 dpi page ran first and includes initial homing. These are host wall
measurements, not claims of stock-equivalent speed.

- Images: [75 dpi](../../../scans/scan_cache_rgb75.png),
  [150 dpi](../../../scans/scan_cache_rgb150.png),
  [300 dpi](../../../scans/scan_cache_rgb300.png).
- [Physical results](hardware/result.json), [final idle audit](hardware/idle.json),
  [host log](hardware_check.log), [verified upload](flash.log).
- Exact package: `app/hybrid/build/scan_cache_verified/application.bin`.
  SHA-256 `fa27e14a23246f8f2651a6446c671c0bea895775f05b5fe008c407c18199b0a4`.
  Normal hybrid and standalone build outputs were also refreshed; their older
  outputs were preserved in `before_scan_cache_*` subdirectories.

The ordinary application updater reported verified C0 completion; the two
protected boot stages were retained. This remains hybrid firmware. These
changes add no stock dependencies; the scanner logic is readable source.

## What was fixed

1. **MPU setup ordering.** Data-cache regions now receive permissions and
   attributes before being enabled. The old order temporarily denied access
   to the current code/stack. This is consistent with the rebuild-sensitive
   first-ScanJobs freezes. The [negative regression](mpu_order_before.log)
   catches the exact intermediate loss of code access; the final compiled
   sequence preserves access after every register write.
2. **Cache state and USB DMA ownership.** I-cache setup clears inherited
   overriding regions. Mutable USB DMA buffers occupy uncached
   `0x20400000..0x20408740`; `__bss_end` includes them so reset initializes
   them and stock RAM initialization preserves them. Write-through cache
   transitions drain writes; repeated enable does not invalidate live data.
3. **Normal scanner startup.** I-cache starts automatically. Ordinary eSCL
   clients no longer need the bench-only `0x8c` command.
4. **High-resolution colour throughput.** Initial capture timing and motor
   speed are set together: 40,000 counts/s at 75 dpi, 26,000 at 150, and
   6,000 at 300. This gives conversion/PNG processing enough time to keep up.
   The 300 dpi trial before the timer fix maintained a backlog of two rows.
5. **Long-move clock.** A 32-bit duration at 96 MHz wraps after 44.7 seconds.
   The motor now accumulates whole milliseconds while retaining fractional
   ticks. The original late-tick stop and phase deadlines remain effective.
   The [old-build regression](motor_wrap_before.log) reproduces the false stop;
   the new test also proves a real delayed tick still stops the drive.

The preceding agent's run-in shading calibration, per-LED exposures and
geometry changes are retained, as are the earlier consecutive-component RGB
ring and unconditional acquisition shutdown fixes. [Swatch samples](swatch_comparison.json)
compare the initial repaired 75 dpi image with the recorded stock reference;
this is not a general optical-quality certification.

## Validation and limits

All suites passed before flashing this exact candidate:

- [Cache, USB and hybrid](rollover_usb_hybrid.log): intermediate MPU states,
  inherited regions, repeated cache switches, both IPP interfaces, required
  ZLPs, management/recovery, and sentinel-filled USB buffers surviving stock
  RAM initialization.
- [Motor and homing](rollover_motor.log): duration rollover, genuine delayed
  tick, bounded jogs, homing, cancellation, deadlines and management recovery.
- [Scanner](rollover_scanner.log), [RGB results](rollover_colour.json): exact
  synthetic pixels at 75/150/300 dpi, ring reuse, full 75 dpi page, cancellation,
  immediate capture shutdown and unread rows surviving return-home.

Unicorn does not implement stale cache lines or this MPU's access checking.
The CP15 observer checks the compiled register operands and intermediate
permissions. Synthetic optics and motor feedback check software contracts;
the physical scans above establish operation on this printer. The cache and
permission attributes follow the [ARM946E-S reference manual](https://documentation-service.arm.com/static/5f032835cafe527e86f5b8ad).

300 dpi is now functional but considerably slower than the recorded stock
reference. Next work is conversion/encoding speed and coordinated acquisition
pause/resume for a slow host. **Do not increase speed on the assumption that
motor-only throttling slows DMA requests.** Earlier physical logs show the
IRQ producer continuing while the carriage crawls. Model acquisition
eligibility versus encoder position before claiming slow-consumer support;
brief 21 describes stock's stronger pause/restart approach.

Keep USB DMA in its linker section and inside the stock RAM exclusion range.
Keep the safe MPU ordering and the millisecond motor duration. Update over
USB only; allow about a minute for hybrid restart after verified C0.

Earlier trials are retained for diagnosis: `569db934…` passed calibrated
75 dpi repeats and a 150 dpi crop; `4b6a9135…` exposed the MPU ordering fault
([startup failure](startup_failed/result.json)); `c26c1e32…` passed full
75/150 dpi but hit the 44.7 s motor clock wrap ([timer failure](timer_failed/failure.log)).
For long-scan performance analysis, use host wall time: the existing eSCL
`produce_ticks`/`row_ticks` profiling sums are still 32-bit and can wrap after
44.7 seconds of accumulated CPU time. Widen those statistics before relying
on them for optimization. This does not control motor timing.

Use the installed package above, not those trials. The [checkout patch](checkout.patch)
includes inherited calibration edits as well as this repair; changes remain
uncommitted.
