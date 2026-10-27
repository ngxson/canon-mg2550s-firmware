# Stock band path run as a fixture (2026-10-26, towards a stock-quality page)

Emulator only. `emu/stockapp/band_fixture.py` replays RE brief 8's executed
recipe on the fixture harness and then runs the stock band executor on the
descriptor it produces. Reports: [`band_pass_fixture.json`](band_pass_fixture.json)
(executor entry `0xe6b80`, slot flag 0 set) and
[`band_task_entry_fixture.json`](band_task_entry_fixture.json) (task entry
`0xe6d44`, no flag).

```sh
docker run --rm --network none -v "$PWD:/project:rw" mg2550s-emu \
  emu/stockapp/band_fixture.py --gate-index 0 --output emu/output/fixture/band_gate0.json
```

## What runs

| Step | Call | Result |
|---|---|---|
| 1 | `0xadc14(job)`, job per brief 8 §1 (mode 1, width 320, 600 dpi) | returns 8 after 108,543 instructions; manager 6 = 80 slots, width 320, stride 1280, size 25,600, base in the fixture's scratch. Same values as brief 8. Needed: semaphore polls (timeout 0) answered with -50 so the drain loop ends; the kernel privilege/cache helpers around the uncached allocation (`0x1509e4`, `0x150bf4`, `0x150bfc`, `0x150c04`, `0x150080`) and the `svc #0` wrappers (`0x150850`, `0x150870/4`, `0x15089c`) stubbed, because the harness cannot take real SVC exceptions with correct register banking |
| 2 | `0xa8422(P)`, packet per brief 8 §2 (8 rows, 320 bytes, direct memory) | returns 2 after 5,533 instructions with 170 register writes (RB2PB converter programming, `0xc0c21000..`, `0xc0c25xxx`); the converter is not modeled, raster storage stays zero |
| 3 | `0xb12a2(0)` with selection matrix all -1 except group 6 row 18 = slot 0, R = 8, D = 600 | reaches the publication call at `0xb1aa0` with r0 = 0x2d and Q (0x494 bytes, 47 nonzero words); returns 1 |
| 4 | `0xe6b80(Q)` | returns 1 immediately unless one of the ten band-slot bytes at `0x180cda70..79` is 1 (gate `0xe3a1c`); with byte 0 set it runs 30,118 instructions, 51 service calls and **323 register writes**, then requests a CR move to target 0, profile 0 and waits on `0x1a:0x14` |

`0xe6d44(Q)` (band task entry) runs 17,048 instructions and 26 services with
no register writes: it prepares and queues, the executor runs from the task
loop.

## The band pass register sequence (step 4)

Ordered writes, grouped (full list with program counters in the JSON):

- **Head interface pulse** `0xc0c20000 = 0x80`, then 0.
- **Raster feed block `0xc0c27xxx`** (`0xe493a..0xe49ec`, ten lanes): per
  lane i, `0xc0c27000+4i` = buffer address, `0xc0c27100+4i` = stride,
  `0xc0c27200+4i` = second address, `0xc0c27300+4i` = extent-1, `0xc0c27400+4i`
  = 0, mask triple `0xc0c27570+0x10·i`. Lanes 0 and 1 (black) get the slot
  address `0x101902a0`, stride `0x500`, extent `0x4f` (80 columns) and mask
  `0xfffeffff, 0xffffffff, 0xffffffff`; lanes 2..9 zero with masks all ones.
  Then `0xc0c27814 = 1`, `0xc0c27ac0/ac4 = 0`, `0xc0c27500..0xc0c27554 = 0`,
  `0xc0c27ae0/ae4`, `0xc0c27830`, `0xc0c27b00..b08 = 0`, `0xc0c27810 = 0x100`,
  `0xc0c27820 = 0xff`, `0xc0c27838/848 = 0`, `0xc0c27720..0xc0c2775c = 0`,
  `0xc0c27840..868 = 0`, `0xc0c277e8 = 0x404`.
