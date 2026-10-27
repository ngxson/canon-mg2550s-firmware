# Full-page printing through the hybrid band path (2026-10-26 evening)

Goal: a stock-quality test sheet from our firmware. After the PLACED sheet
proved ink on the hybrid band path (BAND_FEED_INIT.md), two things stood
between that and a page: the band selected only one eight-row slot, and the
loader leaves the sheet short of the nozzles. This directory records the
changes that turn the single 320-column, 8-row band into a host-scheduled
page of full-head bands, and their emulator and dry hardware checks.

## What changed (package `85a4560c…b8c4`, app `stock_shim.c` + tools)

- **Band geometry over USB** (`0x9a` columns/rows, `0x9b` placement):
  the stock job is allocated `cols` wide, the BK selection matrix
  (`0x1806cd04`, 7 output groups × 40 entries) maps entry `2+g` to raster
  slot `g` for `rows/8` groups, and `Q+0x3a` takes the host's placement.
  The 8-row legacy band (entry 18 → slot 0, the PLACED sheet) is kept.
- **Lane masks come from the builder** (`Q+0x2ac..`), no longer overwritten
  with the single-slot `0x10000`.
- **Shim arena 64 KiB → 352 KiB** (bss, which the stock RAM init skips):
  stock's pool allocation for a 4224-column band is 348,992 bytes; with the
  old arena the kernel pool service returned 1 and stock halted forever at
  `0xa10b2` ([refused run](wide_4576_arena64k_refused.log)).
- Owner wait `1b:1` capped at 2 s per band (it expired at 15 s on PLACED and
  the band printed anyway).
- Host: `page_pattern.py` (rulers, seams, staircase, nozzle map, label) and
  `page_live.py` (stock load → first-line advance → per band: fresh
  prepare, upload, stock-CR executor, 608-Ulf advance → stock eject).

## Measured in the emulator ([log](validation_hybrid.log), [json](validation_hybrid.json))

