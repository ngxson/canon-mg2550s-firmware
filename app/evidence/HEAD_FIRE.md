# Stationary black prefire: first head actuation (2026-10-24)

Firmware `b3bf0d3d…` (`app/build/manifest.json`), installed by a normal
protected USB update; bootloader untouched. Module `src/head_fire.c`, tool
`tools/head_spit.py`, suite `tests/validate_head_fire.py` (12 suites passed
before flashing). Register contract: the stock stationary prefire work list
ROM ID 24 as given in `fw_dump/gpt/BRIEF_head_fire.md` (response section 4)
with the waveform rows of `fw_dump/gpt/BRIEF_head_drive.md` (section 3).
Room temperature 20 C, so ambient 40 half-degrees: black selector 3, colour
selector 3.

| Run | Carriage x | Result | Records | Last record completion | ADC black/colour before -> after | File |
|---|---|---|---|---|---|---|
| dry run (no arm) | 3000 requested | carriage stalled at 1152 before the prefire | - | - | - | `head_fire/dry_run_x3000.json` |
| dry run (no arm) | 64 (park) | dry_ok | 2 | - | 755/751 -> 736/736 | `head_fire/dry_run_park.json` |
| prefire | 64 (park, over the cap) | ok | 2 | 16 ms | 755/752 -> 731/736 | `head_fire/spit1_park.json` |
| prefire over a held sheet | 59840 | ok | 2 | 16 ms | 755/752 -> 731/736 | `head_fire/spit2_paper_x60000.json` |

Observations:

- Cartridge fuse ROM read before every run: black rank 37, colour ranks 16/7,
  both stable, connection test 1/1. The firmware's rank gate matched.
- Register read-back after the sequence (dry and real, identical):
  `29850=002900e5 295dc=0014014c 29700=002f0062 295d0=001400ae
  29600=00020002 29608=00000000 29b10=00000063 29ae4=000003e6`, and
  `29aa0/29030/29034/29af0/29ae0 = 0` after the cleanup. The block accepts
  the waveform and control words; `29608` reads back 0 after being written 1.
- With the engine armed, bit 0 of `0xc0c29030` rose on its own, 16 ms after
  the arm for a 100-count record (polled, no IRQ). Both records completed in
  both real runs; no timeout. Whole sequence 590 ms including the ADC reads and
  the 2 ms / 3 ms acquire and release waits.
- The ADC drop of about 20 counts after the sequence is present in the dry run
  too, so it comes from the interface shutdown, not from heating. Black ended
  5 counts lower than colour in both real runs; too small to interpret.
- Paper run: sheet picked and held (`paper_hold.json`, pickup 10144 counts,
  advance 3762), carriage moved from park to 59840 in 2.8 s, prefire, sheet
  ejected (`paper_eject.json`), carriage parked, printer power-cycled.
  Result: **blank sheet**. Repeated with the sheet advanced 8000 counts past
  the handoff (`paper_hold2.json`, eject then 11817 counts, consistent with
  the extra advance) and three consecutive spits at x=59776
  (`spit3_paper_adv8000_*.json`): **blank again**.
- Thermal check, six consecutive prefires at the park position
  (`thermal_park_1..6.json`): black ADC before each run 755, 751, 749, 748,
  747, 746 and after each run 731, 728, 726, 725, 724, 723; colour (not
  selected) constant at 752 before / 737 after. Energy reaches the black head
  only, so the engine does drive the black heaters. The missing ink is then
  either the cartridge (INKLVL 127, the last stock level step; possibly dry
  nozzles after storage) or an ejection condition outside the head-local
  list (superseded by the placement finding and the ink result below).
