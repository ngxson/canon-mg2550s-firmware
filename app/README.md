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

Run all six scanner and management checks before flashing:

```sh
for check in app home motor buttons scanner page; do
  docker run --rm --network none -v "$PWD:/project:rw" \
    mg2550s-emu "app/tests/validate_${check}.py" || break
done
```

Proceed only after all six pass. Reports are written under `app/build/`.
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
| `tests/`, `tools/`, `evidence/` | Emulator checks, host commands and reviewed results |

The app compiles shared bootloader entry, runtime, panel, integrity and I²C
sources into its own image. App-only clock handling is selected at build time;
uploading the app does not install newly built bootloader code.

## Remaining work

Optical origin, shading, white balance, colour reconstruction and sensor geometry
need calibration. Travel scaling and homing repeatability need broader physical
testing. The preview aspect ratio is approximate and highlights may clip.
Scanning currently uses the supplied USB host script; standard scanner-driver
integration, faster acquisition and physical printing remain unfinished.
