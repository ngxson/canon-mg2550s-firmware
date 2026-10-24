# Public emulator validation — 2026-10-24

Validated in the supplied Docker image: Python 3.12, Unicorn 2.1.4, Linux arm64.
The public repository was mounted as `/project`; stock boot/homing received a
separate read-only 1 KiB EEPROM input. The emulator did not import internal code.
No printer, ESP32, USB or serial device was accessed.

- Eight CLI/state regression tests passed: bad firmware/EEPROM inputs,
  output/input alias protection, incompatible options, disabled assertions,
  failed-scenario reporting, and repeatable architectural state comparison.
- Stock startup reached panel initialization. The ten-case startup matrix passed,
  including stalled-transfer release and the earlier-handshake negative control.
- Scheduler, no-IRQ and spurious-IRQ reference checks passed at 400 ticks.
- Stock boot passed at 2,000 ticks with all modeled readiness inputs enabled.
- Frozen-feedback HOME passed at 3,500 ticks: five move attempts and stock error 5.
- Scripted backoff reached optical-reference entry at tick 888; the completion
  handler disabled drive. Optical homing itself is outside the modeled path.
- With the same scripted travel but completion withheld, the stock backoff remained
  pending at 1,600 ticks and did not enter optical calibration.
- `test --suite all` passed all eight replacement-firmware suites: 31 baseline
  bootloader cases, 21 whole-flash cases, and six application suites. The optional
  stock-restoration case in the bootloader suite was not enabled.

The three HOME traces were compared against the retained internal reference
runs. Instruction counts, move checkpoints, MMIO/capture writes, feedback reads,
mailbox events, IRQ dispatch, final registers and motor state match exactly.
The comparison summary is in [evidence/validation.json](evidence/validation.json).
Full stock traces and EEPROM transaction data remain local in ignored output files.

One portability correction was needed: raw Unicorn context buffers contain
implementation data that can differ between saves. The public startup matrix
now hashes mapped memory and architectural registers across CPU banks from a
detached saved context. Tests check stable snapshots, unchanged live CPU state,
and detection of memory and banked-stack changes. This does not alter firmware
execution or peripheral inputs.

Validated replacement app: 31,008 bytes, SHA-256
`28c045e55c74fec907f824c8f5b12d7fb3f3a77993c97cf5ecba11157c97605f`.
Boot region: 65,536 bytes, SHA-256
`fb5b09443f5717aa81c8675db491a6b86db9f471130506768979a4b9726d9d8e`.
Neither image was rebuilt or flashed for this export. The internal WIP emulator
sources were retained unchanged.

These results validate the stated software scenarios and model inputs. They do
not establish electrical, mechanical or optical behavior; see the
[model boundaries](README.md#reports-and-model-boundaries).

## Cartridge model correction — later on 2026-10-24

The earlier export results above remain historical. Following the physical
breakthrough recorded in [CARTRIDGE.md](../app/evidence/CARTRIDGE.md), the shared
head model now rejects the incomplete setup that the old model accepted.

- All **12** public unit/CLI/state tests passed, including four new head-model
  tests for failed subset/full configuration, captured bits/readbacks, retention
  versus explicit cold reset, and invalid transactions/injected failures.
- `emu/run.py test --suite cartridge` passed **eight** ARM scenarios: both heads,
  keep-prepared/release, raw mode, disconnected colour head, incomplete/full/
  retained initialization, captured-word replay, rejected requests, and management
  service followed by reboot into the bootloader. No fuse-cut or flash writes.
- Full initialization gives black `0x64f12b7f`, colour `0xa737853f` plus `0x03ef`
  high bits when those captured words are supplied as model inputs. Controls
  alone or the earlier partial configuration return all ones after inversion.

Detailed report: [evidence/cartridge_validation.json](evidence/cartridge_validation.json).
Latest tested app: 74,176 bytes, SHA-256
`299f2d813d5c7d52fdb5bc813371dbe41b65c6d701e885e0d6e8d386e3336913`.
The same eight scenarios first passed on the 73,120-byte app
`54e49a134ea82c438377a1721c1306c5de67b38af575d5bc9ba5378fe64de1c5`;
that [earlier report](evidence/cartridge_validation_54e49a13.json) is retained.
The suite was rerun after a concurrent application rebuild. Both reports record
the boot-region hash. Existing firmware artifacts were used; this emulator work
did not rebuild, flash or access hardware. Other ARM suites were not rerun for
this change; the earlier eight-suite export result is not a test of this app.

The model treats the fixed register configuration as a group. Individual
prerequisites and electrical meanings remain unproven. Captured fuse data are
test inputs, and a modeled cold reset is not physical cold-start confirmation.