- **Placement finding (owner watching, 2026-10-25):** at the hold position
  (3762 counts past the handoff) and even 8000 counts further, the sheet's
  leading edge had not reached the carriage line. After hold at 8000 plus an
  advance-only step of 8000 (USB 0x69 mode 3, about 16000 counts past the
  handoff) the owner confirmed the sheet under the head. The dry-page loop's
  assumption that the hold position is the first print line is therefore
  wrong on this unit; the ejection afterwards needed only 3770 counts. Third
  paper run at that placement: `spit4_paper_adv16000_1..3.json`, three
  prefires at x=59840.
- **First ink on paper (2026-10-25, scan `spit4_scan.png`):** two small
  black marks along the paper-travel axis (the nozzle-column direction),
  measured below. So the work engine ejects ink,
  the selector-3 waveform rows are firing pulses, the black cartridge has
  ink, and ID 24 selects only a few nozzle groups (brief 5: record words
  `291e0/291e8 = fffffc00/ffffffff`), not the whole column. The earlier blank
  sheets were paper placement only.

- **Scan-back measurement (`spit4_scan.png`, `-marks.png`, `.json`, via
  `tools/mark_scan.py`, 75 dpi grey, sheet face down on the glass corner):
  two marks at the same across-paper position x = 134.4 mm, centres
  y = 100.9 and 112.5 mm (11.7 mm apart along the paper-travel axis), sizes
  1.7 x 5.1 mm and 1.4 x 3.1 mm, darkness 0.92 / 0.90. A 0.3 mm2 speck at the
  top edge is not ink. The photo's apparent 3 mm spacing was perspective; the
  scan is the reference. Three prefires overlapped at one carriage position,
  so the along-paper extent is the fired nozzle span, about 5 mm and 3 mm, at
  two places about 12 mm apart, consistent with two groups at the ends of a
  roughly half-inch nozzle column.

- **Record comparison (2026-10-25, build `6702a9c3…` with the record-select
  option):** one sheet at the confirmed placement, record 1 alone at
  x = 44800 (`rec1_x45000.json`) and record 2 alone at x = 74752
  (`rec2_x75000.json`); both completed in 16 ms. Scan-back
  (`rec_compare_scan.png/.json`): two sites. Site at x = 93.5 mm: two dots
  about 1 x 0.7 mm at y = 97.4 and 111.3 mm (13.9 mm apart). Site at
  x = 171.6 mm: one mark 1.0 x 3.4 mm at y = 99.1 mm and three 0.7 mm dots at
  y = 108.3, 109.8 and 111.2 mm. The earlier triple run (5.1 and 3.1 mm marks
  11.7 mm apart) is the union of both records plus more ink. Orientation
  (owner): scan top = trailing edge of the sheet (exits last), scan right =
  printer right = park side, so scan x falls as carriage counts rise. Hence
  **record 1 (carriage 44800) = the x = 171.6 mm site (3.4 mm mark plus three
  dots) and record 2 (carriage 74752) = the x = 93.5 mm site (two dots)**.
  Carriage scale from the two sites: 2.61 um per count = 9740 counts per
  inch, within 1.5 % of the 9600 counts per inch constant in stock's
  geometry code (brief 5 section 3), so one 600 dpi column is 16 counts.
  Predicted x for the earlier 59776-count run: 132.5 mm (measured 134.4 on
  another placement). The sheet's right edge (scan x about 212 mm) is at
  about 29200 counts; the paper spans about 29k..110k counts. Both records
  put ink near both ends of the column; record 1 fires more nozzles in the
  group nearer the trailing edge.
  Note: the owner states both cartridges are refilled and full; the fuse
  INKLVL fields are stale bookkeeping.