| Quantity | Value |
|---|---|
| Manager 6 at 4224 columns | 80 slots, stride 1280, size 337,920; arena used 351,368 of 360,448 |
| Builder BK lane masks, entries 2..39 selected | `ffffffff 0000003f 00000000` (38 bits); lane DMA masks `0`, `ffffffc0` |
| Entry → mask bit | entry 18 → bit 16 (PLACED), entries 0..39 → bits 0..37: **bit = entry − 2**, so 38 groups = **304 rows** per band |
| CR window (`0xc0c51114` start, `0xc0c51108` end) | start = **4 × place + 34080** raw counts, quantized to 64; end = start + **16 × N + 640** (34272 from package `0957ec84` on, which keeps stock's cached common offset) |
| Executor | returns 1, BK lanes `1,1`, DMA base = raster storage `B`, stride 0x500, count 0x4f |

Full suite: `PASS hybrid` (cap, wipe, feed, band, owner-3 acquire, wide
band, forward step, panel).

## Hardware, no motion ([log](hardware_dry_prepare_4224.log), [flash](flash_85a4560c.log))

Package flashed with the protected updater (`C0`). `band 4224/304/904`,
fresh prepare: result 1, 0 errors, manager 6 width 4224 size 337,920 at
`0x20210080`, Q masks `ffffffff 3f 0` on both BK lanes (same as the
emulator); band 0 of the page (337,920 bytes) uploaded in 0.88 s and read
back identical.

## Units and placement used by `page_live.py`

- Ulf = 1/1200 inch: stock's LF position word went 218 → 871 between load
  and first print for a 3.2 mm top margin (telemetry engine README), and
  the PLACED scan (row 18 printed 12 mm below the leading edge after
  2 × 400 Ulf) agrees. Band pitch 304 rows = 608 Ulf; first-line advance
  650 Ulf after the stock loader.
- Columns are 16 raw counts; the sheet spans about x 29000..108300 in our
  carriage frame (stamp_plan, PLACED strip position). Default page: 4224
  columns (178.8 mm) from x 35008, i.e. placement 904.

## Not yet verified on paper

Physical nozzle order within a band (row 0 at the leading edge or not),
whether entries 0/1 really drive nothing, the band pitch, the first-line
distance and the left margin. The page pattern carries seams, a staircase
and labels so one sheet settles all of them.

## Sheet PG2009: band 0 printed, bands 1..20 silent (2026-10-26 20:09)

[Run log](hardware_PG2009.log), [steps](hardware_PG2009.json),
150 dpi scan (`scan_PG2009.png`, not kept in git), [counters](hardware_PG2009_counters.json),
[post-run registers](hardware_PG2009_post_registers.json).

- Stock load, 650 Ulf first advance, 21 × (fresh prepare, upload, stock-CR
  executor to 102824, 608 Ulf advance), stock eject, home: 203 s, no
  exception, every executor returned 1.
- **Band 0 printed completely and correctly**: label, rulers, "BAND 00",
  diagonal, both seam marks; readable text, so nozzle row 0 is at the
  leading-edge side and column 0 at the low-count side. 304 rows measure
  ≈12.5 mm (600 dpi nozzle pitch), left margin ≈13 mm from the sheet edge
  (predicted 16 ±2), top margin ≈2 mm (predicted 3).
- **Bands 1..20 left nothing**, and the paper did advance (no overprint).
  The head activity counters read once after the run hold 26180/20853 for
  the two BK lanes: band 0's expected 21940/16613 plus a 4240 baseline, not
  the 175021/126957 of all bands. Later passes fed no data at all.
- Post-run registers: raster-feed start word still 1, compare events
  `0xc0c51404 = 0x1e` pending, `0xc0c812c0 = 1`, `0xc0c87000 = 1`.

### Paper-free reproduction and cause

[`twopass_probe.py`](../../../tools/twopass_probe.py), 76-dot raster,
counters per pass ([twopass_A](twopass_A.json)): after our head prepare
(`cartridge.c`), pass 0 counted 76; passes 1 and 2 with a fresh snapshot
each counted 0. The only register that distinguishes them before the sweep
is `0xc0c812c0` bit 0: 0 before the pass that fired, 1 before the silent
ones.

Emulator, two passes (fresh vs
[not fresh](validation_hybrid.log)): with the snapshot restored before pass 2
the executor's register image is identical to pass 1 except read-modify-write
leftovers. With stock's RAM carried over from pass 1, pass 2 performs the
**full head power-up** (`0xc0c81584/815c4/812c4 = 0x2e5/81594 = 0x83/84664`,
head-prep timer 3, two owner acquires) that the fresh snapshot suppresses:
stock's pass end releases the head (`0xc0c87000 = 0` after the firing pass
on the board) and the snapshot claims it is still powered. Fix: restore the
snapshot only before the first sweep; later sweeps keep stock's state. The
cached common offset `0x180cda80/84/88` is then also left to stock (zeroing
it moved pass 2's window by 0x4140), after which pass 2's window and bounds
equal pass 1's (package `0957ec84…1703`).

## Sheet PG2039: non-fresh passes print, queue stalls at band 5 (2026-10-26 20:39)

Package `0957ec84…1703` (snapshot only before the first sweep, cached offset
left to stock). Paper-free probe first ([twopass_B_nofresh](twopass_B_nofresh.json)):
three passes counted 76/76/76, i.e. every pass fed data; passes 2 and 3 took
30 s because stock waited twice for `0x1c` bit 31 (the "previous head work
finished" event posted by stock's callback `0xca41e`, never reached here).

[Run log](hardware_PG2039.log), [steps](hardware_PG2039.json): bands 0..4
printed (ink on every band), 37 s per band, then band 5's prepare spun in the
raster worker's slot allocator (`0xa90ca`: five slots at `0x1806b1dc`, one
claimed per prepare, freed by stock only when a packet completes) until the
dry-run budget aborted it (`0xbad3`). The sheet was ejected with routine 9.

Fixes in package `1f07ef5b…8bbe` (emulator: eight consecutive non-fresh
passes, result 1 each, window `0x8840..0x192c0` every time, no bit-31 waits):

- `band_finish()`: stock's band-task tail after the executor
  (`0xe6dc6..0xe6e24`: `e281a(r)`, `e7052(0)` = CR wait + release, flags at
  `0x180d0320` cleared, `e2a3e(Q)`, `e2c40(Q)`, `e753c(Q)`, `e28a6(Q,1)`).
- The raster-worker slot tables at `0x1806b1dc` (10 bytes) are freed in every
  prepare with the other per-pass allocator state.
- The `0x1c` bit-31 wait is capped at 500 ms as a fallback.

Open: the DMA lane masks of non-fresh passes enable groups 2..39
(`0xc0c27570/74 = 0x3, 0xffffff00`) where the fresh first pass enables
0..37; the PG2039 scan will show whether bands 1..4 sit 16 rows off band 0.

### PG2041 scan: band pitch right, first band 16 rows high

Scan `scan_glass_2055.png` (not kept in git; 150 dpi, sheet of the run logged as PG2039
above; the printed label is PG2041), [seam crop ×4](scan_PG2041_seams_x4.png).
"BAND 01..04" labels sit 12.9 mm apart (608 Ulf = 304 rows at 600 dpi), the
long verticals and the diagonal continue across all five bands. At the
first seam the two seam marks (band 0's last row, band 1's first row) are
**~16 rows apart**; at the later seams they coincide. Bands 1..4 (non-fresh
passes, lane masks groups 2..39) agree with each other; band 0 (fresh
snapshot, masks groups 0..37) prints two nozzle groups higher. The value
that changes between the first and later builds is being located in the
emulator (`HYBRID_RAM_DIFF`).

## Sheet PG2052: the first complete page (2026-10-26 20:52)

Package `1f07ef5b…8bbe`, `page_live.py` default run: [log](hardware_PG2052.log),
[steps](hardware_PG2052.json), 150 dpi scan (`scan_glass_2110.png`, not kept in git),
[top-left crop](scan_PG2052_topleft.png), [geometry](scan_PG2052_geometry.json),
[rulers](scan_PG2052_rulers.json). 21 bands in 185 s (8 s per band: 0.3 s
prepare, 0.9 s upload, 1.6 s executor incl. two capped waits, 1 s advance),
stock eject, home. All 21 bands, both rulers, the centre cross, the corner
to corner diagonal and the "BOTTOM RIGHT" label are on the sheet.

| Measured | Value | Designed |
|---|---|---|
| Left ruler 10 mm ticks (vertical scale) | 59.0 px at 150 dpi, 0.999 | 59.06 px |
| Band pitch (BAND labels, PG2041 and PG2052) | 12.9 mm | 608 Ulf = 304 rows = 12.87 mm |
| Long verticals at 20 / 89.4 / 118.8 / 158.8 / 178.8 mm | 19.3 / 87.9 / 116.5 / 155.8 / 174.6 mm | horizontal scale ≈ 0.977..0.983 |
| Page left frame from the sheet's left edge | 16.8 mm | 15.9 (x 35008, sheet edge ≈ 29000) |
| Page top from the sheet's top edge | ≈5 mm (sheet edge from the scanner frame, ±1 mm) | 650 Ulf after the loader |
| First seam | band 0 two nozzle groups high (known; blank first pass added afterwards) | |

Horizontal scale: a column is 16 raw CR counts in the executor's window
arithmetic (span 16 N + 640), and that travel is about 2 % less than
1/600 inch on this carriage. The page can be compensated on the host
(resample columns by 1.02) or in the placement; not done yet.

Sheet PG2107 (same package, `page_live.py` with the blank first pass) printed
in 195 s; its scan should show the first seam closed.

## Sheet PG2107: consistent page with the blank first pass (2026-10-26 21:07)

[Log](hardware_PG2107.log), 150 dpi scan (`scan_glass_2120.png`, not kept in git),
[first seams ×4](scan_PG2107_seams_x4.png). `page_live.py` default run on
package `1f07ef5b…8bbe`: fresh snapshot, one all-zero pass (sweep, no ink,
no advance), then 21 real bands as non-fresh passes, 195 s. Every seam on
the page is a single line (band N's last row directly above band N+1's
first row), the diagonal and the per-band staircases continue without
steps. This is the sheet to compare against the stock geometry test: same
layout rules (600 dpi rows, 12.87 mm bands), vertical scale 0.999,
horizontal ≈0.98 (open item above).

Remaining stock dependencies on this path: loader `0xdce50`, LF task/servo,
CR task/servo, allocation `0xadc14`, raster worker `0xa8422`, Q builder
`0xb12a2`, lane preparation `0xe6aea`, executor `0xe6b80`, post-pass tail,
LF request `0xd614c`. Each has a contract in `stock_shim.c`; replacing
them in C is the next phase.

## Colour: stock print mode 7 in the emulator (2026-10-26 night)

Stock's print-mode table (`0x17a56c`, 16 bytes per mode) and slot table
(`0x179b0a`, 24 bytes per mode) give the group slot counts: mode 1 = black
80; mode 6 = C/M/Y 31 each + black 41 with R = 16; **mode 7 = C/M/Y 61 each +
black 81, R = 8**; modes 8..12 smaller symmetric sets; 13/14 colour only.
With the shim's job mode and C/M/Y selection (`0x9a` wIndex bit 11, mode in
bits 12..15; selection matrix groups 0/2/4 entries 2+g → slot g; Q lanes
2/5/7 on managers 0/2/4; `Q+0x350/354` bit 2), mode 7 at 128 and 256
columns in the emulator:

- Pool request 189,248 / 247,552 bytes: **455 bytes per column + 131 KiB
  fixed** (black 81 N; each colour manager 61 × 2N: the colour buffers are
  allocated at **double width**, stride 976).
- Executor returns 1; lane-active bytes `01 01 01 00 00 01 00 01 00 00`,
  "any colour" `0x180cda7a = 1`; DMA bases of lanes 0/1/2/5/7 = each
  manager's raster storage (base + 24 × slots), strides 1296/976, extents
  80/60; black masks groups 2..39, **colour masks 22 groups (176 rows)**;
  raster-feed offsets `28300..` unchanged.
- Mode 13 (colour only) allocates six 40-slot managers and no black.

The arena is 1.5 MiB from package `…` on (RAM bound raised to
`0x20420000`: stock zero-initialises that region and no fixture page lies
in it), so one sweep can carry 2112 columns in colour (half a page) or the
full 4224 in black. Next on paper: `colour_band.py`, one sweep with black
and C/M/Y patterns (colour planes at 2N columns, content in rows 0..175),
RGB scan to measure each colour's span, its offset from black and the
column pitch.

### Sheet CB2159: first colour band (2026-10-26 21:59)

Package `f9e472b2…` + upload-bound fix (1.5 MiB arena, mode 7),
`colour_band.py`: stock load, 650 Ulf, blank fresh pass, one non-fresh sweep
with black (1024 columns) and C/M/Y (2048 columns each) planes uploaded as
tiles, eject. [Log](hardware_CB2159.log), grayscale scan (`scan_CB2159.png`, not kept in git),
[band crop ×3](scan_CB2159_band_x3.png).

