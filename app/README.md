# MG2550s replacement application

A source-built C application for the Canon PIXMA MG2550s, with scanner control,
USB management and a full-page scan proof of concept. It occupies the main app
slot at flash `0x10000` and runs at RAM `0x20020000`. Normal app updates preserve
the first 64 KiB containing both [bootloader](../bootloader/README.md) stages.
No stock executable code is linked into the application.

## Current capabilities

- **Full-page grayscale scan:** home, advance to measured positions, stop and
  capture each row, transfer it over USB, then return home. A physical scan
  completed all 880 rows of 2,592 16-bit samples in 329.5 seconds. The saved
  image contains recognizable document content; it remains uncalibrated.
- **Mechanical homing and forward motion:** a left seek, verified forward
  backoff and repeated left seek establish the mechanical reference. Motor
  setup, acceleration and speed control are implemented in C.
- **Panel controls and LEDs:** Colour homes, B/W requests a short forward jog,
  and Cancel stops motion. Held keys do not repeat; keys held at startup are
  ignored. There is no automatic motion at boot.
- **Cartridge identification:** reads both cartridges' fuse ROM (ID,
  destination code, rank values, level field) through the stock head-interface
  sequence; USB requests 0x62/0x63 and `app/tools/cartridge.py`.
- **Paper path and carriage:** closed-loop feed routine (pickup with bounded
  retries using the paper-end sensor, measured advance, eject), carriage
  homing and closed-loop moves, and a dry-run page loop that sweeps the
  carriage band by band over a fed sheet; see [CARRIAGE.md](evidence/CARRIAGE.md)
  and [PRINT_MECH.md](evidence/PRINT_MECH.md).
- **Stationary black prefire (spit):** the stock prefire work list (two
  records of 100 pulses on the black lanes, waveform mode 8) translated
  register for register into C, with the head thermistor read before and
  after. Confirmed on the printer: the engine completes, heats the black head
  and put the first ink marks on paper (2026-10-25). Printing a raster (band
  path, nozzle map) is not implemented. `tools/mark_scan.py` scans a sheet
  back and measures the marks.
- **Recovery management and diagnostics:** application USB identity `04a9:12fe`,
  `fwctl` status/reboot support and a USB debug log. USB is serviced during
  motion; I²C management is polled when the motor is idle. Stationary captures
  occupy about 110 ms before management polling resumes.

Physical measurements and emulator coverage are recorded in
[page-scan evidence](evidence/PAGE_SCAN.md) and [homing evidence](evidence/HOMING.md).
Emulation uses scripted feedback and synthetic sensor data; physical tests
establish actual motion and image capture.
The [emulator guide](../emu/README.md) also covers stock startup, scheduler and
homing experiments, alongside a single command for the application test suites.

## Build and validate

Run commands from the repository root (`canon-mg2550s-firmware`). You need
Python 3, an Arm bare-metal GCC toolchain (`arm-none-eabi-gcc` and binutils), and
Docker for the tested Linux/Unicorn emulator path. The recorded builds use
Arm GNU Toolchain 14.2.Rel1; the bootloader's pinned first-stage hash is
compiler-sensitive. `ARM_PREFIX` can select another toolchain location, for
example `/path/to/bin/arm-none-eabi-`.

Prepare bootloader artifacts once for the emulator, then build the application:

```sh
python3 bootloader/build.py
python3 app/build.py
docker build -t mg2550s-emu bootloader/tests
```

These are local builds. The app package is `app/build/application.bin`; the
bootloader build also produces a separate LED demo package. For routine app
work, keep the bootloader artifacts matching the installed recovery stages.
Tests combine `bootloader/build/boot_region.bin` and its symbol manifest with
the new app package and manifest.

Run all sixteen scanner, cartridge, mechanism, head, tunnel, bench, power and management checks before flashing:

```sh
for check in app home motor buttons scanner page cartridge printregs feed carriage page_job head_fire stamp tunnel bench power; do
  docker run --rm --network none -v "$PWD:/project:rw" \
    mg2550s-emu "app/tests/validate_${check}.py" || break
done
```