- **Calibration sheet (2026-10-25, `cal_scan.png/.json`):** row 0 spits at
  carriage 20000 / 60000 / 100000 at one paper position, then two more at
  60000 after 4000-count paper advances each. Sites on the sheet: x = 22.0 mm
  (carriage 100000) and x = 126.2 mm (60000), both at y = 105 mm (row 0);
  x = 124.9 mm at y = 47.5 mm (row 1) and y = 31.6 mm (row 2). The 20000-count
  spit is off the paper (right of the sheet edge at about 29000 counts).
  Carriage scale from 100000 vs 60000: 104.2 mm / 40000 = 2.60 um per
  count, 9750 counts per inch, confirming the 9600 nominal within 1.6 %.
  Paper: row 0 -> row 1 moved 57.3 mm, row 1 -> row 2 moved 15.9 mm for the
  same 4000-count advance (counter deltas 4004 and 4006). Either the sheet
  moved with the motor off (back-driven rollers) or the feed slipped; 16 mm
  per 4000 counts (250 counts/mm, about 6350 counts/inch) is the candidate
  true scale. A creep test with the LF counter watched while idle follows.
- **Colour prefire on the printer (build `6a1b921d…`, `colour_x60000.json`,
  `colour_x80000.json`):** work ID 0x25, two records of 600 pulses on lanes
  2,5,7 then 3,4,6, interval 598. Both completed, 59 ms per record (the
  black list takes 16 ms per 100-pulse record: 160 us per pulse at interval
  998, 98 us at 598, so one interval unit is about 160 ns). Only the colour
  thermistor moved (736 -> 721 -> 716), black steady. Scan-back
  (`colour_scan.png/.json`, grey scan, darkness threshold 0.75): two solid
  marks 4.1 x 8.5 mm and 4.1 x 8.8 mm at x = 97.4 mm (carriage 60000) and
  x = 45.2 mm (80000), same y = 102.7 mm; 52.2 mm per 20000 counts = 2.61 um
  per count. All six colour lanes fire; the along-paper extent of about
  8.5 mm fits a colour column of 160 cells at 600 dpi (6.8 mm) plus spread.
- **Feed creep test (no ink):** after a hold and after each of two
  4000-count advances the LF counter (status word and raw 0xc0c53100) did
  not change over 25 s with the motor off. The rollers are not back-driven;
  the 57 mm versus 16 mm discrepancy on the calibration sheet is therefore
  paper sliding relative to the roller or a transport difference between the
  two steps, still unexplained.

- **Feed scale resolved (`feed4_scan.*`, four black spits at carriage 60000
  with three 4000-count advances):** long marks at y = 101.5, 44.2, 37.6 and
  27.8 mm. The first advance moved the sheet 57.3 mm on this sheet and 57.3 mm
  on the calibration sheet; later advances 7 to 16 mm. Reading: 4000 counts =
  57.3 mm is the roller scale, 1770 counts per inch measured, nominal 1800
  (the carriage measurement was 1.6 % high in the same way), so **3 LF counts
  per 600 dpi row, 69.8 counts/mm**. Then the stock nip-to-nozzle term of
  3762 counts is 53 mm: at the first spit the trailing edge was 101 mm
  upstream of the nozzles, 48 mm upstream of the feed nip, and the first
  57 mm advance carried it out of the nip; from then on only the output
  rollers drove the sheet, slipping. The LF counter never drifts with the
  motor off (creep test), consistent with slip at the output rollers rather
  than at the encoder. Consequences: printable rows end about 53 mm before
  the trailing edge unless the output rollers grip better at speed; a
  13.5 mm black column (320 rows) is a 960-count band advance; the feed hold
  position (3762 past the handoff) puts the leading edge at the nozzle line
  only if the handoff is the nip, which the earlier placement test
  contradicts, so the page job's first-line distance must be re-derived from
  these numbers (about 16000 counts past the handoff was under the head).

Conclusions for the print path: the head-local prefire contract is physically
confirmed end to end (acquire, waveform banks, mode-8 words, arm, completion,
cleanup, release). Still unknown: how a record or band selects individual
nozzles, the nozzle order, the column clock during motion, and the stock
first-line feed distance; brief 6 (`fw_dump/gpt/BRIEF_head_blank.md`) now
asks those.
- A move from park to a target 3000 counts away stalls at 1152 counts (cap
  lip): the controller's kick ends before the carriage unseats and the short
  remaining distance keeps the command low. Targets of 8000 and more, as in
  the dry page loop, work. Spit positions close to the cap should be reached
  by homing only (x = 64) until the controller handles short unseats.

