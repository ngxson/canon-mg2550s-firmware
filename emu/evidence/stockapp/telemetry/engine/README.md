# Engine-state regions (telemetry ABI 3) — 2026-10-25

Regions 7–11 come from the answered RE brief 12
([fw_dump/gpt/BRIEF_message_ids.md](../../../../../../fw_dump/gpt/BRIEF_message_ids.md)):
the reported-status cache at `0x1803a790` (296 bytes; DJS category nibble at
`+0x0f`, substate bits 4–8 of the u16 at `+0x10`, PID at `+0x13..+0x15`, CC
override at `+0x124`), print geometry at `0x1806c974` (1/600 inch), the ASF
state byte `0x180c6f54`, the LF position word `0x180d5790` and the saved
paper-edge coordinate/state at `0x1879b98c`. `tools/stocktrace.py --engine`
reads and decodes them (DJS mapping per the brief: category 7 with substate
4/25 → `WP`, 5 → `LD`, 6 → `PR`, 7 → `EJ`, 0–3/11 → `ID`; otherwise `NO`,
substate 20 → `S1`; CC override wins). Installed package
`f4eb8d476de3e4be78edc56827e557922d399f46b81d4d67c5a9496c1e6f382d`.
Provenance: [manifest.json](manifest.json).

## Idle and scan

`idle_engine.jsonl` and `engine_during_scan.jsonl` (5 Hz, 91 snapshots
around a 50 × 50 mm, 150 dpi scan; USB state `[4,1,1,1]` throughout):

- Idle after the power cycle: DJS `NO` (category 0/1, substate 0), PID
  `0 0 0`, all twelve geometry fields zero, LF position 3095, edge coordinate
  `0x6fffffff` (sentinel) with edge state 1, ASF byte `0xff` on the first
  read after install and 0 afterwards.
- During the scan nothing in the print-engine fields moved. The only change
  in the status cache was byte `+0x127` going 1 → 0 at scan-session start
  (`0xdb20`) and back to 1 at `AbortSession`/`EndJob`. That byte is therefore
  a scanner-idle flag in the reported-status cache; it is not in the brief's
  field list and is noted here as a physical observation only.

## Print — geometry sheet at 600 dpi, 10 Hz engine polling

`engine_during_print.jsonl` (443 snapshots, 66 s) with
`print_during_engine_trace.jsonl`; USB state `[4,1,1,1]` throughout, EndJob
acknowledged, page printed.

