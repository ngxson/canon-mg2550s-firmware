# Print-quality repair — 2026-10-27

**Print repair verified on `62517f31…`; current package `602fe9f4…` retains
it and adds the [scanner stripe correction](../scanner_stripe/README.md).**
Protected flashes returned C0 (success) and application restart passed.
QI272229B completed through normal IPP;
the final 300-dpi scan verifies continuous bars with both gap defects gone.
Both boot stages are retained. This is still hybrid firmware: paper/carriage
motion, band preparation and firing call stock code. The scheduling, raster
conversion and transport fixes are readable C/Python in this repository.

## What changed

- `print_engine.c` prints each band in two disjoint row passes. Physical rows
  0..15 in each 32-row group print one half; a 16-row feed lets the same
  nozzles print the other half. Each intended dot is assigned once. The
  remaining band advance is shortened equally, so nominal page scale stays
  unchanged. Blank raster rows are explicitly cleared as well.
- Conversion is deferred while `hybrid_busy()`. Stock servos are polled;
  converting incoming rows inside their wait loop can delay motor updates.
  USB management and packet reception continue, with document backpressure.
- `ippusb.c` no longer rewinds the packet offset after a partial document
  feed. That extra decrement replayed a consumed byte or underflowed at
  offset zero, corrupting larger streamed pages.
- The host URF encoder in `tools/ipp_print.py` now encodes a singleton using
  the one-pixel repeat code. Its previous literal code was the invalid byte
  256. This host-tool error did not cause the original printed stripes.

The two-pass workaround is physically verified on Q272212. It costs a second
sweep per segment. No heater timing/energy changes were made. The underlying
reason for the periodic weak rows remains unresolved.

## Physical evidence

| Sheet | Purpose | Result |
|---|---|---|
| Q272125 | Same marks, varied width and density | Repeating K/C/Y gaps at every width, including full-width mono; all raster readbacks and requested dot counts exact. |
| Q272212 | SINGLE versus ALT16 | Recurring thin strips disappear in ALT16; sparse lines/staircases remain continuous. |
| QI272220 | Normal IPP pipeline on `c560e06d…` | 3 bands / 12 sweeps complete, but first inter-band feed overshoots by 32 print rows, leaving a larger colour gap. |
| QI272229B | Normal IPP on `62517f31…` | 3 bands / 12 sweeps complete; all six feeds exact; final scan confirms continuous bars. |

### Width/density diagnostic Q272125

Each sweep starts at the same carriage position. Marks comprise a solid
32-column bar, a two-column line, a half-density bar, a repeating 32-row
staircase, labels and row ticks. First feed 650 Ulf, subsequent pitch 640 Ulf;
304 black rows and 160 colour rows. No diagnostic heater words are changed.

| Sweep | Mode | Columns | K requested / counted | Each C/M/Y requested / counted |
|---|---:|---:|---:|---:|
| C1024A | 7 | 1024 | 20598 / 20598 | 8320 / 8320 |
| C1024B | 7 | 1024 | 20630 / 20630 | 8320 / 8320 |
| K1024 | 1 | 1024 | 20326 / 20326 | inactive |
| K2112 | 1 | 2112 | 20182 / 20182 | inactive |
| K4224 | 1 | 4224 | 20310 / 20310 | inactive |
| C2112 | 7 | 2112 | 20166 / 20166 | 8320 / 8320 |

Every uploaded byte was read back, with zero mismatches. ASIC activity
counts do not prove correct ink deposition, but they rule out a net loss of
requested dots in these sweeps. Files: `Q272125.json`, `Q272125.log`,
`dot_summary.json`, `scan_Q272125_rgb300.png` (2480 × 1400; 37.568 s transfer).

The strong black gaps are near rows 26–28 modulo 32, cyan near 20, yellow
near 22; 300-dpi sampling/ink spread limits exact row assignment. Magenta
lacks the strong gaps. The gaps are about two nominal print rows wide, not
an established missing eight-row group. Earlier apparently clean full-width
pages did not contain comparable tall bars; width and density are ruled out
by this controlled comparison. The old “one group in four” claim is unproved.

### Physical workaround Q272212

`quality_probe.py --alternate-nozzles` used the original installed firmware
(`e42542a0…`) for one SINGLE sweep and two ALT16 sweeps. All readbacks and
counts match: SINGLE K 20582, ALT16 K 11286 + 8896; each C/M/Y 8320 versus
4160 + 4160. K totals differ because the labels differ. The 32-Ulf feed
moved 192 encoder counts, as expected. Ejection and homing completed.

[Comparison crop](Q272212_comparison.png): SINGLE above ALT16. The repeated
white lines in K/C/Y are absent in ALT16. The full 2480 × 700 RGB scan is
`scan_Q272212_rgb300.png` (19.881 s transfer). Interior ROI measurements in
`Q272212_scan_metrics.json` show black median/brightest row intensity falling
from 95.4/183.6 to 94.8/104.0 (0 black, 255 white). No ALT16 plane has an
interior row exceeding its median by 40 levels; SINGLE has such rows in K/C/Y.
These measurements establish removal of the strong gaps on this pattern,
not perfect photographic quality.

The shim cumulative error counter rose from 5 to 6 during SINGLE's existing
finish-event wait; neither ALT16 sweep added an error.

### Feed overrun exposed by QI272220

The protected `c560e06d…` update passed C0 verification and re-enumeration.
`quality_ipp.py` then sent a labelled 2480 × 200 RGB URF through normal IPP.
It completed 324 output rows, 3 bands and 12 sweeps in 61.63 s, ejected and
homed with zero engine failures. Both RAM canaries remained intact, with no
hardware owner active afterward (`post_ipp_health.json`). The shim retained
three existing acquisition/finish-event timeouts: engine success is distinct
from the shim's event accounting.