Scope: this exercises the stock prefire only. Pulse energy units, drop count
per pulse and the nozzle map are still not established; see the briefs.

## Mask-mapping sheet, first attempt (2026-10-26, build 9805fdc9)

`tools/mask_map.py run`: three crawl-job rows of 32 entries (5 mm apart,
one record-word bit each, 100 pulses), all 96 fired, feed advance of 1024
counts between rows. The scan (`mask_map_scan.png/.json`) shows the three
rows **on top of each other**: the sheet did not move for the two advances
(slip after placement, as on the calibration sheet). Rows 0 and 1 are
complementary (one bit cleared, one bit set in word A), so their union is a
full column for every bit; the only visible difference is that bits 0..9
give partial, gapped columns and bits 10..31 full 14 mm columns, consistent
with the stock record (bits 0..9 cleared gave 3.4 mm). No bit-to-group map
can be read from this sheet. Next attempt: one pass without any feed, the
A-cleared and B-cleared entries interleaved 2.5 mm apart (`--interleave`).

## Mask-mapping sheet, interleaved one-pass attempt (2026-10-26): the black nozzle map

`tools/mask_map.py run --interleave` on the first sheet turned 180 degrees:
64 entries in one crawl (bit i: word A with only bit i cleared at
x0 + 1920 i, word B with only bit i cleared 960 counts further), 100 pulses,
no feed between entries; all fired. Scan `mask_map_il_scan.png/.json`:
**exactly ten marks**, at the A-cleared positions of bits 0..9
(x = 199.3 mm for bit 0 down to 154.4 mm for bit 9, 5 mm apart), each about
1.0 x 1.7..2.0 mm, with the paper-axis position stepping 1.43 mm per bit
(y = 112.1 mm for bit 0 to 99.2 mm for bit 9). Nothing for bits 10..31 and
nothing for any word-B entry.