- **Block `0xc0c28xxx`** (`0xe50dc..0xe514a`): `0xc0c28000 = 0`,
  `0xc0c28080 = 1`, `0xc0c280c0..0xc0c2814c = 0`, `0xc0c28200 = 9`,
  `0xc0c28210..0xc0c28220 = 0x9f0000` (five range pairs from Q+0x394),
  `0xc0c28000 = 1`.
- **Head engine `0xc0c29xxx`** (`0xc3526..0xc3632`, the same writer our
  head_fire.c translates): three 16-word waveform banks at `0xc0c29850`,
  `0xc0c29700`, `0xc0c297e0` with tails `0xc0c295dc/d0/d8`, then
  `0xc0c29190 = 0`, `0xc0c29580 = 1`, `0xc0c291a0 = 3`, `0xc0c295f0 = 8`,
  `0xc0c29584 = 0`, `0xc0c295b0 = 0x80c0009`, `0xc0c29600 = 0x20002`,
  `0xc0c29608 = 1`, `0xc0c29d50 = 0x58`, `0xc0c29174 = 0x121c12`,
  **record words `0xc0c291e0 = 0xc0c291e8 = 0xfffffc00`** (both black lanes),
  `0xc0c29ac0 = 0`, `0xc0c290a0 = 1`, `0xc0c290b0 = 0xfc`, `0xc0c290c0 = 0x58`,
  `0xc0c29ab0/ac8 = 0`, pattern words `0xc0c29230 = 0x8000300`,
  `0xc0c29234 = 0x80008`, `0xc0c29238 = 0`, `0xc0c29280..288 = 0`,
  `0xc0c29200 = 0x11111100`, `0xc0c29204 = 0`.
- **Sweep windows** (`0xe55a4..0xe5624`): `0xc0c293a0 = 1`, `0xc0c293a4 = 0xff`,
  `0xc0c293b0 = 0x11`, `0xc0c293b4 = 0`, `0xc0c29400..0xc0c2941c = 0`,
  `0xc0c29450..0xc0c2946c = 0`, `0xc0c294a0..0xc0c294bc = 0`, `0xc0c29ca0 = 0`.
- **CR position compares** (`0xe5aae..0xe5b4e`): `0xc0c51400 = 0`,
  `0xc0c51404 = 0`, `0xc0c51500 = 0`, `0xc0c51d34 = 0x20ac0`,
  `0xc0c51d38 = 0xc40`, window tables `0xc0c51700/800/900/a00 + 4k` for
  k = 0..9 with only entry 0 nonzero (`0x20ba8`, `0xbac`, `0x20ba8`, `0xbac`),
  `0xc0c51d04 = 1`, `0xc0c51d0c = 1`, `0xc0c51500 = 0x90000`,
  `0xc0c51108..110 = 0xc40`, `0xc0c51114 = 0x20ac0`, `0xc0c51604/608/614/618/630/62c/600 = 0`,
  VIC `0xc0130008 = 0x2920`, `0xc0c51400 = 0x1e`, `0xc0c51504/508 = 0`.
- Then the **CR request**: `0xd1f70(u32[0x180cda4c], u32[0x180cda54], 0, 0)`
  became a `0x4041` move to target 0 with profile 0, because the band
  scheduler state at `0x180cdaxx`/`0x180cdd60` was never filled; the executor
  then waits for `0x1a:0x14`.

## What this does and does not establish

- The register sequence of one black band pass as the stock executor emits it
  for an 80-column, 600 dpi, mode-1 job with a hand-made selection and an
  empty scheduler state. Values that depend on that state (sweep start,
  profile, window positions `0x20ba8/0xbac/0xc40/0x20ac0`, range pairs
  `0x9f0000`) are not a usable geometry.
- The raster slot was never converted (no ASIC in the emulator), so the
  buffer content behind `0x101902a0` is zero; on hardware the same path
  would run the RB2PB converter.
- No head fired, no carriage moved. The CR adapter was dry.
- The fixture's kernel stubs (semaphore -50, cache helpers, syscall wrappers)
  are emulator conveniences; on hardware stock's privilege helpers really do
  switch the CPU to user mode after an uncached allocation, which the firmware
  shim must accommodate before this path can run there.

