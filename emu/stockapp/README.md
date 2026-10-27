# Stock as an app: offline feasibility experiment

The default build below is the original unchanged-bootloader experiment.
`--register-vectors --boot-dir bootloader/build/vectors` instead builds the
[registered-vector candidate](../../app/stockapp/README.md), using the
user-authorized [bootloader extension](../../bootloader/evidence/VECTORS.md).
That variant executes real flash dispatch and rejects `--vectors overlay`.

This experiment keeps the production **entire 64 KiB boot region byte-identical**
and asks which stock dependencies prevent app-only installation. It does not
access hardware, modify bootloader sources/builds, or change the normal app.

**The generated package is for emulation only. Do not flash it.** It has no
hardware vector redirection or running-stock management/reboot integration.
With the actual frozen vectors, its first SVC reaches the bootloader fault loop.
The explicit `overlay` scenario models a possible future instruction-cache
vector overlay. It does not implement or validate that cache on hardware.

Results and remaining work: [STOCK_AS_APP.md](../evidence/STOCK_AS_APP.md).
This is mixed source/Canon firmware for observation, not open replacement firmware.

## Build and reproduce

From the public repository root, with the documented Arm compiler and existing
production `bootloader/build/boot_region.bin` and manifest:

```sh
python3 emu/stockapp/build.py
```

The builder reads but never builds/writes the bootloader. Outputs are isolated
under ignored `emu/output/stockapp/`. It requires the pinned original stock
image, production stage-0 hash and a matching boot-region manifest.

Run these Python commands in the established `mg2550s-emu` Docker environment
(see the [emulator guide](../README.md)), mounting the public tree at `/project`
and your existing raw 1024-byte EEPROM snapshot read-only at `/inputs/eeprom.bin`:

```sh
python3 emu/stockapp/probe.py startup --candidate emu/output/stockapp/candidate.bin --expect vector-fault --output emu/output/stockapp/startup-frozen.json
python3 emu/stockapp/probe.py startup --candidate emu/output/stockapp/candidate.bin --vectors overlay --remove-helpers --expect helper-fault --output emu/output/stockapp/startup-missing-helpers.json
python3 emu/stockapp/probe.py startup --candidate emu/output/stockapp/candidate.bin --vectors overlay --output emu/output/stockapp/startup-overlay.json
python3 emu/stockapp/probe.py boot --eeprom /inputs/eeprom.bin --output emu/output/stockapp/boot-stock.json
python3 emu/stockapp/probe.py boot --candidate emu/output/stockapp/candidate.bin --vectors overlay --eeprom /inputs/eeprom.bin --output emu/output/stockapp/boot-overlay.json
python3 emu/stockapp/probe.py home --eeprom /inputs/eeprom.bin --output emu/output/stockapp/home-stock.json
python3 emu/stockapp/probe.py home --candidate emu/output/stockapp/candidate.bin --vectors overlay --eeprom /inputs/eeprom.bin --output emu/output/stockapp/home-overlay.json
python3 emu/stockapp/validate_loader.py
python3 emu/stockapp/summarize.py
```

Do not use Python `-O`: inherited stock-model assertions are part of validation.
Large raw reports remain ignored and can contain private EEPROM transaction
bytes. The compact committed summary omits those bytes. It compares ordered
observations and hashes the normalized traces; only instruction-count fields
are excluded because the shim and vector branches add instructions.

## Source and binary changes

- `build.py`: constructs a normal layout-2 MGAP package, retaining stock flash
  addresses and all helper bytes at `0x10094..0x1010f`.
- `shim.S`: banked stacks/CPSR and jump to stock C runtime. These CPU operations
  require assembly. No driver, OS or application behavior is implemented here.
- `probe.py`: uses existing startup, boot, IRQ and scripted home models. The
  optional vector-fetch substitution never changes underlying flash bytes.
- `validate_loader.py`: executes production reset, CRC validation, normal USB
  update and full RAM copy; stops before app entry. Also verifies a fresh reset
  can hold rescue. It does not model an automatic reboot out of stock.
- `summarize.py`: requires successful positive/negative controls, compares the
  full ordered observation lists and records source/input/report identities.

The app header and first payload branch replace `0x10000..0x10023`. The shim
occupies otherwise unused flash starting at `0x10110`. The short NOP-delay
helpers are copied to `0x10d00`, and the verified veneer at `0x1509c8` is
redirected there. Other runtime bytes from `0x11000` onward are unchanged.
The compiler's 60-byte shim is executed from the bootloader's full RAM copy.

Stock dependencies remain extensive: C runtime, RTOS, exception handlers and
all peripheral drivers. Their role is observation and comparison while source
drivers replace them in the separate open application.

## Running one stock routine as a fixture (hybrid shape B)

`fixture.py` boots the unmodified stock image to idle in the public emulator,
snapshots RAM, and direct-calls stock functions with the RTOS services
(event flags, semaphores, mailboxes, pools, delay) either stopped at or
stubbed and logged. `fixture_summary.py` prints the ordered service calls,
decoded mailbox requests and MMIO writes of a saved report. Named cases cover
the head-maintenance entries from RE brief 16; see
[the maintenance evidence](../evidence/stockapp/maintenance/README.md) for
the conventions (system mode, scratch allocator, zero status replies) and
results.
