# Optional application exception vectors — candidate revision

2026-10-25. **Installed; the entire 64 KiB boot region was physically read twice
and matches the tested build byte-for-byte.** A user-authorized temporary audit
app supplied bounded readback after the original whole-flash writer lost USB
communication before `C0`. The original source app was then restored through
the protected updater (`C0`) and is responding over USB. See the
[physical installation record](vectors/PHYSICAL.md) and
[direct readback comparison](vectors/audit/physical_read.json). The earlier USB
timeout's cause remains unresolved. The original offline validation below
performed no printer access or physical motor operation.

[Checked summary](vectors/summary.json), [build manifest](vectors/manifest.json),
[vector cases](vectors/vectors_validation.json),
[migration rehearsal](vectors/migration_validation.json).

## Result

Applications can now register ARM exception handlers without placing any Canon
addresses/code in the bootloader. Reset clears registration before initializing
the board; independent first-stage I²C recovery remains available. Normal app
updates still protect the entire first 64 KiB. The second-stage bootloader and
demo are byte-identical to the saved pre-extension build.

The experimental stock app executes the real registration function, flash
vectors, wrappers and C validator. **No cache overlay or vector-fetch
substitution is used.** It reaches panel startup, runs 1,000 RTOS ticks and
reaches optical-reference entry after scripted seek/backoff.

## ABI and implementation

[`include/vectors.h`](../include/vectors.h) defines the read-only descriptor at
flash `0x40`: magic `0x3158564d` (`MVX1`), version 1, size 24, state address
`0xe0000880`, then ARM pointers to `install(targets[7])` and `clear()`.
An app must check the descriptor before calling it on an older bootloader.

Indices are undefined instruction, SVC, prefetch abort, data abort, reserved,
IRQ and FIQ. Reset is never registerable. Zero entries select the default fault
loop. Registration requires privileged ARM execution with IRQ/FIQ masked.
Nonzero targets must be word-aligned ARM entries within the current MGAP
payload's declared flash or copied-RAM range. The loader already checked CRCs;
dispatch rechecks header format/bounds rather than recalculating the whole CRC.
This validates addresses, not handler semantics, and is not a hostile-code sandbox.

Apps must reserve SRAM `[0xe0000880,0xe00008c4)`. Magic/complement, version and
each target/complement are checked. Registration commits magic last; a failed
replacement registration disables forwarding. Reset clears magic before board
initialization, so old registrations cannot survive a restart. `clear()` disables
and wipes the complete table.

The wrapper preserves r0–r12, SP, LR, CPSR flags and SPSR. It aligns a temporary
stack for C, then restores the original SP before entering the handler. Both
SP modulo 8 values 0 and 4 are tested; maximum measured wrapper/validator stack
use is **68 bytes**, before the app handler's own use. Reset establishes internal
SRAM stacks for UND/ABT/FIQ/IRQ. Apps own their exception stacks after handoff.

Validation and dispatch remain in flash and do not depend on stage-0 RAM
surviving stock scatter loading. Assembly is limited to exception entry, CPU
state preservation, banked stacks/status, and existing CP15/reset operations.
Registration and target checks are C in [`src/vectors.c`](../src/vectors.c).

## Validation

All **97 bootloader checks** pass:

- 31 existing startup, USB/I²C, timeout, protected-update, damaged-stage recovery
  and interrupted-maintenance cases.
- 21 base whole-flash writer cases: malformed inputs, read-back failure, retry,
  and early/late interruption. The optional stock restoration fixture was omitted.
- 42 new vector cases: all seven exceptions and both stack alignments, flash/RAM
  callbacks, register preservation, default entries, rejected pointers/targets/
  caller states, corrupted registrations/headers, reset disarming, and current
  source-app boot plus management reboot into rescue.
- Three migration phases: the saved old writer installs the candidate, software
  reset reaches the current built source app, then a normal app upload/restart
  succeeds through the new bootloader.

A second build reproduced all five artifacts byte-for-byte. The evidence
collector checks firmware source hashes against the manifest.

Six stock scenarios pass: startup, boot and scripted home, each starting at the
shim and starting at the actual loader reset.

| Scenario | SVCs | IRQs | Outcome |
|---|---:|---:|---|
| Startup | 878 | 0 | Panel component `0x135768` |
| Boot | 24,249 | 2,412 | 1,000 ticks; idle `0x104da2` |
| Scripted home | 24,141 | 2,914 | Optical-reference `0xf4c2c`, tick 888 |

With identical synthetic initial conditions, ordered MMIO writes, EEPROM
transactions, context restores and motion observations match native stock,
excluding instruction counts. Boot/home have 15,735/17,984 matching MMIO writes.