The user reused a sheet; the right-hand bars avoid the old diagnostic area.
[Scan crop](QI272220_feed_gap.png) shows the thin repeating gaps gone, but a
larger gap at the first colour-band boundary. The first requested 272-Ulf
advance ended at 2016 raw counts: 336 Ulf, an excess of 384 counts / 64 Ulf /
32 print rows. The next identical advance was 1632 counts, exactly requested.
The three 32-Ulf interpass advances were 192, 193 and 192 counts. Full scan:
`scan_QI272220_rgb300.png` (2480 × 700, 22.927 s transfer); feed evidence:
`QI272220_firmware.log`. A constant feed compensation would be inappropriate.

At a band boundary the ring admits more input. `shim_poll()` services IPP
inside stock calls, so newly admitted rows could run expensive conversion
inside the LF polling loop. `app_yield()` there only services the scanner.
A new emulator hook reproduces conversion during a stock call on c560e06d
(`servo_starvation_before.log`). The 62517f31 guard prevents this overlap;
the follow-up print records exact travel for every feed (below).

## Validation and limits

`feed_guard_colour_emu.log` and `feed_guard_mono_emu.log` pass exact paper
reconstruction from emitted lane rasters and commanded feeds: every expected
dot, no duplicates, blank margins, both colour segments, the band seam and
final partial band. A hook asserts conversion never runs while a stock
routine is running or pending. These checks model commanded geometry, not
physical nozzle behaviour or servo timing. The fixture's known dry-motion
and event timeouts remain.

All 17 application/eSCL suites passed on both candidate revisions:
`regression.log`, `feed_guard_regression.log`. They include management reboot
into rescue. Native URF streaming tests pass (`urf_test.log`); Python syntax
and `git diff --check` pass. The richer colour/mono streams reproduced the
old HTTP offset corruption and now complete with exact pixels.

Baseline emulation also completed three 2112-column colour and two
4224-column mono passes without changing live pattern buffers:
`baseline_colour_emu.log`, `baseline_wide_emu.log`, `emu_fixture/`.

The idle head-register comparison (`hardware_idle_head.json`) matches fixed
stock boot configuration after allowing for documented readback masks and
counter-latch commands. It is not evidence for increasing firing energy.

## Artifacts and reproduction

| Package | Full SHA-256 | Archived build directory |
|---|---|---|
| Initial installed baseline | `e42542a0e3d686e67f6b6a2c4d5848679a7bc70d22deb3bdef749ec9924b03e1` | `app/hybrid/build/print_quality_baseline/` |
| Two-pass + HTTP fix, feed overlap still present | `c560e06d852cdde9e9fe3d72f3de48d383e320887437658235e3c708bcb8d76c` | `app/hybrid/build/print_quality_candidate/` |
| Adds conversion scheduling guard | `62517f311f4adb4993913d287225c7ef8c99841c09934274814304ec6cc7a42b` | `app/hybrid/build/print_quality_feed_guard/` |

Build with `python app/hybrid/build.py --boot-dir bootloader/build/vectors`.
The default boot build lacks the stock-call vector API; the first emulator
attempt stalled before stock calls for this reason. Correct emulator boot
SHA is `f22df711f7afd8c3804c43e2d491b8029af59a49f6190c2cafe1097a702e4b92`,
matching the baseline image. Ordinary app updates preserve the physical
first 64 KiB; no bootloader/whole-flash update is used here.

For targeted checks set `HYBRID_IPP_PRINT=1 HYBRID_IPP_QUALITY=1` when running
`app/hybrid/validate_hybrid.py` in the Docker ARM emulator. Add
`HYBRID_IPP_MONO=1` for mono. Mount `emu_fixture/` at
`/project/emu/output/fixture`, as in the recorded commands.

`candidate.patch` compares our changes to takeover copies. `before.patch`
includes pre-existing uncommitted work; it is not an isolated fix patch.
The manifests record source hashes. `flash.log` and `flash_feed_guard.log`
record ordinary protected updates. The first flash script stopped before
any write because it compared a decoded empty owners list with integer zero;
that preflight predicate was corrected and the update then succeeded.

The electrical/nozzle explanation for the weak rows, full-page photographic
quality and speed optimisation remain separate work. Two-pass scheduling
adds sweeps; it is a measured workaround, not a claim of a complete stock
print-engine replacement.

The first post-update host attempt QI272229 failed claiming the IPP USB
interface before transmitting print data (`feed_guard_ipp_live.log`); the
separate retry QI272229B uses the same ready sheet. This is a host connection
failure, not a partial/duplicate submitted job.

## QI272229B follow-up

The normal IPP page completed in 61.61 s: 324 output rows, 3 bands, 12 sweeps,
zero engine failures, sheet ejected and carriage homed. The six feed moves
now exactly match their commands: 2364, 192, 1632, 192, 1632, 192 encoder
counts. `feed_comparison.json` contrasts them with QI272220's 384-count
overshoot. This supports the servo-starvation diagnosis; no constant feed
compensation was added. `post_feed_guard_ipp_health.json` confirms idle owners
and both RAM canaries intact. The final scan `scan_QI272229B_rgb300.png` confirms continuous K/C/M/Y
bars, including the band boundary. `QI272229B_verified.png` is the crop.
The broad central vertical bright stripe in that image was a separate
scanner calibration artifact. It is now fixed and physically verified on
`602fe9f4…` by rescanning this same sheet; see the
[scanner evidence](../scanner_stripe/README.md) and
corrected scan (`after_rgb300.png`, not kept in git).
