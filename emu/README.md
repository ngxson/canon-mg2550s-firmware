# MG2550s firmware emulator

A Unicorn-based ARM946E-S emulator for exploring Canon MG2550s stock firmware
and testing the source-built replacement firmware. It runs ARM instructions
against explicit peripheral models and produces JSON traces. It does not access
a printer, USB device or serial port.

This is the maintained public snapshot of the project's reverse-engineering
harness. Experimental work continues separately; this directory has no dependency
on that internal workspace.

## Quick start

Run from the repository root. Docker provides the tested Linux environment;
native macOS Unicorn has crashed while executing this CPU model.

```sh
docker build -t mg2550s-emu emu
docker run --rm --network none -v "$PWD:/project:rw" \
  mg2550s-emu emu/run.py startup --verify
```

This executes the included stock image from C runtime entry to panel component
initialization, then checks the observed path. The report is saved to
`emu/output/startup.json`. A successful check confirms the modeled execution
path, not physical LEDs or complete printer startup.

For native Linux, use Python 3.10+ and install the pinned dependency in a virtual
environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r emu/requirements.txt
python3 emu/run.py startup --verify
```

## Commands

Use `python3 emu/run.py COMMAND --help` for options. In Docker, substitute
`docker run --rm --network none -v "$PWD:/project:rw" mg2550s-emu` for `python3`.

| Command | Execution and inputs |
|---|---|
| `startup` | C runtime, constructors, scanner identification and panel startup; no EEPROM needed |
| `scheduler` | Grouped VIC interrupts, timer ticks and actual stock scheduler/context restores; no EEPROM needed |
| `boot` | Continues stock tasks with modeled EEPROM transactions, peripheral readiness and interrupts; requires `--eeprom` |
| `home` | Stock homing command, motor-register trace and stationary or scripted position inputs; requires `--eeprom` |
| `test` | Runs the existing replacement-firmware suites from reset; requires locally built firmware |

Stock commands accept `--firmware`, `--output`, `--budget` and `--verify`.
`--verify` checks the reference scenario's outcome and returns nonzero if it
fails. Without it, a bounded exploratory run can finish before the expected
milestone; inspect `stop_reason`, `pc` and `error` in the report. Firmware faults
and input-validation errors still return nonzero. Do not disable Python assertions
with `-O` or `PYTHONOPTIMIZE`.

Stock experiments use fixed addresses in this exact 2 MiB dump:
[`mg2550s_w25q16cv_full.bin`](../dumps/mg2550s_w25q16cv_full.bin), SHA-256
`4b032367cff5976aeb426c8723e08d3c431a38e622e245a614791a3e5067e533`.
Other images are rejected before execution. Replacement firmware uses its own
models and symbol manifests through `test`.

### Startup and interrupt controls

```sh
# Ten verified cases: response types, stalled transfers, release and early wait.
python3 emu/run.py startup --matrix --output emu/output/startup-matrix.json
# One missing scanner completion; expected to stay in the stock polling loop.
python3 emu/run.py startup --stall-transfer 1 --verify

python3 emu/run.py scheduler --verify
python3 emu/run.py scheduler --no-irq --verify --output emu/output/no-irq.json
python3 emu/run.py scheduler --spurious --verify --output emu/output/spurious.json
```

The scheduler defaults to 400 synthetic timer ticks. The two controls verify
that absent or spurious interrupts do not advance the scheduler tick count.
Ticks and instruction budgets are execution bounds, not calibrated milliseconds.
The startup matrix uses fixed per-case budgets.

### Stock boot and homing

Supply a raw **1,024-byte 24C08 EEPROM snapshot** for these scenarios. The
emulator never reads it from hardware; no device-specific EEPROM snapshot is
shipped. Put your input in the ignored `emu/inputs/` directory, or mount an
external file read-only into Docker. Stock writes affect an in-memory copy only.
Different EEPROM state can change stock behavior and fail reference assertions.

```sh
# Assumes your snapshot is saved as emu/inputs/eeprom.bin.
python3 emu/run.py boot --eeprom emu/inputs/eeprom.bin --verify

# Frozen feedback: stock seeks, retries/backoffs, then reports its motion error.
python3 emu/run.py home --eeprom emu/inputs/eeprom.bin --verify

# Scripted seek/stall/backoff: reaches optical-reference entry and stops there.
python3 emu/run.py home --eeprom emu/inputs/eeprom.bin \
  --feedback backoff --ticks 1600 --verify --output emu/output/backoff.json

# Same scripted positions, but no companion completion event/IRQ27.
python3 emu/run.py home --eeprom emu/inputs/eeprom.bin \
  --feedback backoff --no-completion --ticks 1600 --verify \
  --output emu/output/no-completion.json
