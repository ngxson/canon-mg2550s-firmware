# Earlier stock-as-app hardware attempt — 2026-10-25

This is the historical first attempt. The later native-startup/Power-pulse build
now responds as Canon firmware 2.050; see [current bring-up evidence](../bringup/README.md).
Statements below describe the state at 18:41 UTC, before those experiments.

**Stock programming was verified with `C0`, but stock USB startup failed its
observation check. The exact original open app is now restored and responding.**
The running-stock image had no smoke stop; it was removed during the requested
recovery described below.

The 2,031,616-byte MGAP package has SHA-256
`b6be3687ab4fc0286922b82734f37b4152a8006d8f4fec117896222e9fad5e13`.
It was sent only through protected request `0x43`, starting at flash `0x10000`.
Both bootloader stages are outside the update destination. At
`2026-10-25T18:27:47Z`, INFO returned `C0`, idle and valid app, and the host
requested application startup. [Physical transfer/startup report](physical_install.json).
No dangerous-write command was used. No new complete boot-region readback was
performed during this attempt; the earlier smoke experiment's readback and
same-image first-stage recovery verification are separate evidence.

The user reported **Power LED steady on** while the host waited for USB. The
host did not observe the expected Canon printer-class identity with
`VER:2.050` during the complete 300-second wait. The
[final USB inventory](usb_final.json) contains no Canon device. The report's
`error` outcome refers to this startup check, after the successful flash
verification, not a rejected or incomplete upload. LED state alone does
not identify the executing PC or prove successful stock initialization.

A [non-rebooting I²C check](i2c_startup_check.json) also received no header/read
traffic in either orientation and left GPIO4/5 floating. The same wiring had
passed the full request/read/rejection exchange immediately before flashing.
The running-stock build uses stock USB for management, rather than polling the
open firmware's I²C mailbox alongside stock's EEPROM driver; silence therefore
does not establish that the wires are wrong. It also did not demonstrate a
return to cooperative rescue. The USB rescue-command hardware phase was not
run without the prerequisite stock USB identity.

The [source shim](../../../../app/stockapp/runtime/README.md) redirects native
reset/update paths to source rescue and intercepts the stock USB parser's
firmware-update event before it reaches the native updater. Those changes have
passed emulation, but this attempt does not establish full physical stock
operation or delivery of the USB rescue command. The previously tested
reset-time I²C loader remains the independent recovery mechanism. A later
user-requested power cycle and original-app restoration are recorded below.

## Historical recovery at 18:41 UTC

The user manually power-cycled and reported Power LED solid for approximately
10 seconds, then blinking, then off. The [first watcher](open_app_restore.json)
did not capture management USB and performed no write or reset. A replacement
watcher was active before the [remote power-cycle request](restore_power_cycle.json)
at `18:40:41.568 UTC`, which returned HTTP 200. The watcher captured and held
source USB rescue at `18:41:16.050 UTC`, about 34.5 seconds after that request.
This interval includes the external power-control sequence and printer startup;
it is not an isolated measurement of CRC time or the USB attachment instant.

The [restore report](open_app_restore_retry.json) records protected request
`0x43` writing the exact 95,328-byte original app, SHA-256
`47f078c2a3cc75dac1d67dd02c0531e9c85e47f67b0cfe0edfa471d9e582b15a`.
It returned `C0` at `18:41:19.664 UTC`; after application reboot, USB identified
`MG2550s App`, valid and idle, at `18:41:23.398 UTC`. Neither boot stage was an
update destination. There was no new complete boot-region readback in this
recovery. The unchanged original package had already passed emulator restore
checks and earlier physical restores. A concurrent non-flashing ESP32 wiring
probe completed `WIRING VERIFIED SDA4 SCL5 PROOF=REJECT90 FLOATING` and released
both pins. The stock image is no longer installed.

## What the failed startup establishes

The exact failing PC and cause are unknown. The earlier smoke image physically
proved entry through stock runtime to the stop at `0x135768`, before panel
component initialization. That is the last instrumented hardware milestone;
it does not establish how far this different, running-stock image progressed.
LED state and absent USB cannot distinguish a peripheral wait, IRQ problem,
intentional power state, or a fault. No diagnostic record from the failed run
was read before the power cycle.

There are concrete differences that the successful emulator runs do not settle:

- The shim enters `0x11960` after setting stacks and vectors, bypassing native
  startup calls including runtime MPU/cache setup at `0x11150`. Source reset
  disables MPU/caches; the shim does not reproduce the native setup. This is a
  known difference, not proof that disabled caches caused this failure.
- `emu/stock/boot.py` supplies PHY bit 8 at `0xc0c88024` and core bit 31 at
  `0xc0780010`, satisfying stock waits at `0x49aa0` and `0x49abe`. It also
  supplies timer completion and IRQ events. Actual hardware must produce these
  conditions. The boot test does not perform USB enumeration.
- `emu/stockapp/probe.py` transfers CPU/RAM/stored MMIO from the bootloader
  model, then changes to the stock peripheral model at app entry. RAM aliasing,
  cache behavior and electrical timing are not established by that transition.

The next useful diagnostic is staged, bounded app-only stops around panel
initialization, first timer IRQ, and the two USB readiness waits, returning a
checked SRAM progress/register record to rescue. Each variant needs emulator
validation before any future hardware run. Such probes would identify the
first hardware divergence; no additional candidate was flashed during this
analysis.

[Validation](validation.json) separates emulator results from hardware:

- Real bootloader reset through stock boot and scripted home passed.
- Normal boot matched the earlier registered stock app exactly for all 16,135
  MMIO writes, 106 motor writes, 117 EEPROM transactions and 4,606 task restores
  with the same input fixture.
- Three native reset functions and direct native restart entry returned to
  rescue before reaching the replaced boot code.
- Actual BJL/XML parsers, with modeled FIFO input, routed the firmware-update
  request to held rescue; an unrelated event continued normally.
- The full-slot protected upload committed the app header last and preserved
  every boot byte in the emulator.

Stock runtime, RTOS, USB and device drivers remain proprietary observation
dependencies. This is not the final open-source replacement firmware.
