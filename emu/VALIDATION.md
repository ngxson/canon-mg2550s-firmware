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
