# Print speed — profile and sweep planner (2026-10-28)

Work order from [the print-speed brief](../../../../fw_dump/gpt/BRIEF_print_speed.md):
step 1 (timing profile) and step 2 (skip/crop empty raster) are implemented
in readable C (`app/src/print_engine.c`, counters in `app/src/stock_shim.c`);
stock motion, band preparation and firing are still borrowed.

## Baseline decomposition (source observation: QI272229B firmware log)

CR servo ticks are 2 ms, LF ticks 1 ms. Of the 61.5 s job: carriage motion
37.0 s (25 moves; firing traverses ~2.2 s per 2112-column segment, returns
1.5 s / 0.28 s), feed 3.9 s, two capped owner-wait timeouts 4.0 s, the rest
(~16 s, ~1.2 s per sweep) unattributed. The profile below attributes it.

## QI272327 — package `1e01a548…`, physical, 2026-10-28

Same QI pattern as QI272229B plus a cyan bar across the planner's sweep
boundary (output column 2176) and a one-pixel line on that column. Normal
IPP print, 12 sweeps, zero engine failures, ejected and homed, **57.8 s**
(QI272229B: 61.6 s). Device profile (ms, `QI272327_ipp.json`):

| Engine phase | ms | | Shim counter | ms (count) |
|---|---:|---|---|---:|
| fire | 36246 | | CR motion | 33141 (25) |
| load | 5539 | | LF motion | 4279 (14) |
| advance | 4077 | | event waits (satisfied) | 37234 (200) |
| convert | 3855 (162 rows) | | wait timeouts | 4002 (2) |
| eject | 3277 | | stock delay() | 7181 (73) |
| prepare | 1336 | | RAM init | 867 (3) |
| home + head | 872 + 801 | | stock prepare | 1042 |
| input wait | 784 | | arena peak | 1091816 |
| fill | 570 (12) | | errors | 3 |
| other | 237 | | | |

Sweeps planned (firmware log): first pass 2112 + 1056, then 2112 + 1312,
2112 + 1376 (x4), 2112 + 640 columns. The right-hand group printed in
cropped sweeps (1376 columns: 1.65 s traverse instead of 2.2 s); the left
window stayed 2112 wide. Replaying the same URF in the emulator reproduced
the plan exactly and showed why: this build kept one `[lo, hi)` tile span
per ring row, so rows with ink in both the left and right groups counted as
fully occupied. Build `49437d4a…` reads the ring bytes directly (exact per
tile); the replay then plans the first pass as 1888 + 672 columns, and the
later left sweeps stay 2112 only because the seam bar reaches tile 135.

Scan `scan_QI272327_rgb300.png` (300 dpi RGB, whole page; the sheet was a
reused one with a faint older print further right): all bars, thin lines and
diagonals continuous; the seam bar is solid across the sweep boundary and
the seam line straight (`scan_QI272327_seam_x2.png`). Bar edges versus the
QI272229B scan: every group +5..6 px (0.5 mm), both left and right equally,
i.e. a sheet-position difference, not a per-sweep placement error.

What the profile says about the remaining time:
- Motion dominates (33.1 s CR + 4.3 s LF). Traverse time scales as roughly
  0.75 s + 0.69 s per 1000 columns, so cropping helps in proportion to the
  blank width; the stock profile itself is later work (brief step 6).
- 73 stock `delay()` calls cost 7.2 s, and 7 advances cost 4.1 s against
  0.6 s of LF motion: the per-feed overhead (resource coordination before
  each LF request, `TICK_MS` 10 per delay tick is a fixture assumption)
  is the next software target, with the two 2 s owner-wait timeouts.
- Conversion 3.9 s (24 ms per input row) is additive; brief step 4.
- Prepare/fill/finish/drain total under 2 s: step 3 is not the priority.

## QI272343 — package `49437d4a…` (exact occupancy, merged feeds), physical

Same pattern 1500 input rows lower on the same sheet (`quality_ipp.py --top
1500`), so 20 empty bands precede it. 12 sweeps, 40 passes skipped, zero
failures, **90.8 s**: fire 33.4 s (CR 30.3 s in 23 moves), **convert 33.1 s
for 1662 input rows** (20 ms per row, nearly all white), advance 9.0 s in 11
moves (feeds `394, 1200 ×5, 80, 32, 272, 32, 272, 32`: the 40 skipped
passes' 6080 Ulf merged into six moves), load 5.5 s, eject 2.3 s, stock
delays 10.6 s (95). Sweeps: 2112 + 928, 2112 + 1344, 2112 + 1376 (×2),
then 224 + 224 (×2) for the last band. `scan_QI272343_strip.png`: bars,
lines and the seam bar continuous; bar edges within 0.5 mm of QI272327.
The dead time the user saw after the sheet was fed is the blank-row
conversion: the next build flags white URF rows in the decoder (memset
instead of halftoning) and converts both output rows of an input row in one
byte-at-a-time pass.

## QI272358 — package `c18905b7…` (white-row fast path, byte-at-a-time converter, per-routine profile), physical

Same pattern 2600 input rows down a different reused sheet (`--top 2600`;
the sheet's top carries old content). 16 sweeps (the pattern now straddles
four bands), 68 passes skipped, 17 feed moves (`394, 1200 ×8, 736, 32, 272,
32, 272, 32, 272, 32`), zero failures, **77.4 s**. Conversion **3.7 s for
2762 input rows (1.4 ms per row, from 20 ms)**. Fire 42.9 s (CR 39.7 s in 29
moves), advance 12.9 s (LF 5.3 s in 22 moves), load 5.5 s, eject 1.8 s,
home + head 1.9 s, other 5.4 s (the planner reading 160 KB of ring bytes
for each empty pass; the next build keeps a per-row blank flag). Per
routine (`PRF3`): feed moves 68 delays = 9.7 s (≈690 ms of stock delay per
feed), loader 24 delays = 1.9 s + one 2 s owner-wait timeout, sweeps 17
delays = 1.2 s + one 2 s timeout, eject 5 delays = 0.6 s. Stock delays are
13.4 s of the job: the helper brief `BRIEF_print_overhead.md` asks for the
delay unit (the shim assumes 10 ms per tick), the per-feed resource acquire
and the owner-wait completion.

`scan_QI272358_strip.png` (the sheet lay rotated on the glass; crop rotated
back): bars, lines and the seam bar continuous. The left magenta bar's left
edge is slightly ragged here and on QI272343 but not on QI272327: noted,
not investigated. Package `74e7174f…` (blank-row flags in the planner) is
installed afterwards, emulator-validated, not yet printed. It then did not enumerate on USB (2026-10-28); `c18905b7…` was reinstalled.

## Flash layout change

The app had reached the hole's 132 KiB snapshot boundary. Snapshot pages are
now word-run encoded (121344 → 90808 bytes) and packed behind the app inside
the hole, table in the erased tail sector `0x1f2000`
(`app/hybrid/LAYOUT.md`). `flash_read_watch_B.log` is the emulator read
watch that showed the band builder reading sectors `0x16b000`, `0x16c000`,
`0x178000`, `0x179000` during a job; a trial placing pages there crashed
the loader, so maintenance-coverage "untouched" runs are not free for data.
`flash_1e01a548.log`: ordinary protected update, result C0.