**The owner sees blue, magenta and yellow on the sheet (yellow below the magenta, as designed: C/M/Y labels at raster rows 48/96/144).** The app's scanner is
grayscale only (`scan_source_scanner.c`: `.color = false`), and under its
illumination cyan is almost invisible and yellow invisible; a first reading
of the scan wrongly concluded "black only". What the scan does show: the
black plane complete, and the magenta label "CB2159 M" (raster row 96) about
9.3 mm below the black label (raster row 8), i.e. the magenta array prints
≈5.6 mm (≈130 rows) further down the sheet than black for the same raster
row. Cyan and yellow offsets need the owner's reading against a printed
ruler (next sheet) or a colour scan driver.

Emulator facts for mode 7 (256 columns): the executor enables lanes 0,1,2,5,7
(`0xc0c28080 = 0xc0c27830 = 0xc0c51d04/d0c = 0xa7`, `0xc0c27814 = 5`,
`0xc0c27838 = 0xa4`), programs compare windows for lanes 2/5/7 and three
per-colour areas at `0xc0c27510/514/518` and `0xc0c27530/534/538`
([mode 7 writes](executor_writes_mode7_256.json), [mode 1](executor_writes_mode1_256.json)).
Raster-worker plane ids (synthetic packet `+8`): **0x00 → manager 0,
0x08 → manager 2, 0x10 → manager 4, 0x18 → manager 6 (black)**; other ids
are rejected (`a8422` returns 1).

### Sheet CR2225: ruler band on the reused sheet (2026-10-26 22:25)

Same sheet as CB2159, `colour_band.py --first-ulf 1200` (second band
≈13 mm below the first): black mm rulers from raster row 0, each colour a
6 mm line at row 0 and row 168 plus a vertical line at its column.
[Log](hardware_CR2225.log), grayscale scan (`scan_CR2225.png`, not kept in git),
[band crop ×2](scan_CR2225_band_x2.png). Readings from the scan (cyan and
magenta are faint in grayscale, yellow invisible):

- **Colour columns are 600 dpi with the same index as black**: the cyan
  marker designed at 12 mm (column 2·283 of the 2N-wide plane) printed at
  24 mm, magenta's 20 mm at 40 mm. The double-width colour buffers of mode 7
  are capacity only; colour planes must be encoded at N columns.
- **Cyan row 0 ≈ 4.7 mm below black row 0** (≈111 rows); magenta ≈5 mm
  (CB2159 gave 5.6). The row-168 lines are not resolvable in grayscale;
  the owner's readings fill in the spans and yellow.

### Sheet CP2238: first colour page (2026-10-26 22:38)

`colour_page.py`: 2112 columns × 24 bands of 176 rows (89 × 179 mm), one
sweep per band, colour planes sliced 112 rows below black (CR2225 offset),
352 Ulf per band, blank fresh pass first. Black rulers/frame/labels, C/M/Y
bars and their overlaps, colour text, halftone ramps, concentric rings,
per-band registration ticks, four diagonals. 217 s, no errors beyond the
capped waits ([log](hardware_CP2238.log), [steps](hardware_CP2238.json)).
Owner's inspection and a grayscale scan (black + magenta geometry) pending.