| t | DJS | cat/sub | PID | ASF | LF position | edge | note |
|---:|---|---|---|---:|---:|---|---|
| 0.0 s | NO | 0/0 | 0 0 0 | 0 | 3095 | sentinel/1 | idle |
| 4.1 s | **WP** | 7/**4** | 2 0 0 | 0 | 3095 → 53095 → 11983 | sentinel/1 | geometry latched (below); host OUT NAKed 33 s |
| 37.4 s | ID | 7/3 | 2 0 0 | 0 | 11983 | | 0.1 s transition |
| 37.6 s | LD | 7/5 | 2 1 0 | 0 | 218 | | paper load, LF counter restarted |
| 39.6 s | PR | 7/6 | 1 1 0 | 1 | 871 … 12519 | | printing; host finished sending at 51.0 s |
| 56.1 s | PR | 7/6 | 1 1 0 → 3 1 0 | 1 | 13159 | **12589 / 2** | trailing edge latched |
| 57.5 s | EJ | 7/7 | 3 1 0 | 1 → 0 | 16185 | 12589 / 2 | eject |
| 57.9 s | ID → NO | 7/0 → 0/0 | 0 1 1 → 0 0 0 | 0 | 16185 | | job end |

**Geometry (1/600 inch), latched at `WP` and unchanged afterwards:**
requested area `x=83 y=75 w=4791 h=6816`, requested paper `4961 × 7016`;
**active area `x=83 y=75 w=4791 h=6545`**, paper `4961 × 7016`. Stock
shortened the height by 271 rows: `75 + 6545 = 6620 = 7016 − 396`, i.e. a
bottom margin of 396 units = **16.76 mm**, and 6545 rows = **277.1 mm**,
which is exactly where all three printed sheets stopped (277.2 mm measured).
The bottom cut-off is therefore a geometry clamp applied by stock at job
setup for this job's media/mode, not an engine or sensor limit. The brief's
margin check of 118 units (5 mm) is a different, smaller value; the 396-unit
figure must come from a media/mode table that is still to be located.

**`WP`:** substate 4, not 25, for the whole 33 s. During it the LF position
word moved 3095 → 53095 → 11983 before being reset to 218 at `LD`, so the
feed motor ran for most of the wait. That is consistent with a maintenance
cycle (purge pump and wiper on this class of printer are driven through the
LF motor) rather than a passive delay; this is an inference from the LF
accounting, not an identified routine. The two earlier jobs had no such
wait and loaded within 0.3 s.

**Feed accounting:** LF 218 at load, 871 at first print, 12519 by the time
the host had finished sending; the saved edge coordinate became 12589 with
edge state 2 about 5 s after the host's EndJob, and the final position after
eject was 16185. Units are not established (the brief lists the conversion
as unknown); 12371 units from load to edge latch is more than the 7016
600-dpi rows of A4, so the word is not 1/600 inch. Raw values are preserved.

**PID bytes:** `2 0 0` from job accept through `WP`/`LD`, `1 1 0` while
printing, `3 1 0` for the last pass and eject, `0 1 1` at `ID`, zeros idle.

## Scan of this print

`scan_of_geometry_print2_engine_run_300dpi.jpg` (second print of the geometry
sheet, scanned 300 dpi full platen): origin 2.96 mm from the left paper edge
and 3.47 mm below the top edge, last full-width bar 276 mm, lowest ink
277.6 mm below the origin = row 6558 at 600 dpi, against the latched active
height of 6545 rows (277.1 mm). Scan and latched geometry agree.

## Why the active height is 6545: verified in code and in the emulator

Following the user's request to look inside the stock app rather than wait,
the clamp was located and executed:

- **Code (VERIFIED):** the function at `0xaac50`, called once from the
  accepted-page setup `0xaacaa` at `0xaaeb0`, computes
  `paperH − (Y + H)` and, if that is below the immediate `0xff + 0x8d = 396`
  (built at `0xaac86`/`0xaac8c`), stores `H = paperH − Y − 396` to
  `0x1806c97a` and to the bookkeeping word at `+0x6c` (`0xaaca4`/`0xaaca6`).
  It reads no media, ink or mode fields; the resolution test just before it
  (`1200`/`300`) only scales a group-advance byte. The 118-unit check in
  `0xaacaa` sets a flag and does not replace this clamp.
- **Emulator (VERIFIED):** `emu/output/stockapp/clamp_fixture.py` runs that
  function in Unicorn on a synthetic geometry struct; with `Y=75 H=6816
  paperH=7016` it writes 6545, leaves `H=6545`/`6000` unchanged, and clamps
  `paperH=6600` to 6129, i.e. `min(H, paperH − Y − 396)` in every case.
- **Hardware (VERIFIED):** telemetry ABI 3 latched exactly these values on
  the printed job, and three scanned sheets stop at 277.1–277.6 mm.

RE brief 13 ([fw_dump/gpt/BRIEF_bottom_margin.md](../../../../../../fw_dump/gpt/BRIEF_bottom_margin.md))
independently reached the same instructions and adds: the bottom distance is
a literal, not a media-table entry, and no traced setup branch skips the
call, so with truthful A4 dimensions and `Y=75` at most 6545 rows print
through this path. Only a larger declared paper height changes the
arithmetic, and that is not a verified way to print farther on real A4.

## Units and labels from RE brief 13, now used by the decoder

- `0x180d5790` is a signed software position in **Ulf** = 6 controller
  counts (`0xd931c`/`0xd9322`: `(raw − base − 0x700000) / 6`); 1/1200 inch
  is an inference, not a calibration. The decoder names the field
  `lf_position_ulf`.
- The saved edge `s32[0x1879b98c]` uses the same units; state byte 1 = not
  latched, 2 = edge captured. Eject finishes at `saved_edge + 3596 Ulf`
  (`0x180c6ef0`), with a 2197-Ulf preliminary target; the trace's
  `12589 + 3596 = 16185` matches, and the decoder reports
  `eject_remaining_ulf` while state 2 holds.
- Forward alignment target: `627 + s16[0x180c6f98] + s16[0x180c70ec] + mode
  correction` Ulf, so 218 and 871 are not fixed constants.
- PID byte 0 maps notification values to 0/2/1/3 (idle / preparing-loading
  / printing / finishing, inferred); bytes 1–2 are copied page values that
  became 1 at `LD` and after eject.
- `WP` is category 7 with substate 4 (notification key `0x1058`, value
  `0x8005`) or 25 (`0x801a`); `LD/PR/EJ` are values `0x8006/7/8`. The
  producer of the substate-4 notification and the reason for the 33 s wait
  remain unknown; the label is "pre-print wait, substate 4".
- Status-cache byte `+0x127` (`0x1803a8b7`) is a session availability latch
  cleared by the session handler at `0x3a10e` and set back at session end;
  the decoder exposes it as `session_available`. It is not scanner progress.
