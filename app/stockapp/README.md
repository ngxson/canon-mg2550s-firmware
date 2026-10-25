# Experimental stock app: registered exception handlers

This observation candidate uses the generic bootloader vector ABI. It is mixed
source/proprietary firmware, not the standalone open-source replacement. The
bootloader itself contains no Canon code.

The subsequent [running-stock variant](runtime/README.md) retains normal stock
startup while guarding native restart/update paths and routing its USB
firmware-update request into held source rescue. It has separate emulator and
physical evidence under `emu/evidence/stockapp/runtime/`. The plain registered
candidate described below remains an earlier, unguarded experiment.

The [bootloader candidate](../../bootloader/evidence/VECTORS.md) executes real
exception dispatch in emulation. **Do not flash this stock package yet:**
management polling and safe restart/update redirection remain incomplete in
this full-stock variant. Independent reset-time recovery has passed physically.

A separate [startup smoke candidate](smoke/README.md) now stops before panel/USB
startup, redirects restart/fault paths to held rescue, and passes 11 offline
cases. Its physical run confirmed the planned stop and return to rescue; all
64 KiB of bootloader read back unchanged and the original app was restored.
This does not make the full stock package hardware-ready.

`register.c` checks the bootloader descriptor and registers seven stock ARM
handler addresses. `entry.S` supplies only the banked stack/CPSR transition,
then enters stock at `0x11960`. Failed registration stops execution; it never
assumes a cache overlay or rewrites the vector page. `shim.ld` binds the ABI
descriptor and places the shim in the app's RAM copy.

Build from the public repository root after building the vector bootloader:

```sh
python3 emu/stockapp/build.py --register-vectors --boot-dir bootloader/build/vectors
```

Outputs are isolated in ignored `emu/output/stockapp/registered/`. The shared
builder retains stock helpers and redirects the overlapped short-delay veneer
as documented in the [layout experiment](../../emu/stockapp/README.md). It reads
and checks the boot region/manifest; it never updates them.

In the standard offline emulator environment:

```sh
python3 emu/stockapp/probe.py startup --candidate emu/output/stockapp/registered/candidate.bin --output emu/output/stockapp/registered/startup.json
python3 emu/stockapp/probe.py boot --candidate emu/output/stockapp/registered/candidate.bin --eeprom /inputs/eeprom.bin --output emu/output/stockapp/registered/boot.json
python3 emu/stockapp/probe.py home --candidate emu/output/stockapp/registered/candidate.bin --eeprom /inputs/eeprom.bin --output emu/output/stockapp/registered/home.json
```

Repeat with `--from-reset --boot-build bootloader/build/vectors`, writing
`reset-startup.json`, `reset-boot.json` and `reset-home.json`, to retain the real
loader's CPU/RAM/stored MMIO state. The behavioral peripheral model changes at
app entry. `--vectors overlay` is rejected for registered candidates.

Stock C runtime, RTOS, exception handlers and peripheral drivers remain binary
dependencies for observation while source implementations replace them in the
separate open app. The reserved registration SRAM is checked after the deep
reset-based boot/home scenarios.
