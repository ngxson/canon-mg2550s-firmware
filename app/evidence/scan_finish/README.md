# Scanner completion and continuous colour repair — 2026-10-27

**Installed and physically verified.** A short colour scan followed by a full
637 × 876 RGB scan completed on the printer. The full image arrived in 13.43 s;
the page finished returning home at 22.99 s. Both ended in DONE with no error
and scanner power off. A final readback confirmed motor drive, acquisition gate
and scanner power all zero, USB management responsive, and both application
canary arrays intact.

- [Full colour scan](../../../scans/scan_finish_rgb75.png)
- [Physical results](hardware/hardware_result.json), [final idle audit](hardware/idle_audit.json)
- [Full-page firmware log](hardware/hardware_full.log), [host log](hardware_check.log)
- [Verified upload and restart](linear_flash.log)

Installed package: `app/hybrid/build/scan_finish_linear/application.bin`, SHA-256
`1c0fba1941631a5ba1bad448df0b7394a397c79e045a7ef0ab97d4deb15644d0`.
The normal application-only updater reported verified `C0` completion; both
recovery stages were preserved. This remains intermediate hybrid firmware.
These scanner changes add no stock scanner calls.

## Changes and guidance

1. Moving colour acquisition stores each line's three component rows
   consecutively, using the same small DMA stride as grey. The ring holds 219
   RGB lines. The former large planar jumps are no longer used for streaming;
   finite burst/calibration captures retain their small stock planar layouts.
   Consumers must use `scanner_line_row()`, not the old planar-ring formula.
2. Acquisition completion switches off the lamp and ends forward travel before
   waiting for a motor endpoint. It then returns home.
3. Hardware shutdown is unconditional, including prepared-but-not-started and
   already-completed acquisition. Cancel stops acquisition before consulting
   page bookkeeping, so invalid page state cannot bypass that shutdown.
4. Successful return-home preserves unread rows until the next page. A slow
   host can finish downloading; cancellation and failure discard rows.
5. The other agent's scratch-memory diagnostic is retained, but yields every
   128 bytes. Its uninterrupted 8 KiB loop caused a measured 70,513-instruction
   motor-service gap; the corrected suite's maximum is 5,509.

Firmware edits are in `scanner.c`, `scan_page.c`, and the two diagnostic loops
in `app_main.c`. [The scanner delta](fix.patch) is relative to the initial
checkout snapshot and includes the other agent's concurrent scratch-diagnostic
integration; the diagnostic itself was not created by this repair.

## Validation and limits

- [Original regression](finish_before.log): compiled ARM firmware waited for
  motor runout after capture had completed.
- [Shutdown regressions](linear_finish.log): immediate acquisition/travel stop,
  unread RGB rows surviving home, and Cancel with invalid page bookkeeping.
- [Continuous RGB](linear_colour.log), [JSON](linear_colour.json): exact pixels
  at 75/150/300 dpi, 231 rows across ring reuse, and a full 637 × 876 page.
- [Existing scanner suite](linear_escl_scan.log): all eight cases pass,
  including cancellation, repeated jobs, geometry and motor servicing.
- [Final hybrid validation](linear_hybrid_validation.json): exact package boots
  through the protected loader and passes existing hybrid mechanism checks.
  [Full log](linear_hybrid_validation.log).

The completion-only package `a44cd5b5…` fixed the short scan's cleanup but
still timed out during a full-page request after 1,168,318 response bytes
([report](planar_hardware_result.json)). The user restarted the printer. The
revised layout passed the full page with checked RAM canaries intact. This
supports keeping the simpler layout, but does not prove the precise ASIC
mechanism behind the earlier corruption. Do not restore the large jumps on
the strength of short-burst or synthetic-DMA tests alone.

Physical full-page verification here is **75 dpi RGB**. 150/300 dpi were checked
in emulation, not physically by this repair. Image shading/noise and optical
calibration still need work; this fixes continuity/shutdown, not image quality.

## Recovery note

The setup is USB-only. An attempted I²C fallback selected an unrelated ESP
serial device and briefly reset it while probing; no firmware was written.
The user was informed. Do not probe arbitrary serial ports as though a recovery
bridge were connected. The documented mains-cycle URL returned HTTP 403; the
user's physical restart restored access. Hybrid startup also exceeds the host
tool's default 15-second post-upload wait: allow about a minute after verified
`C0` before treating a missing application as an update failure.