Proceed only after all sixteen pass. Reports are written under `app/build/`.
Tests execute the linked ARM instructions, including boot and recovery, and
check motion sequencing, capture/transfer failures, Cancel and management
liveness. No stock dump is needed for these tests. Native execution is also
possible with [the emulator requirements](../bootloader/tests/requirements.txt),
but the Linux container avoids the native macOS Unicorn crash encountered
during development.

## Install over USB

This assumes the source-built layout-2 bootloader is already installed. For
initial installation or migration, follow the [bootloader guide](../bootloader/README.md).
Set up host dependencies in a virtual environment; PyUSB also needs a system
libusb backend (for example, Homebrew's `libusb` on macOS).

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r tools/requirements.txt numpy Pillow

python3 tools/fwctl.py status
python3 tools/fwctl.py flash app/build/application.bin
```

The tool requests rescue mode, uploads and verifies the app, then starts it.
A normal update preserves both recovery stages. USB/I²C reboot support remains
in the application; the bootloader also provides a five-second rescue window
after reset. See the [host-tool guide](../tools/README.md) for recovery commands
and I²C bridge setup.

## Scan a page

Place the document face down, align it with the glass corner mark and close
the lid. With the application running:

```sh
python3 app/tools/scan_page.py --out scans/my-page
# Cancel from another terminal, or press the printer's Cancel key:
python3 app/tools/scan_page.py --stop
```

The default is 880 rows at 512 encoder counts per row: nominally 75 rows/inch
and about 298 mm between first and last capture. A pass takes roughly 5½ minutes
on the tested board. `--rows 32` captures a short strip; `--pitch` changes row
spacing. Use a new output prefix for each scan. The `scans/` directory is ignored
by Git.

| Output | Contents |
|---|---|
| `.bin` | Original little-endian 16-bit samples, in row order |
| `.pgm` | The same samples in 16-bit PGM format |
| `-raw.png` | Contrast-stretched preview at native sample dimensions |
| `.png` | Preview resized to an assumed 216 mm sensor span (638×880 by default) |
| `.json` | Row positions, state changes, completion and error information |
| `.log` | Firmware debug log from the pass |

The motor is off during capture and row transfer. Each row stays unchanged
until the host has read and acknowledged it. A 15-second row-transfer timeout
stops the pass with motor and lamp off; partial output is marked incomplete.
Individual Copy keys are ignored during a scan. Cancel or both Copy keys abort
it. A successful pass returns home before reporting completion.

## Read the cartridge fuse ROM

```sh
python3 app/tools/cartridge.py --output /tmp/cartridge.json
```

Read-only: it configures the head block as stock does at start-up, runs the
stock head-interface preparation (connection test, cold preparation, acquire),
samples the cartridge identification fuses (stock names `PK*ID`, `PK*DEST`,
`PK*RANK*`, `PK*INKLVL`), then releases and shuts the interface down. It never
touches the fuse-cut path. `INKLVL` is a seven-bit thermometer code of
programmed level fuses, not a measured volume. Physical results and the
register contract are in [CARTRIDGE.md](evidence/CARTRIDGE.md).

## Feed a sheet

```sh
python3 app/tools/paper_feed.py feed     # pick up, advance, eject
python3 app/tools/paper_feed.py hold     # pick up and stop past the sensor
python3 app/tools/paper_feed.py eject    # forward until the sensor clears, plus margin
```

Physically drives the paper-feed motor. Pickup retries three times and reports
`no_paper` if the paper-end sensor never sees a sheet; Cancel or USB stop abort.
Register contract and measurements are in [PRINT_MECH.md](evidence/PRINT_MECH.md).
The Copy keys are currently bound to bench pulses of the print carriage.

## Move the carriage and run a dry page

```sh
python3 app/tools/carriage.py home
python3 app/tools/carriage.py move 50000        # counts from the park wall, left positive
python3 app/tools/dry_page.py --bands 4         # feed, four sweeps with paper advances, eject
```

Both physically move the mechanism; the head stays idle.

## Stationary prefire (spit) on the black head

```sh
python3 app/tools/head_spit.py --position 6000 --dry-run     # every register write, no arm
python3 app/tools/head_spit.py --position 6000 --ambient 44  # stock prefire: 2 x 100 pulses, black lanes
```

Physically fires the black head unless `--dry-run` is given. The tool reads the
cartridge fuse ROM first (the firmware refuses unless the ranks are black 37 and
colour 16/7, the ones its waveform tables were computed for), reads the head
thermistor, homes and moves the carriage to `--position` counts from the park
wall, runs the stock prefire list, drains the log and parks. `--ambient` is the
stock temperature selector input in half-degrees C (default 50). `--count`
reduces the pulses per record (15..100); `--record 1` or `--record 2` runs one
of the two stock records alone, to attribute marks on paper to a record.
`--colour` runs the stock colour prefire list instead (work ID 0x25: lanes
2..7 in two records of 600 pulses, waveform mode 9).

Experiment: `mask_map.py run` prints one sheet with 32 carriage stops per
row and one record per stop whose two record words (USB 0x83/0x84, fired
with 0x81 option 0x2000) isolate one bit, to map the words to nozzle groups
from a single scan-back (`mask_map.py decode`). Carriage moves below 6000
counts use a short-move mode (kick until motion, fixed rolling effort, stop
on one counter step); the scripted carriage model has friction and momentum
so that mode is exercised in `validate_carriage.py`.

Text by stamping (not yet run on the printer): `stamp_print.py "HELLO"`
plans one entry per pixel column (5x7 font, `tools/stamp_plan.py`), uploads
them (USB 0x83/0x84 words, 0x85 commit) and runs the firmware's stamp job
(0x87): the carriage crawls across the text and one black record fires as
each entry's encoder position is crossed; 0x88 reports fired and missed
entries. `tests/validate_stamp.py` renders the fires onto a virtual page and
checks the text; the nozzle map it assumes (eight nozzles per record-word
bit, cleared bit fires) is the hypothesis the mask-mapping sheet will test. A `timeout` result means the engine
never reported completion; the firmware then runs the stock cleanup, disarms
and releases. The register contract and its limits are documented in
`src/head_fire.c`.

## Scan a printed sheet back and locate the marks

```sh
python3 app/tools/mark_scan.py --out scans/marks1                 # 75 dpi grey scan via eSCL, then detect
python3 app/tools/mark_scan.py --out scans/marks2 --reference scans/marks1.json   # offsets versus an earlier scan
```

Print-then-scan loop for head debugging, in the spirit of the stock head
alignment: put the ejected sheet face down against the glass corner mark and
run the tool. It reports each ink mark's centre, size and bounding box in
millimetres from the glass corner, writes an annotated PNG and a JSON report,
and with `--reference` the shift of each mark versus a previous scan.

## Bench tests on the panel buttons

```sh
python3 app/tools/bench.py map 1     # feed bench
python3 app/tools/bench.py log       # afterwards: what each press did, with counter and sensor values
```

Tests that need someone at the printer are driven from the panel instead of
from timed host sequences (USB 0x8b selects the map). Feed bench, map 1:
Copy B/W picks up a sheet and places it (hold, then 8000 counts); Copy
Colour advances it 1024 counts per press; Power moves the carriage between
park and 60000 counts; Cancel ejects a held sheet, otherwise stops. Map 0
restores the default keys (feeder and transport toggles, Cancel stops).

## Print from the host over IPP (on-device page printer)

On a hybrid board the application accepts IPP Print-Job (and Create-Job +
Send-Document) over IPP-USB and prints the document itself: `src/ippusb.c`
buffers the IPP attributes and streams the document bytes with backpressure,
`src/ipp.c` keeps one job (id, state, Get-Jobs / Get-Job-Attributes /
Cancel-Job), `src/urf.c` decodes Apple URF (W8 grey or SRGB24, 300 dpi) and
`src/print_engine.c` turns rows into 600 dpi K/C/M/Y bit rows (grey
component replacement, 8x8 ordered dither, the measured 1.024 width
compensation applied before halftoning) and drives the stock band path
through the shim: home, head prepare,
stock load, first-line advance, per band prepare / tile raster into the stock
managers / executor / advance, eject. Colour pages print in stock mode 7 with
152-row bands (black leads the colour arrays by 128 rows), grey pages in mode
1 with 304-row bands. Each segment now uses two disjoint row passes, 16 rows
apart, to avoid the repeating weak nozzle rows measured on Q272125. The
remaining feed is shortened by the same amount, preserving page scale.
Q272212 physically verifies removal of the recurring K/C/Y white strips;
this workaround costs a second sweep per segment. Raster conversion pauses
while stock routines run so it cannot delay their polled motor controllers.
See [the print-quality evidence](evidence/print_quality/README.md) for physical
results and remaining limits. Boards without the hybrid band path still answer
`server-error-not-accepting-jobs`, draining the document instead of 413.

```sh
python3 tools/ipp_print.py photo.png            # URF Print-Job over IPP-USB, follows the job to completion
python3 tools/ipp_print.py text.png --grey      # W8 grey page: mode-1 black bands
python3 tools/ipp_print.py --status             # USB 0xa2 engine status; --cancel = USB 0xa3 op 2
```

macOS/Linux hosts print through their own IPP-USB daemon to the same path
once they list the printer (`printer-is-accepting-jobs` is true on hybrid
boards). Emulator: `HYBRID_IPP_PRINT=1 app/hybrid/validate_hybrid.py` runs a
Print-Job end to end in the engine's dry mode (USB 0xa3 op 1: no homing, head
or sheet checks, dry executor) and asserts two colour bands in eight sweeps
with no allocation inside a live band. Add `HYBRID_IPP_QUALITY=1` to check
every emitted paper dot, both segments, the band seam and trailing blanks;
add `HYBRID_IPP_MONO=1` for the four-sweep mono case. These tests also forbid
row conversion during stock routines. `tests/test_urf.c` is the native decoder test.
The shim keeps a prepared band's allocations live until the next prepare
(`band_mark`), the fix for the per-pass pattern-buffer overwrite found by the
brief 22 helper; the validator asserts it on every executed band.

## Power manager

`src/power.c` keeps USB and management alive while unused hardware rests
([brief](../../fw_dump/gpt/BREIF_POWER_MANAGEMENT.md)). States: ready,
active (a hardware owner works), quiescing, standby, waking, fault. Automatic
standby fires after an idle timeout (60 s development default, RAM only;
`fwctl.py power timeout N`, 0 = never) measured from the last moment any owner
was busy: scanner motor/page/lamp/armed capture, paper feed, carriage, page
job, stamp, hybrid routine, cartridge read, head fire, LF jog, print or raster
reception. Status polling, log reads and rejected requests never restart the
timer. Standby runs a bounded, reversible quiesce inside one main-loop poll:
illumination/SCNIO stop and scanner analog power off (unread scan rows are
kept), scanner motor outputs neutral (no reboot inhibit; the next move
re-initialises), panel LEDs dark (keys keep scanning). Readbacks that
disagree put the manager in `fault` with the stage and error; nothing is
reported as reached that was not. Any admitted job wakes automatically
(waking → active) and relights the panel; nothing is energised merely by a
wake, a status read or USB enumeration. The Power key requests standby when
idle and wakes when asleep in button maps 0 and 2 (map 1 keeps its bench
role). Explicit standby while an owner works is refused as busy and cancels
nothing. Not done yet, and reported as unsupported: print-side parking and
head capping, driver-IC sleep, supply-rail switching, CPU sleep/clock changes.

```sh
python3 tools/fwctl.py power status       # USB 0xa0: state, owners, idle age, readbacks, capabilities
python3 tools/fwctl.py power standby      # USB 0xa1 op 1; busy -> accepted: false
python3 tools/fwctl.py power wake         # op 2
python3 tools/fwctl.py power timeout 300  # op 3, seconds (0 = never, max 3600)
```

`tests/validate_power.py` covers the timer with continuous polling, busy
refusal, idempotent standby/wake, automatic wake, the Power key, parameter
validation and rebase, timer disable, the millisecond-clock wrap, an injected
readback failure and INFO/reboot while asleep. Physical qualification
(idle readbacks, repeated standby → eSCL scan → standby, wall power) is still
pending; see the brief's qualification list.

## Register tunnel for hardware-in-the-loop emulation

```sh
python3 tools/tunnel_proxy.py              # reads only; add --unlock to allow forwarded writes
docker run --rm -v "$PWD:/project:rw" mg2550s-emu emu/run.py boot --eeprom emu/inputs/eeprom.bin \
    --tunnel host.docker.internal:7731 --forward c0c80000-c0c88000,c0c8d000-c0c8e000
```

The stock firmware runs in the emulator while accesses in the forwarded ranges
are performed on the printer through USB requests 0x90..0x94 (`src/tunnel.c`).
The firmware allowlists the head blocks, the motor blocks (read only), the
GPIO/alias banks, the panel (read only) and the ADC; the interrupt controller,
clocks, flash, USB core and RAM are never reachable, and writes need the unlock
key the proxy sends only with `--unlock`. Interrupts stay synthetic in the
emulator because this firmware masks them. See the [emulator guide](../emu/README.md).

## Home and inspect logs

```sh
python3 app/tools/home.py --output /tmp/home.json
python3 app/tools/home.py --status-only --output /tmp/home-status.json
python3 app/tools/fwlog.py
```

The first command moves the scanner; `--status-only` reads status without
motion. `fwlog.py` drains a bounded RAM log buffer and reconnects after reboots.
Use it separately from the page tool, which collects the same log itself.
Protocol and controller details are in [HOMING.md](evidence/HOMING.md) and
[PAGE_SCAN.md](evidence/PAGE_SCAN.md).

## Source layout

| Path | Purpose |
|---|---|
| `build.py`, `app.ld` | App-only build, RAM layout and MGAP packaging |
| `src/app_main.c`, `src/service.c`, `src/usb.c` | Main loop, recovery management and USB requests |
| `src/buttons.c`, `src/debug_log.c` | Panel actions and diagnostic log |
| `src/scanner_motor.c`, `src/motor_control.c` | Motor setup, bounded moves, homing and controller arithmetic |
| `src/scanner.c`, `src/scnio_seq.h` | Sensor timing, illumination, DMA and line capture |
| `src/scan_page.c` | Page sequencing and row ownership |
| `src/print.c` | Experimental print-data decoder; physical printing is unfinished |
| `src/cartridge.c` | Cartridge fuse-ROM reader with the stock head-interface sequence (USB 0x62/0x63), shared head-interface steps and thermistor ADC; see [evidence](evidence/CARTRIDGE.md) |
| `src/head_fire.c` | Stock prefire lists (black 0x24, colour 0x25; USB 0x81/0x82), custom record words and the crawling stamp job (0x83..0x88); first ink on paper 2026-10-25, see [HEAD_FIRE.md](evidence/HEAD_FIRE.md) |
| `src/print_mech.c`, `src/paper_feed.c` | CR/LF register dump, bounded motor pulses, GPIO snapshot, closed-loop paper feed; see [evidence](evidence/PRINT_MECH.md) |
| `src/tunnel.c` | Allowlisted register peek/poke for hardware-in-the-loop emulation (USB 0x90..0x94) |
| `src/carriage.c`, `src/page_job.c` | Carriage homing and closed-loop moves; dry-run page loop (feed, sweeps, advances, eject) |
| `PRINT_TEST_PLAN.md` | Staged physical test plan for the CR/LF print motors and head controller |
| `tests/`, `tools/`, `evidence/` | Emulator checks, host commands and reviewed results |

The app compiles shared bootloader entry, runtime, panel, integrity and I²C
sources into its own image. App-only clock handling is selected at build time;
uploading the app does not install newly built bootloader code.

## Remaining work

Optical origin, shading, white balance, colour reconstruction and sensor geometry
need calibration. Travel scaling and homing repeatability need broader physical
testing. The preview aspect ratio is approximate and highlights may clip.
Scanning currently uses the supplied USB host script; standard scanner-driver
integration, faster acquisition and physical printing remain unfinished. The
prefire awaits its first physical run; the moving-band print path (resource
mode 3, band object, carriage windows, column clock) and the nozzle map are
not recovered yet.