```

The default frozen-feedback case runs for 3,500 ticks. The reference run issues
five moves and ends with stock error 5. In the scripted case, the first seek
moves 2,048 coordinate units then stalls; positive backoff advances 64 units
per harness tick. With completion enabled, real stock code processes IRQ27,
verifies 10,582 units of backoff and reaches optical-reference entry at tick 888.
This **does not complete optical homing**: pixels for those stock calibration
stages are not modeled.

Homing is selected by replacing the mode word in one stock-owned `0xc028`
message before its native mailbox send. The report records that input mutation.
No flash instructions, function returns, OS events or home-gate bytes are
patched. This is an internal-command experiment; it does not decode a physical
Copy-button press or a host scan request.

Additional controls: `boot --ready-level 0..4` progressively enables modeled
USB PHY/core and timer readiness; `home --mode 0` leaves the message unchanged;
`--no-timer4` and `--no-serial-irq` withhold the respective inputs. These home
controls use stationary feedback. The report lists the assumptions enabled.

### Replacement-firmware tests

Build the firmware with the documented Arm toolchain first. See the
[application guide](../app/README.md#build-and-validate) and
[bootloader guide](../bootloader/README.md) for build prerequisites and pinned
recovery-stage requirements.

```sh
python3 bootloader/build.py
python3 app/build.py

docker run --rm --network none -v "$PWD:/project:rw" \
  mg2550s-emu emu/run.py test --suite app
# Includes both bootloader suites and all six application suites:
docker run --rm --network none -v "$PWD:/project:rw" \
  mg2550s-emu emu/run.py test --suite all
```

`--suite app` checks startup/reboot, homing, motor commands, buttons, stationary
capture and full-page sequencing. `--suite bootloader` checks management,
protected updates, recovery and simulated whole-flash updates. These tests
operate on simulated flash only. A failed suite stops the runner with a nonzero
exit code. Reports remain in `app/build/` and `bootloader/build/`.

The runner reuses the existing public models and assertions in those directories;
it does not duplicate them. App tests combine `bootloader/build/boot_region.bin`
and its symbols with `app/build/application.bin` and its symbols. Keep these
artifacts consistent with the combination being tested. No stock dump or EEPROM
snapshot is needed for the replacement suites.

## Reports and model boundaries

Stock reports go to `emu/output/` by default; `--output` selects another path.
A repeated command overwrites its report, so use distinct filenames for comparisons.
An output path cannot alias the firmware or EEPROM input. `emu/output/` and
`emu/inputs/` are ignored by Git. Reports can include EEPROM transaction bytes;
review them before publishing.

Reports contain execution markers, register accesses, IRQ dispatch and context
restores, input hashes and unchanged-input checks. Homing adds move arguments,
controller writes, feedback reads and explicitly injected inputs. Long traces
can be large. [Validation notes](VALIDATION.md) record the public snapshot checks.

The stock model starts at `0x11960`, **after** bootloader, CPU, clock and RAM
initialization. It supplies the banked stack state, maps stock RAM at `0x18000000`,
and executes the real C runtime, SVC/IRQ handlers and RTOS. Unknown MMIO normally
reads back its last write or zero. Readiness, serial completion, EEPROM FIFO,
interrupt arbitration and timer progress are synthetic. The replacement model
instead starts at reset and uses its own RAM/layout and peripheral contracts.

Neither model reproduces motor physics, wiring, torque, inertia, optics, analogue
signal quality, cache timing or a complete ASIC. Scripted encoder feedback and
synthetic DMA data exercise software branches; they cannot prove physical motion,
homing accuracy or scan quality. Stock USB packet emulation and full stock scan
jobs are outside this public snapshot. Physical replacement-firmware results are
recorded separately in [homing](../app/evidence/HOMING.md) and
[page-scan evidence](../app/evidence/PAGE_SCAN.md).

## Source map

| File | Role |
|---|---|
| `run.py` | CLI, input/output checks, scenario selection and replacement-suite runner |
| `stock/inputs.py` | Supported stock hash and EEPROM size checks |
| `stock/startup.py` | CPU/memory setup, baseline MMIO, SVC entry and startup comparisons |
| `stock/scheduler.py` | Grouped VIC, IRQ entry, ticks and context-restore tracing |
| `stock/tasks.py` | EEPROM FIFO/completions and native task progression |
| `stock/boot.py` | Readiness levels, additional timers and boot tracing |
| `stock/home.py` | Scanner command input, timer4/serial IRQ and motor trace |
| `stock/feedback.py` | Explicit scripted seek/backoff and completion controls |
| `tests/test_cli.py` | Input protection, argument validation and runner checks |

Run CLI regression checks with:

```sh
docker run --rm --network none -v "$PWD:/project:rw" \
  mg2550s-emu -m unittest discover -s emu/tests -v
```
