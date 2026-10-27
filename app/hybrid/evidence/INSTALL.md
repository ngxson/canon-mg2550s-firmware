# Hybrid package installed on the board (2026-10-26)

Package `app/hybrid/build/application.bin`, SHA-256
`6e5e43f7256a41dff91c56ab21b62a255567dc184fb6baa0e552ee65a5ba4010`
(2,031,616 bytes: stock image with the open app at flash `0x199000..0x1b0660`,
entry branch at `0x10020`, delay helpers relocated to `0x10d00`).

## Emulator (before flashing)

- `app/hybrid/validate_hybrid.py`: bootloader validates and copies the
  payload, the app runs at RAM `0x201a8fe0`, answers INFO as the application,
  USB `0x95` calls `0xe8610` and `0xc763e` in place and their RAM effects match
  brief 16 §2; locked, out-of-image and ARM-bit addresses are refused
  ([validation_hybrid.json](validation_hybrid.json)).
- All fifteen standalone suites pass on the rebuilt app (link address now a
  build parameter).

## Hardware

- The board ran the stock telemetry package. Power-cycled through the
  webhook; the bootloader window was caught with 4.7 s left, rescue pinned,
  upload verified (`C0`), app started ([install_log.txt](install_log.txt)).
  The first `wait_device('application', 60 s)` timed out; the app was
  enumerated as `MG2550s App OPEN0001` by the next query. The 1.9 MiB CRC
  passes (bootloader and app) make the start slower than the standalone app.
- Bootloader untouched: normal update path, first 64 KiB protected.
- `app/tools/hybrid_call.py check`:

```text
before: repeat ['0x69214788'] gates ['0xf7ff0030'] thresholds [2648353, 4226543648, 3490064384, 1770612737]
0xe8610: {'address': '0xe8611', 'r0': '0x0', 'calls': 1, 'refused': 0, 'unlocked': 1}
0xc763e: {'address': '0xc763f', 'r0': '0x18079a98', 'calls': 2, 'refused': 0, 'unlocked': 1}
after:  repeat ['0x1'] gates ['0xf7010101'] thresholds [20160, 43200, 64800, 129600]
PASS stock initializers ran in place from flash
```

The "before" values are whatever the uninitialized stock RAM held: stock's
own boot never ran. After the two calls the bytes hold exactly the values
brief 16 attributes to those initializers. This proves that stock code
executes in place from flash under the open app, nothing more: no motor,
head or status-server path was exercised on hardware.

## Shim on hardware (2026-10-26, later)

Package `50289e2c…b323` (app 102,240 bytes) adds `src/stock_shim.c`: the
stock kernel entry points are replaced by veneers into the shim, stock runs in
System mode from a register-preserving trampoline, the stock SVC handler and
recording exception handlers are registered through the bootloader vector
ABI, stock RAM is initialised from the scatter table (minus our pages), and
the NVM manager singletons get a shim vtable.

`app/tools/hybrid_call.py run cap` (dry motion) on the board:

```text
result=0x1 errors=0 trace=89 flags=0x4 cr_position=81280 exceptions=0 (3.5s)
CR_MOVE 2688/3, CR_MOVE 2688/3, CR_PROFILE 0x2003, CR_MOVE 16640/3, NVM …,
CR_MOVE 2688/3, CR_PROFILE 0x2005, CR_MOVE 81280/3, CR_PROFILE 0x2002,
CR_RELEASE, DELAY 2, STATUS_PUB f004/0x200, NOTIFY 0x1005, …
```

Identical to the emulator trace. The stock cap sequence (wipe-before-cap,
cap, release) executed in place on hardware with our services; no motor
moved (dry adapters). An earlier build without the exception handlers hung
the board within 0.5 s of the same request; the recording handlers saw no
exception in this run, so that hang is not explained, only gone. Stages 1
to 3 (RAM init 3.3 s, SVC registration, presets) had passed individually.

## Package a8bcc6e3 (2026-10-26, late): ARM veneers, band-prepare

The kernel privilege/cache helper bodies (`0x150084/94/a4/b4`, `0x1500d8`)
are ARM-veneered no-ops, the system-services singleton gets shim interface
objects, counting-semaphore polls return -50, and routine 5 `band-prepare`
exists. `hybrid_call.py run cap` on the board: result 1, 89 steps, no
exception, 3.5 s (unchanged). `band-prepare` is expected to abort at a NULL
stock pool object (see `emu/evidence/stockapp/band/README.md`).

`hybrid_call.py run band-prepare` on the board: **USB stopped answering after
5.3 s and the board needed a power cycle.** The recording exception handlers
did not report anything, so the failure is a stock loop or a bus hang, not a
CPU exception. Do not run routine 5 on hardware until the stock object graph
is initialised (see the band evidence); cap remains the only verified routine.

## Package 1bbf79e6 (2026-10-26 morning): RAM snapshot, re-entrant trampoline

Stock RAM pages the fixtures touch are restored from a boot-state snapshot
in the flash hole (`emu/stockapp/ram_snapshot.py`, `SNAPSHOT_FLASH 0x1b5000`),
replacing the hand-made singleton stand-ins; `stock_call` is re-entrant
(the shim runs stock completion handlers from inside stock's waits).
Board: `run cap` result 1 (35 steps now, with real status banks), `run
band-prepare` result 1 in 0.3 s with the RB2PB engine converting the test
packet (see the band evidence). Still nothing moved or fired.

## Packages e4670d3e, 21ee9032, d5683129, ead02490 (2026-10-26 afternoon): band executor live

- `e4670d3e`: the carriage adapter clears the stock "request armed" bit
  (u16 `0x180d5784` bit 12, checked by the executor's readiness step
  `0xd305c`) on completion. `run band-prepare` then `run band-run` on the
  board: executor `0xe6b80` returns 1 in 1.1 s, no exception, CR request
  target 56744 profile `0x103` (dry). The same run in the emulator wrote the
  complete head-engine register set (`emu/evidence/stockapp/band/executor_mmio_writes_emulated.txt`).
- `21ee9032`: live mode presets the raw CR counter into stock's frame
  (`carriage_set_frame(2688)`: capped carriage = 2688 stock counts, our
  park wall x = 0) and maps stock targets through the same offset; a
  motion-only option (`0x200`) clears the raster-engine start word and the
  CR event mask and skips the owner acquire. Board, `run band-run --live
  --motion-only`: frame preset (raw `0xa80` at the wall), stock request
  56744 became our move to 54056, carriage ended at x = 53952 (raw `0xdd40`)
  in 2.45 s, nothing fired, no exception.
- `d5683129`: band raster upload (USB `0x99` + bulk OUT into manager 6's
  storage) and routine 7 `band-fire` (executor only). Board: 25,600 bytes in
  0.07 s, tunnel readback identical; the executor ran on the uploaded raster.
- `ead02490`: `0x400` "fresh" flag re-initialises stock RAM from the snapshot
  before a routine. A second executor run without it keeps direction 0 but
  places the band 16,708 counts further left (the cached common offset
  `0x180cda80` drops from `0x4144` to 0); a fresh prepare restores the
  emulator-verified placement. Full rehearsal of `app/tools/band_live.py
  --motion-only --no-feed` on the board: home, fresh prepare (3.5 s),
  upload, executor live (sweep to 53952), home. Ready for one sheet with the
  owner present.
