# Stock scan reference for the eSCL colour path — 2026-10-27

Stock-as-app (telemetry package `f4eb8d47…`, `emu/output/stockapp/telemetry_v3/
candidate.mgap`) was flashed, scanned the user's colour test sheet through
`tools/stock_scan.py` (full platen, calibration on, the tool's 1/2.2 gamma
tables), and the hybrid package `1c0fba19…` was flashed back through
`app/stockapp/runtime/hold_on_startup.py` + power cycle. Scans: `scans/stock_rgb300_165308.png`
(2480×3508) and `scans/stock_rgb75_165308.png` (620×877), traces alongside.

Three earlier cycles produced no scan: stock answered StartJob with
`DeviceBusy` because its status was `notready / DoorOpen / support code 1200`
(cartridge cover open; `tools/stock_status.py`). The hybrid never reads that
sensor. `stock_scan.py` also aborts if `--output`/`--trace` exist.

## Timing (stock, host traces)

| Scan | Wall | First row | Image phase |
|---|---|---|---|
| A4 300 dpi RGB | 21.9 s | 9.2 s | 12.7 s (reference traces: 12.6 s) |
| A4 75 dpi RGB | 14.5 s | 10.4 s | 4.1 s |

Hybrid `1c0fba19…` 75 dpi RGB for comparison: image 13.4 s, page done 23.0 s
(`app/evidence/scan_finish/README.md`).

## Tone and colour (75 dpi, mean RGB of the sheet's swatches)

| Patch | Stock | Hybrid `1c0fba19…` |
|---|---|---|
| paper | 227 227 229 | 252 247 254 |
| C | 107 181 212 | 0 140 255 |
| M | 219 110 145 | 255 0 124 |
| Y | 223 199 117 | 254 175 0 |
| K | 108 101 99 | 0 0 0 |
| R | 217 110 103 | 254 0 0 |
| G | 99 164 115 | 0 96 4 |
| B | 103 100 145 | 0 0 153 |

Stock: no pixel at 0 or 255 (luminance p0.5 = 43, max 242); the hybrid clips
76 % of red samples at 255 and 15 % at 0. Both apply a 1/2.2 encoding gamma
(stock through the tool's table, the hybrid through `gamma_lut`), so the
hybrid's clipping comes from its fixed levels (`CONT_BLACK` 1000,
`CONT_WHITE` 36000, grey settings used for colour at 8/8 exposure) and the
missing per-component references, not from the curve. In stock's output paper
sits at 0.77 linear (its white reference is the calibration strip, brighter
than paper) and black ink at 0.13 linear.

## Next

1. Dump raw 16-bit colour rows of the sheet from the ring (`line_dump.py`
   after a short eSCL scan) to get per-component black and paper levels and
   to check the AFE is not saturating at gain 13.
2. Map per component so paper → 227 and black ink → ~105 as stock does
   (white reference = dark + (paper − dark)/0.77), then the real calibration
   strip when its position is known.
