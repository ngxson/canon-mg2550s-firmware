# 64 KiB bootloader layout — 2026-10-23

**Historical initial candidate; superseded by the installed dangerous-write
revision.** The initial candidate below was built/emulated while the printer
still ran layout version 1; see [that read-only check](layout64/existing_board.json).
The same 64 KiB layout, with the added whole-flash USB command, was subsequently
installed using a one-time RAM app, without SPI or power cycling. Current hashes
and physical results are in [DANGEROUS_WRITE.md](DANGEROUS_WRITE.md).

## Layout and compatibility

| Flash range (inclusive) | Reserved size | Contents |
|---|---:|---|
| `000000–003fff` | 16 KiB | Reset, hardware initialization, independent I²C recovery |
| `004000–00ffff` | 48 KiB | USB/I²C bootloader and its MGAP header |
| `010000–1fffff` | 1,984 KiB | Main app and its MGAP header |

Both bootloader stages together occupy one reserved **64 KiB** block. Ordinary
app updates cannot erase/program below `0x10000`. Stage-0 maintenance uses
4 KiB `20h` sector erases and guards both ends of its `0x4000–0xffff` destination;
it cannot overwrite stage 0 or the main app. Normal app updates keep the existing
64 KiB `D8h` erase and 4 KiB DMA programming paths. Each path validates the whole
package before erasing, verifies flash read-back, and commits its header block
last. The first stage is pinned to the new 16 KiB image hash.

The RAM addresses and entry contract are unchanged: stage 0 at `0x20000000`,
USB/I²C bootloader at `0x20010000`, main app at `0x20020000`. The USB identities,
five-second window, rescue power-only blink, app four-LED sequence, and reboot
commands are retained.

MGAP package version and MGFW INFO version are now **2**. Version-1 packages
belong to the 128 KiB layout and are rejected before erasure by this firmware;
old firmware likewise rejects version-2 packages. The I²C RSC1 transport frame
version remains 1. Host status/reboot understands both firmware layouts;
ordinary uploads target version 2. The bridge must be rebuilt for version-2
packages and the 48 KiB maintenance limit.

## Original candidate artifacts (superseded)

[Manifest with source hashes and symbols](layout64/manifest.json).

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `stage0.bin` | 16,384 | `2b68ecfc23016d708256318cff7d20338d2351d001b142743ac5b2d6f79b7995` |
| `bootloader.bin` | 9,056 | `d069a61a85d532eb28bc78eca3389ac194eab26785abd982b7fed0589d95a24e` |
| `boot_region.bin` | 65,536 | `399bc1d0757cce30bcacc1b72aabdfcac3eb93e78765f53bf2a4fdde2b0388ef` |
| `application.bin` | 7,296 | `b64c865b2ac415299c4aa59b1a99a49859e7dff9d9c6ad3f66dd08745ba5eaea` |
| `factory.bin` | 2,097,152 | `ffc7339efff0483a5db1f434d33d27ee3d153b759850074253a6dc30c38253fa` |

An [isolated public-source build](layout64/source_only.json) reproduced all five
images. The ESP32 bridge also compiles successfully:
[build log](layout64/esp32_build.log), [manifest](layout64/esp32_manifest.json).
Its new build has not been uploaded, so the current board/bridge pair remains
compatible with the previous layout.

## Validation

All **31 ARM emulator cases passed**:
[validation.json](layout64/validation.json). In addition to the previous LED,
USB, I²C, timeout, reboot, corruption and power-loss cases, this revision tests:

- Rejection of version-1 app packages with no erase.
- An app crossing a 64 KiB boundary: erases at `0x10000` and `0x20000`, with
  the entire first 64 KiB unchanged and the app header programmed last.
- Repair of a damaged second stage through independent first-stage I²C recovery:
  three 4 KiB erases at `0x4000`, `0x5000`, `0x6000`, followed by successful
  bootloader and app launch. Stage 0 and the main app remain byte-identical.
- Rejection of version-1 and oversized maintenance packages without erasure.
- A simulated interruption after a second-stage payload write, then successful
  first-stage recovery on restart.

Host checks accept the new app and bootloader packages, reject old packages for
normal upload, and recognize both INFO layouts. These tests do not establish
physical behavior of the new `20h` command through the Canon SPI controller.
The flash-chip instruction is documented in the
[Winbond W25Q16CV instruction table](https://www.alldatasheet.com/html-pdf/443789/WINBOND/W25Q16CV/6014/20/W25Q16CV.html).

## One-time installation from the 128 KiB layout

The initial plan was external SPI installation of `bootloader/build/factory.bin`.
The user instead requested a temporary RAM app because SPI was unavailable.
That migration is now implemented and physically verified in
[DANGEROUS_WRITE.md](DANGEROUS_WRITE.md). The full image contains both new stages
and the demo at its new offset; `boot_region.bin` alone does not move the app.

The ordinary USB/I²C update commands intentionally cannot replace stage 0.
`stage0-flash` replaces only stage 2 and therefore cannot migrate this layout.
The historical `install_legacy.py` also cannot perform this migration. The new
explicit dangerous-write path executes from RAM and replaces the whole chip;
its interrupted-write tests and limits are recorded separately.

During SPI programming, disconnect the printer's normal power and USB and use
only the programmer's 3.3 V supply for the flash, with common ground. Once the
full image is verified, disconnect that supply before restoring normal printer
power. Rebuild/upload the ESP32 bridge for version 2 before its next image upload.

The subsequent USB migration verified INFO version 2, app offset `0x10000`, the
normal five-second window and USB reboot. Physical tests of first-stage
maintenance's new 4 KiB erase behavior and the rebuilt version-2 ESP32 bridge
remain outstanding; do not count the earlier 128 KiB hardware tests as proof of
those changed paths.

Earlier physical tests in [STATUS.md](STATUS.md) and [LED_MODES.md](LED_MODES.md)
apply to the 128 KiB layout and do not count as installation verification for
this revision.