CP2238 grayscale scan (full scan `scan_CP2238.png` not kept in git, [rings ×2](scan_CP2238_rings_x2.png)):
black rulers, frame, diagonal and labels complete; the magenta ring is
**dashed**: along its sides it prints 1.9 mm segments (≈45 rows) with a
7.5 mm period (= the 176-row band pitch), so each sweep printed only a
~45-row window of the 176-row colour raster, and the cyan ring (faint)
shows the same dashing. CB2159's cyan ladder, by contrast, covered ≈6.8 mm
of rows when the colour planes were uploaded 2N wide. The colour buffer's
row/column arrangement (double width vs. double row density) is therefore
not settled; a 300 dpi scan of the top 70 mm (`scan_CP2238_top300.png`)
is taken to resolve the 8-row ladders and the per-band ticks.

### Sheet BK2254: black cartridge that stock rejects (2026-10-26 22:54)

The owner installed a black cartridge the stock firmware refuses. Our
cartridge read (`cartridge.c`, stock's connection-response test and cold
preparation) reports black 1 / colour 1 (responds), prepared, ID words
`c4e1419f` (the previous cartridge read `64f12b7f`). `page_live.py --bands 4`
ran the normal path: load, 4 bands, eject, 57 s, no exceptions
([log](hardware_BK2254.log)). The hybrid checks nothing about cartridge
identity; the owner confirmed ink on the sheet ("good").

### Sheet CL2302: colour buffer layout probe (2026-10-26 23:02)

Third band on the CB2159 sheet, `colour_band.py --layout-probe` (colour
planes uploaded 2N wide; per colour a 176-row bar and 16-row stripes in
each half of the buffer; black mm rulers and a 304-row bar).
[Log](hardware_CL2302.log), scan (`scan_CL2302.png`, not kept in git), [band ×2](scan_CL2302_band_x2.png).

| Measured (150 dpi grayscale) | Value |
|---|---|
| Black 304-row bar | 12.87 mm (exact) |
| Magenta bar | top 132 rows below black row 0, height 156 rows |
| Yellow bar (visible as grey) | top 128 rows, height 160 rows |
| Cyan bar | faint; same band of rows by eye |
| Second-half bars (buffer columns N..2N) | nothing printed |
| 16-row stripes | period 1.41 mm (designed 1.35) |

So: colour planes must be uploaded in the 2N-wide layout but only the left
N columns print (uploading N-wide planes, as CP2238 did, left only ~45 rows
per band: the dashed rings); each colour array prints ≈160 rows starting
≈128–132 rows below black's row 0. `colour_page.py` now uses 160-row bands,
a 128-row colour offset and 2N-wide colour planes.

### Sheet CP2314: colour page with the measured geometry (2026-10-26 23:14)

`colour_page.py` after CL2302: 26 bands of 160 rows (320 Ulf), colour
offset 128 rows, colour planes in the 2N-wide layout, 2112 columns, 260 s,
no errors ([log](hardware_CP2314.log), [steps](hardware_CP2314.json)).
Scan analysis (`colour_check.py`: ring continuity, right-edge registration
ticks, frame width) recorded below when available. Build `c7000d85…`
(adds the `0x9b` wIndex=1 synthetic-packet plane id) passes the full
validator ([log](validation_hybrid_c7000d85.log)); the board still runs the
previous build (same firmware except that request).

CP2314 scan (full scan `scan_CP2314.png` not kept in git, [rings ×2](scan_CP2314_rings_x2.png)):
black complete; the colour bars print as ≈2 mm strips and all three rings are
dashed with the 160-row band period, i.e. **each band after the first still
prints only ≈45 colour rows** (raster rows ≈0..45 of each colour), although
CL2302's single real pass printed 160. The 2N-wide layout was not the cause;
something in stock's per-pass bookkeeping after the first real pass
(band tail `0xe6dc6..`, LF accounting, activity statistics, registration)
narrows the colour lanes. Being measured in the emulator (four consecutive
passes, per-pass lane masks / extents / windows).

Paper-free colour probe ([twopass_D_colour](twopass_D_colour.json), mode 7,
1024 columns, each plane a 160-row × 8-column block): four consecutive
passes (fresh, then non-fresh) each count **1280 on lanes 2, 5 and 7** and
656/656 on the black lanes, i.e. every colour row is fed and fired on every
pass; the emulator's lane masks, extents and windows are identical across
four passes too. The ≈45-row colour strips on the pages sit at paper rows
≈104–149 below the band's black row 0 on every band, i.e. where the colour
arrays physically are; the single-pass CL2302 bars (160 rows) are the
exception to explain. Next: one sheet with six variants
(`colour_variants.py`: first pass, after a stock LF step, no LF, fresh
prepare, no post-pass tail, tail without e753c) using the post-pass step
mask of build `d28ecab5…` (`0x9b` wIndex=2).