Reset runs execute the actual loader's CRC checks and full 2,031,584-byte copy.
At app entry, its complete Unicorn CPU context, 8 MiB app RAM, internal SRAM
and stored MMIO values transfer to the stock peripheral model. No firmware
function or return is replaced. This is an explicit **peripheral-model boundary
at app entry**, not a unified or cycle-accurate ASIC simulation.

Retaining initialized clocks/timers/USB changes some writes relative to the
zero-initialized C-runtime fixture: there are 15,728 boot writes and 17,972 home
writes. Every difference is recorded in the summary. Motor-controller writes,
EEPROM transactions, context restores, motion requests and feedback still match.
The registration remains intact through boot/home, and no runtime flash store
or SPI-controller write occurs. Stock's observed SRAM scatter initialization
ends at `0xe0000354`, below registration storage.

## Migration model correction

The first same-emulator migration failed although flash read-back matched.
Unicorn 2.1.4 reused translated old reset instructions while data reads saw
new bytes. Old `0x104: ldr sp,[pc,#12]` loaded new instruction `0xe321f0d7`
at `0x118` as SP; `boot_c` faulted writing `0xe321f0c7`. A fresh emulator
booted the candidate successfully.

The SPI model now discards translated blocks for the exact erased/programmed
range after changing memory. This fixes peripheral writes bypassing CPU-store
translation invalidation; it does not model a hardware cache or skip reset.
The same-emulator migration now passes, and affected suites were rerun.
The source bootloader disables caches; cache timing remains unmodeled.

## Artifacts and physical boundary

| Artifact | SHA-256 |
|---|---|
| 16 KiB stage 0 | `b5383aa702982e413601684c9982257ab4aced095e83769799740aaa145c30e8` |
| 64 KiB boot region | `f22df711f7afd8c3804c43e2d491b8029af59a49f6190c2cafe1097a702e4b92` |
| Install image with current built source app | `8701d5ac9b52afef966bab588632ad28bc5c70380bffcb3819918d764ace94a3` |

Artifacts are in `bootloader/build/vectors/`. `install.bin` contains the source
app, **not stock-as-app**. The saved `bootloader/build/` baseline is untouched.
The source pin was deliberately revised. The physical readback now confirms
that exact first-stage image is installed; see the installation record above.

Installation must replace stage 0 once, using explicit whole-flash writing or
SPI. Ordinary app uploads and first-stage maintenance cannot replace it. An
interruption during boot-sector replacement can still require external SPI;
the late-interruption test continues to demonstrate that limit. Normal update
guards were not weakened.

Physical management reboot, protected app updates, and return to the original
app passed during the audit. Independent reset-time I²C recovery subsequently
[passed on hardware](vectors/stage0_physical.json): the exact existing stage 2
was rewritten through stage 0, returned `C0`, then enumerated held USB rescue
and restarted the original app. The ESP32 now detects either signal order
[automatically](../../tools/esp32/evidence/autowire/PHYSICAL.md).
The full stock package still needs management polling and safe restart/update
redirection (SVC 7/8 and updater entry); it is not ready for unrestricted use.
The subsequent [startup smoke](../../emu/evidence/stockapp/physical_smoke/README.md)
physically reached its planned stock Thumb SVC stop and returned to held rescue.
The checked SRAM record confirms the callback and exact caller; a complete boot
readback remained identical and the original app was restored. This validates
the limited hardware path, not arbitrary exception/IRQ timing or full stock use.
Real interrupt timing/latency, arbitrary jobs, deeper stack use, optics and motor
physics are not established; scripted homing stops before optical calibration.

## Reproduce

From the public repository root, build on the host with the documented compiler:

```sh
python3 bootloader/build.py --output "$PWD/bootloader/build/vectors"
python3 emu/stockapp/build.py --register-vectors --boot-dir bootloader/build/vectors
```

In the standard offline emulator container (`/project` working directory):

```sh
MG2550S_BOOT_BUILD=/project/bootloader/build/vectors python3 bootloader/tests/validate.py
MG2550S_BOOT_BUILD=/project/bootloader/build/vectors python3 bootloader/tests/validate_dangerous.py
MG2550S_BOOT_BUILD=/project/bootloader/build/vectors python3 bootloader/tests/validate_vectors.py
```

Use `emu/stockapp/probe.py` with the registered candidate and default `--vectors
flash`; add `--from-reset --boot-build bootloader/build/vectors` for reset
variants. Boot/home use the same read-only EEPROM fixture as native stock.
See [stock-app sources](../../app/stockapp/README.md). Raw reports remain ignored
and may contain EEPROM transaction bytes; the committed summary omits them.
