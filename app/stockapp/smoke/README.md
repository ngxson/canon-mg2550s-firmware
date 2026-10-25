# Stock startup smoke — physically passed, original app restored

2026-10-25: [physical experiment and evidence](../../../emu/evidence/stockapp/physical_smoke/README.md).
Protected upload returned `C0`. The SRAM record confirms the planned Thumb
`SVC #fe` at `0x135768`, return to held rescue, and the entire 64 KiB boot region
read back unchanged. The exact original open app has been restored. This is
limited stock startup; stock panel/USB operation was not entered or validated.

This is a deliberately limited first hardware experiment, not the full stock
application. It runs the real stock C runtime and early task setup, then traps
at the entry to panel startup (`0x135768`) and requests the source bootloader's
held rescue mode. Stock panel/USB initialization and print/scan jobs are outside
this run. It is not a watchdog: an earlier hardware polling loop can still hang.
Independent physical recovery was established before upload via an exact
same-image second-stage reinstall through stage 0, followed by USB rescue and
normal app startup. [Recovery evidence](../../../bootloader/evidence/vectors/stage0_physical.json).

The app still contains proprietary stock runtime/RTOS code as an observation
dependency. The source bootloader remains unchanged. Build inputs are pinned;
the additional patch replaces `0c4910b5` with Thumb `svc #0xfe; b .` at `0x135768`.
The existing delay-helper preservation and vector ABI are retained.

`entry.S` contains only banked stack/CPU and exception entry/exit operations.
`control.c` checks/registers the vector ABI and writes a checked observation
record at SRAM `0xe00008d0..0xe00008ef`. It redirects SVC 7/8, undefined/abort/
reserved exceptions and FIQ to held rescue. Other SVCs preserve incoming
registers/flags and enter stock's original SVC handler; IRQ still enters stock.
Recovery callbacks execute from app-slot flash, independent of stock RAM scatter
loading. They record the reason, request held rescue, disconnect USB and execute
the real reset vector. They never program flash.

Record words: magic `31504d53`, its complement, stage and complement, reason and
complement, exception LR, SPSR. Stage 1 is handoff pending; stage 2 is return.
Reason `fe` means the planned panel-entry stop, `7/8` stock restart requests,
`11..15` faults/FIQ, and `20` failed registration. This SRAM location is outside
the observed stock scatter initialization, vector registration and reboot cookie.

Build/test from repository root:

```sh
python3 emu/stockapp/build.py --register-vectors --startup-smoke --boot-dir bootloader/build/vectors
# In the standard offline emulator container:
python3 emu/stockapp/smoke.py
```

All 11 offline cases passed: startup executes 879 real SVCs and branches to
reset; the real loader then enumerates held rescue while preserving the record;
ARM/Thumb SVC 7/8 and the five fault/FIQ entry tests return safely in the model.
No flash store, SPI-controller write, erase or programming occurs. The reset
and stock peripheral models switch at the documented handoff boundaries; this
does not establish physical timing, RAM aliasing or independent recovery.

The Thumb stimulus initially failed because writing an even PC in Unicorn
cleared the Thumb bit after CPSR setup. The test now sets CPSR after PC and
asserts the intended state before executing the actual SVC. The real Thumb
`fe` startup trap already passed; no firmware change was needed for that fix.

Outputs are isolated under ignored `emu/output/stockapp/smoke/`; compact evidence
is in `emu/evidence/stockapp/startup_smoke.json`. Do not upload the full-image
`candidate.bin` through a whole-chip writer. Any eventual experiment must use
the MGAP package through the protected application updater, with an original
source-app restore image retained.

`hardware.py` performs the protected upload, waits for reset status `80` in
held rescue, installs the validated bounded SRAM/boot reader, and restores the
original source app after reading the evidence. The first physical return took
longer than its initial 45-second allowance; readout/restoration continued only
after USB and I²C both responded. That initial timeout report is preserved.
The wait is now 180 seconds; a timeout still performs no automatic reset or
reflash. `--resume-readout` requires a recorded, fully verified smoke upload
and a subsequently observed held rescue before continuing.

The reader is an isolated build of `app/boot_audit/build.py --stock-record`,
with its own emulation validation. It accepts only control-IN `0x96`, zero
value/index, length 32, for the fixed SRAM record; `0x95` reads only the boot
region. It is no longer installed.