Reading: on the black lanes (selection 3) word A `0xc0c291e0` bits 0..9
each select one group of **32 nozzles (1.35 mm at 600 dpi, 1.43 mm
measured with spread)**, cleared bit = fire, ten groups = the 320-nozzle
column, **bit 0 at the leading-edge end** (larger y in the trailing-edge-up
scan). Bits 10..31 of word A and all of word B do nothing here. This also
explains the overlapped first sheet (one bit set in word A clears the other
nine: full columns) and the stock prefire record 1 (bits 0..9 cleared, all
ten groups: the 3.4 mm mark was one run's partial ink). Per-record
vertical resolution is one group (1.4 mm); finer selection, if it exists,
is in the pattern words or the band path, not in these words. Measured map
in `nozzle_map.json`, used by `tools/stamp_plan.py`.

## First text on paper: "HELLO WORLD" (2026-10-26, build 9805fdc9)

`tools/stamp_print.py "HELLO WORLD" --count 15 --ambient 40`
(`hello_world.json`): 66 bitmap columns of a 5x7 font, 50 entries (blank
columns skipped), one pixel = one nozzle group (32 nozzles, 1.33 mm) high
and 512 carriage counts wide, text columns from x = 100000 down toward the
park side, 15 pulses per entry with the stock black waveform, one crawl at
10,000 counts/s: all 50 fired, none missed, 7.3 s. Scan-back
(`hello_world_scan.png`, crop `hello_world_crop.png`): the text reads
"HELLO WORLD" at about 9.5 mm height and 88 mm width, read with the
leading edge at the top; strokes are columns of 0.7 mm wide dots on a
1.33 mm grid, so horizontal strokes are dotted and the last letter's right
side is lighter. Path used: cartridge rank gate, stock prefire setup
(steps 0..4), per entry the stock record programming with word A = the
column's cleared groups, arm, completion poll; cleanup, release, shutdown.
No stock code runs; the register sequence is the one documented in
`src/head_fire.c`.

## Pattern words and feed ruler (2026-10-26, builds e16d86d9 / d997fcef)

`tools/fine_test.py` on the HELLO WORLD sheet (series 1 and series 2 run
separately after a carriage fix; the sheet was not fed between them and the
scan was taken with the sheet turned around; `fine_test_scan.*`).

**Series 1, pattern words:** seven jobs, one entry each, group 5 only,
15 pulses, pattern pairs (0x03000300/0x00030003 stock, 0x01000100/0x00010001,
0x02000200/0x00020002, swapped, 0x0f000f00/0x000f000f, zeros,
0x03030303/0x03030303). All seven marks are one group tall (1.0..1.35 mm)
and 0.7..1.0 mm wide; darkness falls through the series (0.56, 0.40, 0.57,
0.42, 0.34, faint, faintest: `fine_test_pattern_crop.png`). The words modulate
the amount of ink, not the nozzle selection; no sub-group addressing.

**Series 2, feed ruler:** reference pair (groups 0 and 9, 13 mm apart) at
placement, then twenty advance-only feeds of 1024 counts, a reference pair
3.9 mm further across after each (`fine_test_feed_crop.png`). Steps 0..14
(fifteen pairs) lie on the same two lines (y = 25.5 and 38.7 mm in that
scan): **the sheet did not move for fifteen advances (15,360 counts of
roller rotation)**; from step 15 the pairs step down the page by about
15 mm per advance (1024 counts at 70 counts/mm). Transport engages only
after a long roller travel in this state; on the calibration sheet it was
the opposite (first advance 57 mm, then slip). The LF counter follows the
motor in both cases. Paper transport after our placement is not under
control; multi-band printing needs the stock engage conditions (brief 10).

The 150 dpi scan attempt timed out on the IPP-USB bulk write; 75 dpi was used.

## Feed engagement tests (2026-10-26, build 90d4d372)

Automated over USB with the paper-end sensor (`0xc0c84e08`) as the motion
judge (it clears when the trailing edge passes, after about four 1024-count
advances from the placement), plus the feed-bench button map on the panel.
Every variant moved the sheet: bench presses alone; carriage off the cap
(test A); a cartridge read (head-interface acquire/release/shutdown) before
the advances (B); a dry stamp job before (C); a dry job with keep (D); real
firing jobs alternating with advances (E); six dry 32-entry crawls with
keep, an advance after each; the same with real count-100 firing
(`feed_replica_scan.png`: six rows of marks 14.3 mm apart, 1024 counts at
70 counts/mm). The earlier non-moving sheets (mapping rows, feed ruler)
are therefore not reproduced by any firmware sequence; the remaining
suspect is mechanical at the output side (the leading edge hanging half
out after placement and catching), which no sensor sees.

## Band-path mask registers 0xc0c27570.. with the stationary engine (2026-10-26)

Written through the register tunnel before single-group fires (group 5,
15 pulses, one entry per case at 2500-count spacing): lane-0 triple
untouched (0), zero, all ones (the third word reads back 0x1), bit 0 only,
low 16 bits; then the lane-1 triple zeroed. Scan `mask27570_scan.*`: the
first five marks are identical one-group marks (0.7..1.0 x 1.35..1.7 mm);
the lane-1-zero case left no visible mark (see the pixel check in the memo
if it was merely faint). The lane-0 masks do not change what the work
engine fires; these registers belong to the band path. Finer-than-group
selection is not available through the stationary engine with any word
tried so far (record words, pattern words, 27570 masks).

## Lane-1 mask words, first mapping row (2026-10-26)

`tools/mask_lane1.py`: eight single-group fires (group 5, 15 pulses) under
lane-1 triples (0xc0c27580/84/88): all ones; word0 low 16; word0 high 16;
word1 only; word2 only; word0 bits 0..3; bits 0..1; bit 0. Scan at 75 dpi
(`mask_lane1_scan.*`), marks 6.5 mm apart from x = 189.1 mm: all ones 0.7 mm
tall (!), low16 1.35, high16 1.7, word1 1.7, word2 1.35, bits 0..3 1.7,
bits 0..1 none, bit 0 none. Fifteen-pulse marks are too faint and 75 dpi too
coarse (a 32-nozzle group is four pixels) to read sub-group structure; the
only firm facts: zero masks suppress the fire, one or two enabled bits give
no visible ink, four or more give a full-looking group mark. Next row: count
100 and contiguous byte ranges per word.

## Lane-1 mask words, byte-range row at count 100 (2026-10-26)

`tools/mask_lane1.py run --cases ranges --count 100 --pitch 2000`: thirteen
single-group fires (all ones, then each byte of word0, word1, word2 alone).
Scan `mask_lane1_ranges_scan.*`: thirteen marks, every one 1.0..1.35 mm
wide and 2.0 mm tall (1.7 for the two ends), centroids within 0.3 mm of each
other, ink within 15 %. **No word or byte selects a sub-range of the
group**; the lane-1 words gate the amount fired (zero = nothing, one or two
bits at 15 pulses = nothing visible, any byte at 100 pulses = the whole
group). Together with the record words (ten groups of 32), the pattern words
(ink amount) and lane 0's words (no effect), the stationary work engine
offers no finer selection than a 32-nozzle group. Finer vertical resolution
needs the band path (brief 11).

## Two lines: "HELLO" and "WORLD" with a band advance (2026-10-26, build 90d4d372)

`tools/stamp_print.py "HELLO|WORLD" --count 15`: one crawl per line (25
entries each, all fired), one advance-only feed of 768 counts (7 groups of
32 rows plus a 32-row gap, 3 counts per row) between them, on the reused
sheet turned around. Scan `hello_two_lines_scan.png`, crop
`hello_two_lines_crop.png`: the two lines are separated by the planned band
pitch and read in order with the leading edge at the top. Multi-band
printing on the open path works when the transport engages; the earlier
non-moving sheets remain the open question of brief 10.

## Stock-like pickup handoff (2026-10-26, one sheet, `scans/stock_hold*`)

Brief 10 gave stock's post-edge pickup travel A = 1328 Ulf = 7968 counts and
forward alignment T = 627 + 19 Ulf = 3876 counts; our handoff had been 2048.
Firmware defaults changed (`paper_feed.c` HANDOFF_DEFAULT 7968, ADVANCE_DEFAULT
3876), suites passed, flashed (`47f078c2`). Test: `paper_feed.py hold`
(defaults, pickup 16280 counts, one attempt), record 1 prefire at x=59840,
advance-only 6976 counts, record 1 prefire at x=45184, eject (8892 counts),
park. Scan (75 dpi, top = trailing edge, right = park side):

| mark | carriage | scan x | scan y (centre) | length |
|---|---|---|---|---|
| first (placement) | 59840 | 143.2 mm | 281.4 mm | 13.9 mm |
| second (+6976 counts) | 45184 | 181.2 mm | 180.6 mm | 14.2 mm |

Sheet 296.7 mm long in the scan, so the first column spans 8.4..22.3 mm from
the leading edge (centre 15.3 mm): the stock-like handoff puts the nozzle
line about 5 mm further down the page than stock's 3 mm top margin, and the
sheet was properly in the transport (100.7 mm measured for 6976 counts =
69.3 counts/mm, matching the 69.8 calibration). The earlier "16000 counts
past the handoff" placement was slip after the marginal 2048 handoff, not
geometry. Carriage: 14656 counts = 38.0 mm = 386 counts/mm (9800/inch).
`stamp_print.py` and `fine_test.py` now use the default placement.