### Sheet CV2335: six colour variants on one sheet (2026-10-26 23:35)

Build `d28ecab5…` (post-pass step mask). `colour_variants.py` on the
CB2159 sheet below its three bands (650 + 3 × 1200 Ulf first): blank fresh
pass, then per variant a black label + row-0 tick + 160-row reference bar
and, per colour, a 160-row bar and 16-row stripes:

| Variant | Before the pass | Tail mask |
|---|---|---|
| A | first real pass | all |
| B | stock LF 320 Ulf | all |
| C | no LF (same paper position as B, overprints it) | all |
| D | LF 320, then a blank pass | all |
| E | LF 320 | none (band_finish skipped) |
| F | LF 320 | all but e753c |

[Log](hardware_CV2335.log), [steps](hardware_CV2335.json); scan measurements below.

CV2335 scan (full scan `scan_CV2335.png` not kept in git, [bands ×2](scan_CV2335_bands_x2.png)):
**all variants, A included, print the same ≈40-row colour strip** about
5.1 mm below each band's black row 0, with the 160-row black bars joining
into one continuous column. So neither the LF step, the post-pass tail nor
the pass order narrows the colour window. The remaining difference to
CL2302 (160-row colour bars) is the selection height passed at prepare:
304 rows (38 entries per colour group, builder-capped to 22 bits) there,
160 rows (20 entries) on the pages. Next: one sheet with selection heights
160 / 176 / 224 / 304 and identical 160-row content.

CV2335 measured ([colour_bars_check.py](../../../tools/colour_bars_check.py)):
colour strips of 24–32 rows (C 24–28, M 28–32, Y 28–32) at 6.8 mm pitch,
each 5.3 mm (≈125 rows) below its band's black row 0; black bar continuous,
748 rows for the five positions. CR2346 (selection heights 160/176/224/304)
overprinted the previous bands (sheet position estimate off) and is
repeated on a fresh sheet as CR0006 with a fifth variant: selection 304 with
colour content in all 304 rows.

### Sheet CR0006: colour rows follow the selection height (2026-10-27 00:06)

Fresh sheet, `colour_variants.py --rows-variants`: five sweeps with identical
colour content (160-row bars unless noted) and selection heights at prepare
of 160 / 176 / 224 / 304 / 304 with 304-row content.
[Log](hardware_CR0006.log), scan (`scan_CR0006.png`, not kept in git), [bands ×2](scan_CR0006_bands_x2.png),
measured with `colour_bars_check.py` (M and Y bars; C similar but faint):

