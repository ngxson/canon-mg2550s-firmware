# Cartridge fuse-ROM read: first physical attempt (2026-10-24)

Module `src/cartridge.c`, host tool `tools/cartridge.py`, emulator model
`tests/model_cartridge.py`, test `tests/validate_cartridge.py`.

## What the firmware does

Read-only sampling of the cartridge identification fuses, one bit per trigger:
enable `0xc0c2954c`, selector `0xc0c29540`, pair word `0xc0c29550` (always 0),
trigger `0xc0c29544`, sense input `0xc0c84618` bit 0 (active low, complemented
after packing). Black reads bits 0..31, colour bits 0..31 and 32..47. Each low
word is read twice to show stability. The fuse-cut GPIO `0xc0c84624` and the
longer programming pulse are never used; the emulator model asserts this.

Not implemented: the stock head power/mode acquire ("mode 4") that precedes the
read, including its timer and GPIO routing.

## Emulation

`validate_cartridge.py`: five cases pass (both heads with synthetic fuse words
and exact selector sequence, colour-only with 2 ms wait, sense stuck high,
rejected requests stall, reboot into rescue after a read). Synthetic inputs
only; no electrical model. Report: `app/build/validation_cartridge.json`.

## Physical result

App `93f72d02…` uploaded with `fwctl flash` (normal protected update; both
bootloader stages untouched). Printer powered normally, both cartridges
installed, scanner idle. Three runs with 100 µs, 1 ms and 2 ms per sample:

| Head | low | repeat | high | idle sense bit 0 |
|---|---|---|---|---|
| black | `ffffffff` | `ffffffff` | `0000` | 0 |
| colour | `ffffffff` | `ffffffff` | `ffff` | 0 |

Every sample read 0 on the sense input, before and during the sequence, for
all 48 colour and 32 black selectors and all three timings. The decoded fields
(id 15, dest 63, inklvl 127 for both) are therefore the all-ones artefact of a
stuck line, **not cartridge data**. Reports: `cartridge/read_100us.json`,
`cartridge/read_default_1ms.json`, `cartridge/read_2ms.json`.

Management stayed responsive throughout; the app reported status `80` after
each run.

## Interpretation

The selector/trigger sequence alone does not produce a readable sense level on
this board. The missing prerequisite is the stock head power/mode acquire path
(the memo lists it as `0xddfb6(4)` → `0xe19ae` with timer 3, RTOS event
`0x1b`, head state and GPIO routing) and possibly the pin-mux ownership of the
`0xc0c84618` input. Until that sequence is available as a documented contract,
no ink-level or ID value can be reported.

What this result does establish:

- The write/trigger registers are accessible and the sequence completes in
  bounded time without disturbing USB management.
- Timing between 100 µs and 2 ms per sample makes no difference.
- The firmware never entered the programming path.

## Second attempt with the stock head-interface power sequence (2026-10-24, later)

The traced mode-4 contract (`fw_dump/gpt/BRIEF_mode4_head_power.md`, response;
`fw_dump/PROGRESS.md` §1k) was implemented in `src/cartridge.c`: connection
test (`0xde4f8`), cold preparation (`0xe1764`: bank controls, `0xc0c84664=1`,
`0xc0c84654=1`, 600 ms settle for stock's 5 × 100 ms), acquire (`0xe19ae`:
selector clear, `812c4|=1`, `812c0&=~1`, `0xc0c84604=1`, 4 ms), read with the
stock-nominal 300 µs per sample, release (`0xc0c84604=0`, 6 ms) and shutdown
(`0xe1860`). Stock's timer-3/IRQ-3/RTOS waits are replaced by bounded software
delays that keep USB management polled; the RTOS delay unit is assumed to be
milliseconds and doubled. Emulation: six cases pass (`validate_cartridge.py`),
including control ordering and that no fuse-cut write occurs.

Physical runs, app `9898b691…` then `4fff10a6…` (diagnostics build), both
cartridges installed, carriage parked:

| Run | Connection test | Fuse sense |
|---|---|---|
| `read_powered_300us.json` | black ok, colour ok | stuck low, both heads |
| `read_powered_after_shared_init.json` (scanner shared setup first) | ok, ok | stuck low |
| `read_powered_diag.json` | ok, ok | stuck low; see below |

Diagnostics (`--diag`):

- Head block accepts writes: `0xc0c2954c` reads back 1, `0xc0c29540` reads
  back `0x641`; `0xc0c29544` reads 0 after the trigger (self-clearing or
  write-only).
- Sense `0xc0c84618` bit 0 stayed 0 in 20 000 consecutive reads (48 ms)
  after triggers for selectors `0x641`, `0x6b1`, `0x541`, `0x441`, `0x241`:
  no pulse of 2 µs or longer and no level change. One MMIO read costs about
  2.4 µs on this bus.
- The bank/alias dump line was lost to debug-log overflow; rerun with a quiet
  host if needed.

Interpretation: the GPIO path through the head connector works (connection
test), the head block is clocked and programmable, and every traced register
of the stock read path is exercised, yet the cartridge never drives the sense
line. The remaining candidate is a supply or enable set outside the traced
functions (head logic/VH rail, power-on sequence). Follow-up brief:
`fw_dump/gpt/BRIEF_head_supply.md`.