Open for brief 11 (addendum appended there): the scheduler fields the Q
builder and the executor read for positions (`0x180cda4c`, `0x180cda54`,
`0x180cdd60`, Q+0x350/0x354/0x394..), the ten band-slot flags at
`0x180cda70`, and the slot/raster byte layout the `0xc0c27xxx` feed consumes.

## Scheduler words confirmed, hybrid blocker (2026-10-26, late)

- `--set 0x180cda4c=40000 --set 0x180cda54=3` turns the executor's CR request
  into target 40000, profile 3: **`u32[0x180cda4c]` is the sweep target and
  `u32[0x180cda54]` the profile** read by `0xe3c76 → 0xd1f70`. The compare
  windows (`0x20ba8/0xbac/0xc40/0x20ac0`) did not change, so they come from
  other state (`0x180cdd60..`, Q+0x394..); brief 11 addendum question 6.
- The same steps were added to the firmware shim as routine 5
  (`hybrid_call.py run band-prepare`: allocation, raster worker, Q build,
  no executor). In the hybrid emulator it stops in the stock allocator at a
  NULL C++ pool object (`0x109936`: `ldr r0,[r0,#4]` on a pool wrapper whose
  pointer only stock's static constructors set). The maintenance routines
  needed three such singletons (two NVM managers, the system-services table),
  which the shim now fakes; the print subsystem needs the real object graph
  (pools, mailboxes, managers). Next step: run stock's static-constructor
  table (`__rt_entry` path from `0x11ab8`, constructors loop around
  `0x14f8b8`) under the shim in the fixture first, and veneer whatever kernel
  "create" calls they make.

## With brief 11's recipe (answered 2026-10-26 10:43): a coherent pass

`band_fixture.py --prepare` applies the answer: valid BK extents
(`Q+0x56/0x58 = 0`, `Q+0x6a/0x6c = 1280`), raster addresses inside the
allocation (`B = base+0x780`, end `B + 19·1280`, stride 0x500, count 0x4f),
explicit profile (`Q+0x52 = 0x83`), forward (`Q+0x54 = 1`), placement
`Q+0x3a = 5000`, masks `0x10000,0,0`, unused lanes marked invalid, common
offsets zero with the cache byte valid, optional staging off, the CR profile
table copied from ROM `0x1847c8` to `0x18089900`, then `e6aea(Q)` (lane
preparation `e3ad6`) before `e6b80(Q)`. Report:
[`band_pass_prepared.json`](band_pass_prepared.json).

| Result | Value |
|---|---|
| Active lanes `0x180cda70..79` | `1,1,0,0,0,0,0,0,0,0` |
| Scheduler A: bounds / approach / final / target / dir / profile | 50346..55588, 50426..55668 / 49254 / 2688 / 56744 / 0 / `0x103` |
| Executor | returns 1 after 28,638 instructions, 323 register writes |
| `0xc0c27000/004` (BK lanes), `0x27100`, `0x27200`, `0x27300` | `B = 0x100f07a0`, `0x500`, `B`, `0x4f` |
| `0xc0c27810`, `0xc0c28080`, `0xc0c280c0` | `0x104`, `3`, `0xff` (the prepared values, not the bare capture's) |
| `0xc0c51700/800/900/a00` lane 0, lane 1 | `0xd422, 0xe822, 0xd4a2, 0xe8a2`; `0xd49c, 0xe89c, 0xd51c, 0xe91c` |
| `0xc0c51d34/d38`, `0xc0c51108..114` | `0xd340, 0xe9c0`; `0xe9c0, 0xe9c0, 0xe9c0, 0xd340` |
| `0xc0c51500/504/508`, `0xc0c51d04/d0c` | `0x90106, 0x2e, 0x5c` (mode 7), `3, 3` |
| CR request | `0x4041` target 56744, profile `0x103` |

Lane 1's windows sit 122 counts after lane 0's, which is 2 × (6880 − 6819),
the registration corrections the helper reported, and the window span is
5120 counts for 320 columns as the answer states. The absolute placement is
16,708 counts higher than the helper's example because the cached common
offset (`e4380`, byte `0x180cda88`) was marked valid without being computed;
the structure is otherwise the helper's. Still emulator only: no raster
content, no motion, no firing.

## Hardware: the RB2PB converter ran under the shim (2026-10-26, package 1bbf79e6)

`hybrid_call.py run band-prepare` on the board (stock RAM restored from the
emulated-boot snapshot, 49 runs / 87 KiB in the flash hole): stock allocation
returned 8 with manager 6 at base `0x2020dd60`, the raster worker converted the
synthetic 8-row packet with the real RB2PB engine (its memory-operation and
conversion completion handlers `0x9f19a`/`0x9f592` were run in place by the
shim's event wait), and the Q builder published `Q = 0x202152a0`. 0.3 s, no
exception. The converted raster (tunnel read of `base + 0x780`):

| Input pixel (x, row) | Raster byte | Value | Tile j, halfword r, bit |
|---|---|---|---|
| (0, 0) | `+0x0000` | `0x01` | j 0, r 0, bit 0 |
| (9, 1) | `+0x0003` | `0x02` | j 0, r 1, bit 9 |
| (18, 2) | `+0x0504` | `0x04` | j 1, r 2, bit 2 |
| (27, 3) | `+0x0507` | `0x08` | j 1, r 3, bit 11 |
| (36, 4) | `+0x0a08` | `0x10` | j 2, r 4, bit 4 |
| (45, 5) | `+0x0a0b` | `0x20` | j 2, r 5, bit 13 |
| (54, 6) | `+0x0f0c` | `0x40` | j 3, r 6, bit 6 |
| (63, 7) | `+0x0f0f` | `0x80` | j 3, r 7, bit 15 |

So, measured: byte address `B + j·0x500 + 16·g + 2·r`, little-endian halfword,
bit b = column `16·j + b` (bit 0 is the leftmost column of the tile), for
8-row group g (g = 0 here, slot 0). The open firmware can fill the raster
storage directly in this format; the converter is not needed for a test
pattern. Still unmeasured: which physical nozzle each of the 80 groups × 8
rows drives (the pattern sheet).

## Executor on hardware, carriage frame, raster upload (2026-10-26 afternoon)

Routine 6 `band-run` (brief 11 Q repair, lane prep `0xe6aea`, executor
`0xe6b80`) returned 1 on the board once the carriage adapter cleared u16
`0x180d5784` bit 12 on completion (the executor's `0xe7074` returns −32 while
`0xd305c` sees that bit). The executor's peripheral programming, captured in
the emulator with the write census
([executor_mmio_writes_emulated.txt](executor_mmio_writes_emulated.txt), 574
writes): raster worker/RB2PB (`0xc0c21xxx`, `0xc0c25xxx`), lane DMA image
(`0xc0c27000..` B/stride/count per lane, masks `0xfffeffff`), raster feed
(`0xc0c28xxx`, start word `0xc0c28000 = 1` last), head engine control and
the three waveform banks (`0xc0c29xxx`, the same values our `head_fire.c`
writes), CR compare windows (`0xc0c51700..a24`, `0xc0c51d34/38`,
`0xc0c51108..114`, event mask `0xc0c51400 = 0x1e`), the shared head GPIO take
and cold preparation (`0xde2b2`, `0xe1782`, as `cartridge.c`), then the CR
request. Scheduler block A on the board after the run: bounds
`0xc4aa/0xd924`, approach `0xc066`, final-approach start `0xa80` (= the
capped position), target `0xdda8`, direction 0, profile `0x103`, common
offset `0x4144`.

The windows are compared against the raw CR counter, so live mode writes
the counter into stock's frame first (`carriage_set_frame`: raw = 2688 at
our park wall, x unchanged). Motion-only live run on the board: stock's
56744 → our 54056, ended at x = 53952 (raw `0xdd40`), 2.45 s, firing
disarmed. The band raster is uploaded over bulk OUT (`band_pattern.py`,
labelled 320 × 640 test pattern in the measured tile layout; readback
identical) and routine 7 runs the executor on it. Not yet on paper: nozzle
row order, column order and horizontal placement (the pattern carries
orientation marks); whether the engine fires from the windows alone.