| Selection rows (entries) | Printed colour rows |
|---|---|
| 160 (2..21) | 28 |
| 176 (2..23) | 44 |
| 224 (2..29) | 92 |
| 304 (2..39) | 156–160 |
| 304, content 304 rows | 156 (the array's 160 rows; rows beyond do not print) |

So the colour arrays correspond to the **upper selection entries** (roughly
entries 20..39 ↔ raster rows 0..159 of the colour manager): a colour band
must be prepared with the full 38-entry selection (304) and carries 160 rows
of content; the band pitch stays 160 rows. `colour_page.py` now does that.
Black is unaffected (selection 304 as before).

## Document printing: `print_doc.py` (2026-10-27)

`app/tools/print_doc.py image|pdf` rasterizes a page at 600 dpi (PyMuPDF for
PDF), separates it into K + C/M/Y (grey-component replacement, 8×8 Bayer
dither), and drives the band path: stock load, 650 Ulf, blank fresh pass,
then per 160-row band two 2112-column sweeps (mode 7, selection 304, colour
planes in the 2N layout shifted by the 128-row array offset), 320 Ulf
between bands, stock eject. `--mono` uses mode 1 with 304-row bands and one
4224-column sweep; `--x-scale 1.024` compensates the measured horizontal
scale; `--preview out.png` shows the halftoned planes. First run: the
synthetic [testdoc.png](testdoc.png) (colour patches, text, a line ramp) as
`hardware_DOC.log`.

### Sheet CP0015: correct colour page (2026-10-27 00:15)

`colour_page.py --x-scale 1.024` with selection 304 / 160-row bands /
offset 128 / 2N layout: 26 bands, 261 s. Scan (`scan_CP0015.png`, not kept in git),
[overview](scan_CP0015_overview.png). All three rings continuous, the C/M/Y
blocks and their overlaps full, colour text and halftone ramps present,
four diagonals, black rulers and frame. First colour page from the hybrid.

### DOC0019: first document (2026-10-27 00:19)

`print_doc.py testdoc.png` (colour): 18 bands × 2 sweeps, 317 s, no errors
([log](hardware_DOC.log)). Scan pending.

DOC0019 scan (full scan `scan_DOC0019.png` not kept in git, [blocks ×2](scan_DOC0019_blocks_x2.png)):
the document printed (patches, text, ramp) with two kinds of white lines:
**band seams** every 6.77 mm (colour arrays print 156–160 of 160 rows; the
colour band pitch is now 156 rows) and, in black, cyan and yellow but not
magenta, **a white line every 1.35 mm = 32 rows** inside every band. The same
32-row lines are on every mode-7 black bar (CL2302, CV2335, CR0006), so
black in the colour mode misses one row group in four; mono pages (mode 1)
do not show it. `print_doc.py --black-mode1` prints the black plane in its
own mode-1 sweep per band (twice the sweeps) until the mode-7 black row
mapping is understood (brief 20 §4).

### DOC2: document reprint with the three line fixes (2026-10-27)

`print_doc.py testdoc.png --black-mode1` (156-row colour bands, halftone
after the horizontal compensation, black in its own mode-1 sweep per band):
4 sweeps per band, log `hardware_DOC2.log`. Expected: no 32-row lines in
black, no seam lines in colour. The first attempt found no paper and
stopped at the loader (no firing).

## Brief 20 applied (2026-10-27 ~01:10, build `e77d6011…`)

From the helper's answer (`fw_dump/gpt/BRIEF_page_path.md`): idle head-work
events `0x1c = 0xc0000000` (bits 31 and 30, stock `c8d30`); the first-pass
mask shift is stock's `b29f2` transition keyed on previous-state byte
`0x1802ef84` (paired with a 32 Ulf feed carry we never applied) → the shim
clears that byte before the first build; colour selection entries map to
colour mask bit e−18 (logical rows 144..303) → entries 18..37 → slots 0..19.
Emulator (256 columns, two passes): black masks `fffffffc ff` on the first
pass already (no −2), colour masks `0xfffff` (20 groups, DMA
`0xfff00000`), lane DMA bases now 64-byte aligned (every pool allocation
carries 64 bytes of slack; `0x9b` wIndex=3 selects an explicit shift for
the alignment test). The blank fresh pass is now opt-in in the tools.

### Loader refusal with a sheet at the sensor (2026-10-27 01:15)

Three loads in a row returned in 0.6 s with no CR/LF request, ASF 0 and
notify `0x1021` result 4: the paper-entry input `0xc0c84e08` read 0 (paper
present, active low) while nothing was loaded, i.e. a tray sheet had been
pushed in as far as the sensor. Stock refuses to pick in that state. The
stock eject (routine 9) moved it out, the input read 1 again, and the next
load worked. Operational rule: a load that returns immediately with ASF 0
means "sheet at the sensor" → eject first.

### Sheet CA0119: raster-start alignment variants (2026-10-27 01:19)

Build `e77d6011…`, `colour_variants.py --align-variants`: nine sweeps, the
manager raster start shifted by 0, 8, 16, 24, 32, 40, 48, 56 bytes from the
allocator's position (24 or 56 mod 64 in mode 7), then 64-byte aligned;
each with a 160-row black bar (mode 7) and C/M/Y bars. Measurement:
`colour_bars_check.py` counts the light lines in each black bar run.

### Sheet CT0142: single pass vs two interleaved passes (2026-10-27 01:42)

DOC0054's scan showed the 1.35 mm (32-row) white lines in the black blocks
printed by **mode-1** sweeps too, and their rows differ from yellow's, so
they are printed features in single-pass solid areas (nozzle-group
geometry that stock hides with multi-pass interleaving), not a mode-7 or
scanner effect. `colour_variants.py --pass-variants`: P1 single-pass bars;
P2A even columns; P2B odd columns, printed 16 rows (32 Ulf) further down
with the raster shifted up 16 rows so every paper row is covered by two
different nozzle groups; P1M single pass again. Measurement: light-line
count per black bar (`colour_bars_check.py`).

CT0142 scan (full scan `scan_glass_0143.png` not kept in git, [bands ×2](scan_CT0142_bands_x2.png);
the sheet's top sat under the lid edge, so P1's bar is only partly
visible): the black reference bar has **no white lines in the first pass
(P1) and the 1.35 mm lines in every later pass** (P2A/P2B and P1M), all
with 64-byte aligned raster starts. Together with CV2335 (lines even with
the post-pass tail skipped) and DOC0054 (lines in mode-1 black sweeps):
the lines are not alignment, not mode 7, not the tail, not the LF; they
come from stock RAM state that the first pass leaves behind (a policy
such as nozzle thinning or an interlace pattern that expects further
passes). The two-pass interleave test (P2A even columns, P2B odd columns
16 rows further) is inconclusive in this scan. Alignment variants CA0119
not scanned (moot).

Next session (no more prints tonight): build `4f8b384b…71a0` (not flashed; board runs `e77d6011…`) adds `0x9b` wIndex=4, a
snapshot-range restore before non-fresh prepares (six ranges: lane records
`0x180cd000..`, builder block `0x1806b000..`, `0x18070000..`,
`0x1802e000..`, band-task/LF/CR state `0x180d0000..`, head work lists
`0x18079000..`), and `colour_variants.py --restore-variants` prints nine
bars (first pass, each range alone, all, none) on one sheet to find the
block that carries the policy. Candidates from the pass-1/pass-2 RAM diff:
`0x180cd570/574` (±94), `0x180cd5c0/5c4` (`01010106`, `00020000`),
`0x180cd404`, `0x180cd444`, `0x180cdc9c/ca0`.

## CV2127: six bar sweeps on package `560e16c8…` (allocator lifetime fix, I-cache on) — 2026-10-27 21:27

Package `560e16c8…` keeps a prepared band's allocations live until the next
prepare (`band_mark`; brief 22 answer §1: executor/status messages used to
overwrite the pattern buffers from the second pass on), arena 1.25 MiB, the
on-device IPP print engine, the power manager, and the I-cache enabled at
boot (scanner package lineage). `colour_variants.py` default run (A first
pass, B after 320 Ulf, C same position, D after a blank pass, E tail skipped,
F tail partial), reused sheet (ghost of a mirrored DOC print underneath).
Scan `scan_CV2127_rgb300.png` (300 dpi RGB, the improved scanner),
[bars ×2](scan_CV2127_bars_x2.jpg), counts in `scan_CV2127_lines.txt`
(`bar_lines.py`):

| Bar (B..F stacked, 800 rows) | white lines | spacing (600 dpi rows) |
|---|---|---|
| K | 20 | 32-33 (one 64, one 99/66 pair = merged/missed) |
| Y | 25 | 32-34 mostly |
| C | 16 | irregular, several 32 |
| M | 8 | ~20, weak |

**The 32-row lines survive the allocator fix** on every pass B..F (as the
helper cautioned: the overwrite was real, not proven to be the cause).
**New: pass A printed only its label (left 8 mm of the sweep) and none of
its four bars.** Routine results were nominal (prepare 1, fire 1, errors
2→3, no exceptions). Timing versus CT0142: fresh prepare 0.4 s (was 3.2 s),
later prepares 0.1 s (0.6 s), first fire 5.8 s (5.7 s), later fires 2.9 s
(2.8 s): this is the first print with the instruction cache on. Open: is
the label-only first pass a cache/timing effect or a package change; the
next batch repeats the first pass with the cache off and runs the
restore-range bisect (`--restore-variants`) now that the allocator no longer
corrupts live bands.

## CR2146: restore-range bisect with the I-cache off — 2026-10-27 21:46 (inconclusive)

Same sheet, `colour_variants.py --restore-variants --extra-advance 2`, cache
off (USB 0x8c 0: 26,000 vs 906,000 instructions per 10 ms). Scan
`scan_CR2146_rgb300.png`, [left half](scan_CR2146_left.jpg), counts in
`scan_CR2146_lines.txt`. The placement went wrong, so the bisect cannot be
read: the two 1200 Ulf extra advances moved the paper only a few mm (each
routine 10 returned 0 after 1.0-1.1 s, the same as a 320 Ulf step), the
sweeps that restored snapshot ranges 0..3 before their prepare (N1, N2, N4,
N8) printed 37 mm further left with their labels off the sheet, N16 landed
on top of N0, and only N32 and N0b sat where expected. Restoring stock RAM
ranges therefore moves stock's CR placement state (expected; those ranges
hold it), and LF advances with the cache off are suspect on this package.

What is still readable: **the first pass (N0) printed all four bars** with
the cache off, unlike CV2127's pass A with the cache on; **yellow shows the
32-row lines in every sweep** (24 lines over the four stacked left sweeps,
4 per 164-row bar in N32/N0b, spacing 32), **magenta never** (0-1), cyan
rarely (1-2), black not measurable here (bars overlapped). Across CV2127
and CR2146 the lines are independent of the allocator fix, the cache
state, the tail, the LF, the restored ranges, and the pass index; they are
lane-dependent (K lanes 0/1 and Y lane 7 strong, C lane 2 weak, M lane 5
clean).