## Third attempt: stock head-block configuration words (2026-10-24, later)

The second RE answer (`fw_dump/gpt/BRIEF_head_supply.md`, response) found no
supply enable but identified a stock head-block initialization (`0xc3096`)
that runs long before any fuse read. Options `0x800` (`--headcfg`) and
`0x1000` (`--headcfg2`) write its fuse/transaction-group subset before the
power path; the heater-waveform-looking `0xc0c290xx/291xx` words were not
written.

| Register | Reset value read on the board | Stock value written |
|---|---|---|
| `0xc0c29588` | 0 | 0 |
| `0xc0c29590` | `0x00000007` | `0x14040007` |
| `0xc0c29598` | `0x00000700` | `0x00000700` |
| `0xc0c295a0` | `0x00070000` | `0x1f07000b` |
| `0xc0c295a8` | `0x07000000` | `0x07010002` |
| `0xc0c29580` / `84` / `f0` | 0 / 0 / 0 | 1 / 0 / 0 (option `0x1000`) |

All writes read back. Runs `read_headcfg_diag.json` and `read_headcfg2.json`:
connection test ok on both heads, **fuse sense still stuck low**, no pulse in
48 ms of continuous sampling. App `ae81a268…` installed.

Everything the two traces name as part of the read path, the head-interface
preparation and the fuse-group configuration is now exercised on hardware
with no response from either cartridge. Remaining candidates, none supported
by recovered code: the rest of the `0xc3096` initialization (`0xc0c290xx/291xx`
words, profile-dependent `295b0/295d0..dc`), the unresolved panel/auto-on
power-management chain (platform handlers `0x1166bc`/`0x116868`), or a supply
or enable that stock reaches through a computed address.

## Success: full stock head-block initialization (2026-10-24, later)

Option `0x2000` (`--headcfg3`) adds the remaining fixed words of stock's head
initialization `0xc3096` (`0xc0c29d4c`, `291d0`, `29050`, `29060`, `29070`,
`290d0`, `290b0`, `290f0`, `290f4`, `29130`, `29138`, `2913c`, `29150`,
`29154`, `29158`, `29160`, `29168`, `29174`, `29184`; values from the RE answer
in `fw_dump/gpt/BRIEF_head_supply.md`, Q3). Reset values read on the board were
zero except `29130/38/3c = 0x00070707`, `29150 = 0xb`, `29174 = 0x0012180b`;
`291d0` reads back `0x3fc` for a written `0xfffffffc` (10-bit field).

With `--headcfg --headcfg2 --headcfg3` (app `85258584…`, run
`read_headcfg3.json`) the fuse ROM reads **real, stable data**:

| Head | low | repeat | high | id | dest | rank1 | rank2 | inklvl | idle sense |
|---|---|---|---|---|---|---|---|---|---|
| black | `64f12b7f` | same | `0000` | 6 | 19 | 37 | - | 127 (`1111111`) | 0 |
| colour | `a737853f` | same | `03ef` | 10 | 19 | 16 | 7 | 63 (`0111111`) | 1 |

Both heads differ, both repeat reads match, the shared `dest` code is equal on
both cartridges, and the level fields are the thermometer patterns the stock
programming path uses (`1,3,7,...,127` for level index 0..6): black is at the
highest index (all seven level fuses cut), colour one below. That is consistent
with the user's expectation that both cartridges are low on ink. Field meanings
beyond the stock names remain inferred; `INKLVL` is a fuse count, not a volume.

What made the difference: the head block's drive/timing configuration words.
Without them the fuse transaction produced no sense response although every
GPIO, power-control and fuse-group register was set. The head interface power
sequence (connection test, cold preparation, acquire) is still required; the
earlier raw attempts without it also failed. These ASIC registers keep their
values until power-off, so later reads succeed without the option until the
next power cycle. Reproducibility and cold-start behaviour: see the following
entries.

Follow-up: `read_success_repeat.json` (same app) and `read_default_after_fix.json`
(app `54e49a13…`, head-block init now default) returned identical words. The
`--no-headinit` option keeps the no-response state reproducible on a cold
board. Cold-start confirmation after a power cycle is still to be recorded.

## Head thermistor ADC (2026-10-24, later)

USB `0x80` (`tools/head_temp.py`), implemented from the stock sampler described
in memo section 1i and brief 4 Q3: byte writes `0xc0c90000 = channel`,
`0xc0c90004 = 2`, `0xc0c90000 = channel | 0x80`, 16 halfword samples with the
stock delay argument 2, 16-bit truncated sum, mean = sum >> 4. Black = channel
0x12 at `0xc0c90030`, colour = 0x11 at `0xc0c90028`.

Without the stock platform step `0xc0c90010 = 0x80` (byte) followed by
polling bit 6 of the same register (brief 3 answer, `0x116c5c`), every sample
read 0, with the head interface off or prepared (`head_adc_idle.json`,
`head_adc_prepared.json`). With that init the block answers
(`head_adc_init.json`): black 732, colour 732, samples 731..734, at room
temperature. Raw counts; stock's conversion to half-degrees uses fuse-derived
calibration (`0x184130/0x184170` LUTs, keys `01010570..573`) not reproduced.
Both channels reading the same value is consistent with equal temperature but
not yet proven distinct; warm one cartridge to check.
