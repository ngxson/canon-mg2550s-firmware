# Scan ring engine, colour mode and CPU cache — 2026-10-27

**Later repair: full 75 dpi RGB now completes and shuts down correctly on
hardware.** See [scan_finish/README.md](scan_finish/README.md) for the installed
package, physical scan and current buffer layout. Moving RGB now uses a
219-line ring with consecutive component rows; the older planar streaming
description below is historical. Calibration/burst layouts are unchanged.

Session goal (user): the eSCL scanner path should match stock quality and
scan time. Stock reference from the recorded host traces
(`emu/evidence/stockapp/telemetry/*/scan_*trace.jsonl`): A4 300 dpi RGB 18.0 s
total, image phase 12.6 s (3.6 ms/line); 150 dpi 50 mm RGB crop 7–9 s.

## Results on hardware

| Item | Result |
|---|---|
| Instruction cache (USB `0x8c`, `app/src/cpu_cache.c`) | tight loop 2.6 → 90.6 M instr/s; MPU region 0 + I-cache only, data cache off for DMA |
| 75 dpi grey full page, old whole-page buffer, old build | 409 s, aborted at the end-of-travel stall (fixed: captured lines win) |
| 75 dpi grey full page, stop-and-capture (ring too small for a page) | 213 s |
| 75 dpi grey full page, **ring engine** (package `0148a02f…`, 40,000 counts/s) | **17.1 s wall**, 876 rows, image correct (`scans/ring_gray75.png`) |
| Colour burst timing (stationary, `d=3`) | IRQ19 every 3.39 ms = 3 × P0 sub-period, N requests → N+4 events |
| LDMAC ring wrap | pointer readback after the burst = end of the last transfer; with a window smaller than the transfer count the address wraps modulo the window and advances by segment + second skip per transfer (grey 1 row, colour 2N+1 rows), i.e. component k of line i lands in row (2N + i + k(2N+1)) mod (3N+1) |
| Stationary bursts (grey and colour, any AFE offset, with or without 128 warm-up periods, after a cold power cycle) | every data row exact zeros, sentinel rows untouched; the emulator diff of the burst's register sequence against the committed build shows only the buffer address |
| 75 dpi RGB page (ring engine) | aborted by the "zero output" check on lines 0/1 (component rows per the layout above); cause open, see the variants below |

## Design (app/src/scanner.c, scan_page.c, scan_source_scanner.c)

- The LDMAC window is used as a hardware ring (672 rows of 0x1440 bytes at
  `0x20420000`, the app's RAM bound; stock snapshot pages start at the alias
  of 0x20771000, the stack at 0x207ff000). The DMA transfer count is the whole
  page; grey line i is row i mod 672, colour uses stock's planar window
  (N = 218 lines). Lines are released in order by the page consumer; a
  request that would overwrite an unreleased line fails the page instead of
  silently dropping rows (brief 21 §2: stock stops the motor on pressure).
- Backpressure: `scan_page.c` throttles the carriage to 3,000 counts/s when
  the backlog exceeds half the ring and restores the cruise below a quarter
  (`scanner_motor_scan_throttle`). Whether lines are position-gated (so a
  slower carriage yields correct geometry) is still to be verified on
  hardware with a deliberately slow host.
- Cruise per pitch: 40,000 counts/s at 75/150 dpi, 37,000 at 300 dpi
  (brief 21 §3: stock's P0 timing gives 37,043 for RGB 300; 75/150 dpi stock
  speeds exceed our controller's 60,000 cap). Sub-period
  T = 96 MHz · pitch · 0.98 / (v · d), d = 3 for colour.
- Colour: stock's C=3 register deltas (fixture diff), request mask 7,
  three component rows per line, mapped to R/G/B by a configurable order
  (USB `0x9c`; the physical order is unmeasured).
- eSCL now advertises RGB24 and 75/150/300 dpi. Shading calibration is not
  implemented yet; fixed black/white levels as before.

## Validation

`app/tests/validate_escl_scan.py` models the ring (wrap, second skip, mask 7
→ three component rows) and checks exact grey and RGB pixels, positions,
cancellation and motor servicing; `validate_burst.py`, `validate_page.py`,
`validate_escl.py` and `app/hybrid/validate_hybrid.py` pass on every package
flashed today.

## Zero output: cause and fix (07:00)

Row 0 of the stop-and-capture page is black as well: **the first acquisition
setup after the scanner power rail (0xc0c85614) comes up outputs zeros for
every line**; a second setup with the rail kept up outputs data (stock
programs its P5 staging profile at power-on before any capture). The ring
engine primed once per boot but cut the rail after every page and burst, so
the RGB page and all bursts were first setups. `scanner_power()` now clears
the primed state on every power-off; bursts and pages prime after each
power-on; the zero-output retry keeps the rail up. The six burst variants
(immediate request, no S+0x158 acknowledge, full stock arm table) all read
zeros and are no longer needed as hypotheses.

Dark/white calibration (`scanner_calibrate`, page phase `PAGE_CALIBRATE`):
eight lines with closed illumination windows and eight with the page's
exposure, averaged per sample and component at the mechanical home, applied
per sample by `scan_source_scanner.c` (white target 250, samples with a span
below 1024 fall back to the fixed levels). The emulator model produces dark
lines for closed windows and a fixed white reference for stationary
captures; `validate_escl_scan.py` checks the shaded grey and RGB pixels.

## Open

1. Hardware confirmation of the priming fix and of the colour page (package
   `21d75bb5…`); the board hung once after the burst-variant session (cause
   unknown, recovered by a power cycle).
2. Dark/white calibration, R/G/B order, horizontal geometry (brief 21 §5–6).
3. 300 dpi colour throughput versus stock's 12.6 s image phase.
