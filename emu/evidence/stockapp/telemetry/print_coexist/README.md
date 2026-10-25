# Print with live telemetry — 2026-10-25

Physical evidence collected directly over Mac USB with the installed telemetry
stock app (package SHA-256
`380b98ca115955b8a19d410979a42374b219ced94eb03f5c7c715f28f787461e`).
Two sheets were used, both printing [test_sheet.png](test_sheet.png): mm
rulers from the printable origin, solid C/M/Y/K/R/G/B patches, 11-step ramps,
a continuous K ramp, 1–16 px line weights, circle/square/diagonal, a 1-px
checker and a bottom-right corner mark. The first attempt with the earlier
transport failed without feeding paper; it is preserved with its analysis in
[attempt1_failed/](attempt1_failed/README.md). The runs below used the fixed
transport (stall-tolerant OUT, 250 ms back-channel polling).
Provenance and hashes: [manifest.json](manifest.json).

| Job | Telemetry polling | Result | Job time | OUT waits (NAK) | Back-channel reads |
|---|---|---|---:|---:|---:|
| 300 dpi A4, 112,759 B | none | EndJob acknowledged, page printed | 18.9 s | 13.0 s in 4 slices | 218 (11.5/s) |
| 600 dpi A4, 308,693 B | 10 Hz, history-free | EndJob acknowledged, page printed | 27.0 s | 3.0 s in 3 slices | 315 (11.7/s) |

Both pages printed completely and were scanned back at 300 dpi
(`scan_of_printed_sheet_300dpi.jpg`, `scan_of_printed_sheet_600dpi_job_300dpi_scan.jpg`, stored as
JPEG; the geometry below was measured on the lossless scans, hashes in the manifest;
the second sheet was a reused one and still shows a faint earlier HELLO WORLD).
The printer's own status, parsed from its unsolicited `GetStatusResponse`
replies, went `idle` → `processing/DJS=LD` (load) → `PR` (print) → `EJ` (eject)
→ `ID`/`NO` within each job; `DOC` went `4,00` → `5,01` → back. Status detail
stayed `MarkerSupplyAttention` (`CTK:CL,LOW`, colour cartridge low), which
does not block printing. Printer-class port status was `0x18` throughout.

## What this qualifies

- **`tools/stock_print.py` is physically qualified** for A4 plain paper at
  300 and 600 dpi, image mode. Letter and the built-in `--test-page` pattern
  were not printed.
- **Telemetry coexists with printing.** 10 Hz history-free polling ran
  through the 600 dpi job with no USB error; USB state stayed `[4,1,1,1]` and
  the native error byte zero for all 1,519 snapshots (166 s). No history
  polling was attempted during a print after the scan result showed its cost.
- **The transport fix is physically confirmed.** The printer NAKs bulk OUT
  while printing (13 s total in the 300 dpi job, up to 6 s in one 16 KiB
  chunk); each libusb slice resumed from its reported byte count and the
  decoded page is complete, so the exact-resume path works on real hardware.

## What the telemetry shows during a print

With the ESP32-paced back channel the stock message counter is quiet: 238
stock messages over the 27 s job (≈ 8 per second, i.e. about one message per
back-channel status reply), 0 when idle. Engine, motor and head activity do
not appear in this counter at all. During the failed attempt the unpaced
drain produced ≈ 1,750 messages per second, confirming the counter measures
host-driven USB transactions. The SETUP rings contain only the tool's
`GET_DESCRIPTOR(string 0)` identity checks and, in the failed attempt, the
printer-class `SOFT_RESET`.

## Print geometry measured from the scans

| Measure | 300 dpi sheet | 600 dpi sheet |
|---|---:|---:|
| Ruler major spacing, horizontal | 9.997 mm per 10 mm (−0.03 %) | 10.003 mm (+0.03 %) |
| Ruler major spacing, vertical | 9.994 mm (−0.06 %) | 9.999 mm (−0.01 %) |
| Right corner mark, drawn 16.9 mm tall | 8.8 mm printed | 8.6 mm printed |
| Horizontal part of the corner mark and its label | absent | absent |

Print scale is exact on both axes (and the scanner's too, since both are
measured through it). Both prints stop at about 280 mm from the top paper edge:
nothing drawn beyond ≈ 277 mm from the printable origin appears, leaving the
bottom ≈ 17 mm of the sheet blank even though the job header declares a
288.6 mm (818 pt) printable height. This is stock behaviour with the ESP32's
geometry commands, not a transport error: EndJob was acknowledged after a
normal eject. The absolute origin offsets could not be measured because both
sheets overhung the platen edge during scanning. The 300 dpi black patches scan
as dark grey, the 600 dpi ones darker; the cyan ramp has a nozzle streak.

## Geometry sheet — 2026-10-25 21:43 UTC

A third sheet printed [geometry_sheet.png](geometry_sheet.png) at 600 dpi
(during the message-ring capture) and was scanned with its left and top paper
edges inside the platen (`scan_of_geometry_sheet_300dpi.jpg`). It carries
1 mm bars at 0–10 mm from the printable origin, full-width bars every 2 mm
from 240 mm to the bottom, and rulers on three sides.

| Measure | Value | Nominal |
|---|---:|---:|
| Printable origin from left paper edge | 3.47 mm | 3.53 mm (10 pt) |
| Printable origin from top paper edge | 2.92 mm | 3.18 mm (9 pt) |
| Top bars printed | 0 through 10, all present | |
| Last full-width bar printed | 276 mm | 288 mm drawn |
| Lowest ink | 277.2 mm from origin | 288.6 mm declared |
| Print stops | 280.2 mm from the top edge, 16.8 mm above the bottom edge | 8.4 mm bottom margin declared |

The printer honours the declared origin on both axes within 0.1 mm and
prints the first row, but stops after row ≈ 6548 of the 6,816 it was sent:
nothing beyond 277.2 mm from the origin appears, on all three sheets. With the
ESP32's header this stock printer therefore has a usable area of about
277 mm × 203 mm, not the 288.6 mm declared. **Cause found with telemetry
ABI 3** ([engine/README.md](../engine/README.md)): stock latches an active
area of `h=6545` for the requested `h=6816`, i.e. it applies a 396-unit
(16.76 mm) bottom margin at job setup; 6545 rows = 277.1 mm. It is a
geometry clamp, not a sensor limit: the literal 396 lives in the stock
function at `0xaac50`, confirmed by executing it in the emulator and by RE
brief 13. The printer status went `WP` for 38 s
before `LD` on this job (substate 4 with feed-motor activity on the repeat).

## Limits

- Two sheets, one per mode; no repeat, no Letter, no polling-on/off pair in
  the same mode (the 300 dpi job had no polling, the 600 dpi job had 10 Hz).
- Message counts come from the 8-bit ring index sampled at 10 Hz; IDs are not
  exposed, and nothing about the engine is visible in the current regions.
- The bottom cut-off needs a dedicated test (corner marks at several heights)
  before it is relied on in replacement firmware geometry.
