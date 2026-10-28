# Hybrid (shape B) flash layout: our app inside holes of the stock image

Decision 2026-10-26: no flash chip swap; the open application overwrites stock
regions that the borrowed routines never use.

## Facts the layout rests on

- Flash is memory-mapped at address 0 and stock executes from it in place
  (XIP). Our bootloader reads it memory-mapped too (`bootloader/src/flash.c`),
  and the running-stock package jumps to `0x11960` in flash from a RAM shim.
- The layout-2 bootloader validates the MGAP header at `0x10000` (load
  address fixed at `0x20020000`, entry offset fixed at 0), copies `length`
  payload bytes from `0x10020` to `0x20020000`, and calls `0x20020000`
  (`bootloader/src/service.c`, `integrity.c`). Flash byte `F` of the payload
  therefore also exists in RAM at `0x2000ffe0 + F`.
- The running-stock package already replaces stock bytes `0x10000..0x10023`
  with the header plus a branch and relocates the three displaced delay
  helpers; stock leaves `0x10110..0x11000` erased (`emu/stockapp/build.py`).
- Flash coverage of the emulated stock boot plus all maintenance fixtures
  (`emu/evidence/stockapp/maintenance/flash_coverage.json`): boot executes
  432 KiB and reads 353 KiB of data; the maintenance routines execute 68 KiB
  and read 36 KiB. 816 KiB of the application area is untouched by both
  (`flash_free_candidates.json`), with these largest runs:

| Flash range | Size | Content hint |
|---|---|---|
| `0x199000..0x1d8000` | 252 KiB | tables, repetitive byte patterns, no text |
| `0x16a000..0x17a000` | 64 KiB | USB job strings (`StartJob`, `SendData`, …), buffer-size diagnostics |
| `0x1f2000..0x200000` | 56 KiB | erased |
| `0x15c000..0x169000` | 52 KiB | tables |
| `0x075000..0x081000` | 48 KiB | code (unit-test strings) |
| `0x142000..0x14e000` | 48 KiB | code |

"Untouched" means never executed or read in those emulator runs. Coverage
grows when more routines are borrowed (the band path is next); the hole must
be re-checked against each new fixture before a package is built.

## Proposed layout (no bootloader change)

| Flash | Content |
|---|---|
| `0x000000..0x00ffff` | Bootloader stages, unchanged |
| `0x010000..0x01001f` | MGAP header: length `0x1effe0` (whole remainder, as the stock package does), load `0x20020000`, entry 0 |
| `0x010020` | Branch to the hybrid entry stub (stock bytes relocated as today) |
| `0x010110..0x010d00` | Hybrid entry stub: stack setup, jump into the app in its RAM copy |
| stock image | Unchanged wherever any borrowed routine, its data, or the initialized-data stream `0x1eeadc..0x1f1c4e` lives |
| `0x199000..0x1d8000` | Open application image, linked at RAM `0x201a8fe0` (= `0x2000ffe0 + 0x199000`), so the bootloader's own copy places it; today's app is 95 KiB |
| `0x16a000..0x17a000`, `0x1f2000..0x200000` | Spare: large buffers or a second image slot |

The app keeps its current build and only changes link address; its startup
copies nothing. Stock routines are called at their flash addresses. The RAM
regions stock uses (`0x18000000` banks, `0x1802c000` scatter data,
`0x18089900` motion profiles) are initialized by the shim, either by running
the stock initializers named in briefs 16/17 or by restoring a RAM snapshot
taken in the emulator; that choice is open.

## Update path

The package is the full `0x10000..0x1fffff` region, uploaded with the normal
app update (`flash_usb.py`, 19 s for the stock package). The header is
committed last, so a failed upload leaves the old app valid. Stage 0 and the
I²C recovery path are untouched.

## Status

Built by `app/hybrid/build.py`, emulator-checked by `validate_hybrid.py`,
installed on the board 2026-10-26 ([evidence](evidence/INSTALL.md)). The
entry word is a direct ARM branch; no stub was needed.

## Still to do

1. Coverage of the band path (brief 11) and of cap/uncap/clean variants not
   yet traced, to confirm the hole before each new borrowed routine.
2. A static reachability check from the borrowed entry points over the hole
   (helper brief, seeded with the dynamic coverage).
3. The shim proper: owned status model, CR/LF adapters, event flags, RAM
   initialization of the stock state the routines read.

## RAM bound (2026-10-26 night)

The app's bss bound moved from `0x20280000` to `0x20420000` for the 1.5 MiB
stock-pool arena (colour bands). Evidence that stock does not use
`0x18270000..0x18420000` (the alias of that bss): the scatter table zero-fills
it at boot (no initialised data), the fixture RAM snapshot (62 pages from the
maintenance, band and feed runs) has no page between `0x18200000` and
`0x18771000`, and the shim's RAM init skips our app/bss range. The same
argument placed the app itself.

## RAM snapshot storage (2026-10-28)

The boot-state snapshot pages are stored word-run encoded (`zero_rle` in
`build.py`: 121344 raw bytes become about 91 KB) and packed behind the
application inside the hole, overflowing into the erased stock tail from
`0x1f3000`; the page table (magic `SNAP`, count, raw bytes, format 1, then
RAM address / absolute flash source / decoded length per page) is the tail
sector `0x1f2000`. The shim decodes a page in `snapshot_copy()`. This frees
the hole's fixed 132 KiB application limit: the app may grow to roughly
220 KB before the encoded pages no longer fit. Only the hole and the erased
tail are written; the maintenance-coverage "untouched" sector runs are not
free for the print path (`HYBRID_FLASH_READ_WATCH` showed the band builder
reading `0x16b000`, `0x16c000`, `0x178000` and `0x179000` during a job, and a
trial with pages there crashed the loader).

