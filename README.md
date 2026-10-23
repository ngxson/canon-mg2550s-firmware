# Canon PIXMA MG2550S custom firmware

**WARNING: Work-in-progress**

Current state:
- Dumped the firmware
- RE the firmware flashing via USB
- Modify fw, push via USB --> works
- Make a minimal fw that blinks the LEDs --> works
- Source-built [USB/I²C bootloader](bootloader/README.md): five-second rescue
  window, separate application identity, and updates/reboots without power cycling.
  Rescue blinks only the power LED; the demo app cycles all four LEDs and accepts
  `python3 tools/fwctl.py reboot` over USB to return to download mode.
  Build instructions, wire protocols and test evidence are in that guide;
  [host tools](tools/README.md) live under `tools/`.

The current source uses a **64 KiB total bootloader reservation** (16 KiB first
stage + 48 KiB USB/I²C stage), leaving **1,984 KiB for the app**. This layout is
installed on the development printer. The bootloader also supports an explicit
USB `dangerous-write` command for full-chip replacement. It passed 53 emulator
cases plus the exact migration simulation, then was installed and rebooted over
USB without a power cycle. The one-time installer app was deleted afterward.
See [the migration and verification record](bootloader/evidence/DANGEROUS_WRITE.md).

## Application development

**Treat the installed bootloader as frozen for normal development.** New printer
features belong in the application. Normal `fwctl.py flash` uploads over either
USB or I²C write only the application region, `0x010000–0x1fffff`, leaving the
first **64 KiB** (`0x000000–0x00ffff`, both bootloader stages) intact.

After building and validating the app in emulation, upload its MGAP package:

```sh
python3 tools/fwctl.py flash bootloader/build/application.bin
# Or use the ESP32 I²C bridge:
python3 tools/fwctl.py flash bootloader/build/application.bin --transport i2c
```

The tool requests rescue mode, installs the app and starts it again. The build
currently generates all firmware images, but uploading `application.bin` does
not install the generated `bootloader.bin`, `stage0.bin` or `factory.bin`.
Version-2 application packages require the new layout; old and new packages
are rejected by the opposite layout before any erase.
Applications must follow the [app integration contract](bootloader/README.md#integrating-a-real-main-application)
and retain USB/I²C management handlers for reboot without power cycling. A hung
app can still require a manual power cycle to reach the bootloader's rescue window.

"Frozen" is the development policy, not a hardware write lock. The first stage
is also hash-pinned by the build; the second stage can be deliberately replaced
using the separate `stage0-flash` maintenance command. Explicit USB
`dangerous-write` or external SPI programming can replace the entire chip,
including the first stage. Those operations are outside routine app updates;
arbitrary app code that directly drives flash hardware can bypass the software
protection. See the [flash layout and protection details](bootloader/README.md#flash-and-ram-layout).

For deliberate whole-chip replacement, including returning to a stock dump:

```sh
python3 tools/fwctl.py dangerous-write path/to/full-2MiB-image.bin --sha256 EXACT_SHA256
```

This requires an exact 2 MiB image and its SHA-256, verifies all flash read-back,
and leaves reboot explicit. Power loss during boot-block replacement can require
SPI recovery. See the [command details](bootloader/README.md#explicit-whole-flash-writes).

WIP / TODO:
- Polish the emulator and publish it here
- RE all other peripherals:
  - [x] LEDs
  - [ ] Buttons
  - [ ] Scanner
  - [ ] Motor driver
  - [ ] Cartridge interface

![](./media/HVQBl_HWkAAjYIr.jpg)
