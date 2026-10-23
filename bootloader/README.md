# MG2550s USB / I²C bootloader

Source-built firmware for the Canon PIXMA MG2550s development board. On startup
it enumerates as **MG2550s Bootloader**, offers a five-second window for a host
to request rescue mode, then starts a valid main application. Rescue accepts
application updates over printer USB or the existing EEPROM SDA/SCL wires to
an ESP32-C3. Both the bootloader and the included demo application implement
software reboot commands, so normal development does not require power cycling.

**Layout version 2 reserves 64 KiB total for both bootloader stages.** It is
installed on the development printer. Validation includes 31 baseline cases,
22 dangerous-write cases, and the exact old-layout migration in emulation.
A one-time RAM-running app installed the full new image through USB, followed
by successful software reboot into this bootloader and the demo. That app was
deleted after installation. See [the hardware record](evidence/DANGEROUS_WRITE.md)
and [the original layout design](evidence/LAYOUT64.md).

This is the public source tree. The build does not read a Canon firmware dump,
extract executable bytes, or depend on the earlier private RE workspace.
The implementation is C except for reset vectors, stack/CPSR setup, CP15/cache
operations and the reset branch in `src/cpu.c` / `src/entry.c`.

This is a source implementation informed by reverse engineering, not a claim
of a formally separated legal clean-room process. Printing and scanning are
outside this bootloader milestone. Source licensing has not yet been selected;
Canon dumps elsewhere in the repository are not inputs to this build.

## Startup and recovery behavior

1. The first-stage recovery code initializes CPU, clocks, RAM and LEDs. It
   briefly probes the ESP32 for an **explicit bootloader-maintenance offer**.
2. It checks and starts the second-stage bootloader. A damaged second stage
   leaves first-stage I²C recovery running; first stage has no USB driver.
3. The second stage enumerates as the bootloader USB device and starts a
   **5,000 ms window from USB attachment**. Only the **power LED blinks** in this
   mode (power on/off, other three LEDs off). I²C polling runs alongside USB.
4. `STAY`, a valid update start, or a reboot explicitly targeting the bootloader
   makes rescue persistent until another reboot command. Ordinary enumeration,
   descriptor reads and status queries do **not** extend the window.
5. With no stay request and a valid app, the bootloader disconnects USB, waits
   100 ms, copies the app to RAM and starts it. The app enumerates with a distinct
   product ID and name. With a missing/invalid app, rescue remains active.

The demo application cycles **all four LEDs in sequence**, starting with power.
It accepts the USB reboot command to return to persistent download mode:

```sh
python3 tools/fwctl.py reboot
```

The frozen first-stage I²C recovery stub keeps its original four-LED sequence
before the USB bootloader starts, and while explicitly replacing that bootloader
or recovering a damaged second stage. Its panel code is retained separately in
`stage0/panel.c` so changing the visible USB bootloader pattern cannot change the
hash-pinned recovery sector.

The checked application header includes payload and header CRC32, length,
load address, entry offset and reserved fields. CRC is integrity checking, not
authentication or a secure-boot signature.

An attempted upload pins rescue mode even if it fails. Reboot is rejected while
an update owns the flash path. USB and I²C cannot interleave image data or flash
operations. A partial USB upload times out after 30 seconds without data, before
any flash erase. After success the bootloader stays available; the host tool
normally requests application startup explicitly.

## Flash and RAM layout

| Flash range | Purpose | Update route |
|---|---|---|
| `0x000000–0x003fff` | First-stage recovery, hardware initialization (16 KiB) | Explicit dangerous whole-flash USB write or external SPI |
| `0x004000–0x00ffff` | USB/I²C bootloader with its own MGAP header (48 KiB) | Explicit first-stage I²C maintenance |
| `0x010000–0x1fffff` | Main application with MGAP header (1,984 KiB) | Normal USB or I²C upload |

| RAM range/start | Use |
|---|---|
| `0x20000000` | First-stage runtime |
| `0x20010000` | Second-stage bootloader runtime |
| `0x20020000` | Main application runtime |
| `0x20200000` | Incoming image staging |
| `0x207ff000` | Initial runtime stack top |
| `0xe0000800–0xe000080f` | Checked one-shot warm-reset request |
| `0xe0000f00` | Early first-stage stack top |

Normal updates protect the first **64 KiB** in software. The reserved sectors
are not hardware write-protected. First-stage maintenance deliberately permits
replacing the second stage, but not the first stage. Code with arbitrary memory
access can bypass software guards; this is an owner-controlled development
loader, not a hostile-code isolation boundary.

For ongoing development, **treat both installed bootloader stages as a frozen
baseline** and put new functionality in the main application. A normal
`fwctl.py flash application.bin` operation, over USB or I²C, leaves both stages
intact. The build may regenerate all images from shared sources; only the
explicitly uploaded application package is installed. The first stage is
hash-pinned; the second stage is preserved by the normal upload boundary rather
than a build-time hash pin. Replacing it is a separate, deliberate maintenance
operation, not part of the app development cycle.

Flash erase/program code executes from RAM. A complete image is received and
validated before erasure. The application header's first 4 KiB block is committed
**last**, after other blocks. Word-for-word erase/program read-back and final
application validation must pass before success is reported. If power fails
before the header commit, the next boot rejects the app and stays in rescue.
There is no A/B application rollback slot.

The 16 KiB first-stage image is hash-pinned for this layout:

```text
SHA-256 2b68ecfc23016d708256318cff7d20338d2351d001b142743ac5b2d6f79b7995
```

`build.py` refuses to silently change that sector. This preserves the independent
recovery route while the higher-level bootloader evolves.

Normal app updates retain 64 KiB block erases (`D8h`) starting at `0x10000`.
First-stage maintenance uses 4 KiB sector erases (`20h`) strictly within
`0x4000–0xffff`, so replacing stage 2 preserves both stage 0 and the main app.
The latter path is newly validated in emulation and still needs physical testing.

## Build

From the repository root (`canon-mg2550s-firmware`):

```sh
export ARM_PREFIX=/path/to/arm-none-eabi-
python3 bootloader/build.py
```

Use **Arm GNU Toolchain 14.2.Rel1**, GCC `14.2.1 20241119`, for the pinned
first-stage reproduction. If `ARM_PREFIX` is unset, the script checks PATH and
then the existing PlatformIO toolchain directory under the user's home. Other
compiler versions may generate different first-stage bytes and fail the pin.
Flags include ARM946E-S ARM mode, freestanding C11, `-Os`, and warnings as errors.
There is no C library dependency or unresolved linker symbol.

Outputs in `bootloader/build/` (ignored by Git):

| File | Purpose |
|---|---|
| `application.bin` | Main-app MGAP package; normal host upload input |
| `bootloader.bin` | Second-stage MGAP package; explicit maintenance input |
| `stage0.bin` | Exact 16 KiB first-stage image |
| `boot_region.bin` | Complete 64 KiB reservation containing both stages |
| `factory.bin` | Full 2 MiB image for explicit whole-flash installation / migration |
| `manifest.json` | Compiler, source hashes, artifact hashes and symbols |
| `*.elf`, `*.map`, `*.dis` | Debugging, section sizes and disassembly |

The demo app uses the same USB management and I²C mailbox service, but is built
with `APPLICATION_BUILD`. It blinks the LEDs and accepts management commands;
it cannot program flash directly. Its I²C application-offer handler resets into
rescue, which performs the update.

## Routine host commands

Install Python dependencies and libusb (for example, `brew install libusb` on
macOS):

```sh
python3 -m pip install -r tools/requirements.txt
python3 tools/fwctl.py status
```

Typical USB edit/build/upload cycle:

```sh
python3 bootloader/build.py
python3 tools/fwctl.py flash bootloader/build/application.bin
```

`flash` finds the cooperative main app, asks it to reboot into persistent rescue,
uploads and verifies the new application, then asks the bootloader to start it.
It waits for the application USB identity to return. `--stay` leaves rescue
active after successful programming. A short/failed USB write is not retried
automatically because the device's consumed prefix may be unknown.

Other commands:

```sh
python3 tools/fwctl.py stay
python3 tools/fwctl.py reboot
python3 tools/fwctl.py reboot --mode bootloader
python3 tools/fwctl.py reboot --mode application
python3 tools/fwctl.py reboot --mode normal
```

- `normal`: full software restart, including the five-second boot window.
- `bootloader`: full restart into persistent rescue.
- `application`: full restart, validate the app, then skip the boot window.

Plain `reboot` defaults to `bootloader`, so the device stays ready for download.

The USB status stage must finish before a reboot is performed. This lets the
host distinguish an acknowledged command from a cable failure. The restart
branches through the reset entry and reinitializes the board; it is not a
power-domain reset. If CPU execution, board hardware or both management paths
are stuck, a manual power cycle can still be necessary.

Use `--serial SERIAL` before the subcommand to select a USB printer when more
than one is attached. Ambiguous USB matches are rejected. The demo serial is
currently `OPEN0001`; assign unique serials before using multiple boards.

## ESP32 I²C transport

The Canon MCU remains bus master. The ESP32-C3 is a slave at seven-bit address
**`0x42`**, separate from the EEPROM at `0x50–0x53`. This code never reads or
writes the EEPROM.

| ESP32-C3 | Printer |
|---|---|
| GPIO5 | SDA |
| GPIO4 | SCL |
| GND | GND |

Use the printer's normal supply, and power the ESP32 from Mac USB. During this
setup connect only SDA/SCL/GND between them; leave programmer **3.3 V feeds
disconnected** from the printer. Printer-side pull-ups are used. GPIO4/5 start
as floating inputs without internal pulls and return to that state on completion,
`FLOAT`, an error, or 120 seconds of inactivity. Reset never automatically arms
the bridge. The host also sends `FLOAT` in its cleanup path.

Build/install the C ESP-IDF bridge once:

```sh
pio run -d tools/esp32
pio run -d tools/esp32 -t upload --upload-port /dev/cu.usbmodem1101
```

PlatformIO configuration pins `espressif32@6.12.0` and the registry package
`framework-espidf@3.50500.0` (ESP-IDF 5.5.0), without a developer-specific path.
The proven reply path preloads all 32 FIFO bytes during the preceding request
write and disables clock stretching. A task-prepared, clock-stretched reply did
not work with the Canon master in earlier physical testing.

```sh
python3 tools/fwctl.py --port /dev/cu.usbmodem1101 flash bootloader/build/application.bin --transport i2c
python3 tools/fwctl.py --port /dev/cu.usbmodem1101 reboot --mode bootloader --transport i2c
python3 tools/fwctl.py --port /dev/cu.usbmodem1101 reboot --mode application --transport i2c
python3 tools/fwctl.py --port /dev/cu.usbmodem1101 float
```

The main app polls the mailbox even with printer USB disconnected. It recognizes
an armed application offer and reboots into rescue automatically. Stage 0 ignores
this offer; stage 2 receives it. The host loads the image into ESP32 flash before
arming, so printer reception does not depend on Mac serial scheduling. The
I²C-only host path does not require a successful printer USB enumeration.

The tool can restart an ESP32 left in its ROM loader using native USB-Serial/JTAG
RTS, then verifies the bridge reply. It does not erase/reflash the ESP32 implicitly.
An interrupted/disconnected host cannot guarantee immediate cleanup; the bridge's
inactivity timeout remains the fallback.

## Explicit bootloader maintenance and first installation

Routine `flash` accepts only a main-app package targeting RAM `0x20020000`.
To replace the second stage deliberately:

```sh
python3 tools/fwctl.py stage0-flash bootloader/build/bootloader.bin --sha256 EXACT_SHA256
```

This loads a distinct maintenance offer into ESP32, then asks the responsive
firmware to reboot. Stage 0 accepts that offer and writes the second-stage slot.
Packages are capped at 48 KiB. The main-app slot is preserved. If stage 2 is
already broken, add `--manual-reset`
and power-cycle the printer only after the tool reports `ARMED`. Stage-0 failure
itself still requires external SPI access.

For a blank or unbootable board, external SPI can install `factory.bin`.
The stock Canon RAM updater also accepts full-chip images through its own
protocol; that is different from `fwctl`'s source-defined USB protocol. Never use
a raw factory image as normal app-upload input. Changing layouts replaces stage
0 and moves stage 2 from `0x10000` to `0x4000`; neither ordinary `flash` nor
`stage0-flash` can do that. The earlier 128 KiB firmware has no dangerous-write
command, so this board used a temporary application linked at `0x20020000`,
packaged as version 1, to receive and write the new full image from RAM. That
one-time app is deliberately not retained. Future whole-flash replacements use
the installed bootloader's [dangerous-write command](#explicit-whole-flash-writes).

Rebuild/upload the ESP32 bridge for version-2 packages after migrating the
printer. The retained `install_legacy.py` only supports the historical OpenFW
0.1 → 128 KiB layout migration with the old first-stage hash and version-1
packages. It cannot install this 64 KiB layout.

## Wire protocol

USB development identities:

| Mode | VID:PID | Product | bcdDevice |
|---|---|---|---|
| Bootloader | `04a9:176d` | `MG2550s Bootloader` | `0200` |
| Demo main app | `04a9:12fe` | `MG2550s Demo` | `0200` |

These reuse identities from this Canon development board; they are not a new
USB-IF allocation for distributing another product. The host also checks the
`MGFW` management reply, rather than trusting VID/PID alone. Interface 1 retains
printer-class layout 7/1/2, OUT `0x01` and IN `0x82`; only OUT carries image data.
Interface 0 is an empty vendor interface. EP0 carries management requests.

Vendor requests, all device-recipient:

| bmRequestType | bRequest | wValue / wIndex | Data / behavior |
|---|---|---|---|
| `c0` | `42` INFO | `0 / 0` | 32-byte information reply |
| `40` | `40` STAY | `0 / 0` | No data; pin rescue mode |
| `40` | `41` REBOOT | mode `0..2 / 0` | No data; restart after status ACK |
| `40` | `43` BEGIN | length low 16 / high 16 | No data; accept an app on bulk OUT |
| `40` | `44` DANGEROUS_WRITE | `0020 / 0020` | No data; receive 32-byte DNGW header + exact 2 MiB raw image |

INFO is eight little-endian u32 values: `MGFW` magic (`0x5746474d`), version 2,
mode (1 rescue / 2 app), flags (bit 0 held, bit 1 update busy, bit 2 valid app,
bit 3 dangerous-write supported, bit 4 reboot blocked after destructive failure),
remaining window milliseconds, update status, app flash offset, app capacity.
App-valid is suppressed while updating. BEGIN pins rescue and reserves the
transfer. Send exactly the declared number of raw MGAP bytes to endpoint 1;
poll INFO until `C0` or an error. Normal commands never accept a flash address.

Status: `80` idle/receiving/programming, `C0` verified success, `90` invalid
image, `91` reception failure/timeout, `A1` flash ID, `A2` erase/read-back,
`A3` program/read-back, `A4` final application validation. An invalid/busy
control command is stalled. A USB reset aborts partial USB reception; it does
not cancel flash programming already underway.

MGAP is eight little-endian u32 words followed by payload: magic `0x5041474d`,
version 2, payload length, load address, entry offset 0, payload CRC32, flags 0,
header CRC32 over the first 28 bytes. Length must be nonzero and four-byte aligned.
Version 1 belongs to the earlier 128 KiB layout and is rejected before erasure.
Use `application.bin` from the build, or package an independently linked payload
with the same constraints. The included linker script rejects a BSS that reaches
the staging region.

### Explicit whole-flash writes

`fwctl.py dangerous-write` intentionally bypasses the normal app boundary. It
can replace **all 2 MiB, including both bootloader stages**, with an exact raw
image, including a stock dump. Normal `flash` and I²C maintenance retain their
existing bounds. Only the bootloader advertises this USB-only capability; the
demo app rejects the command and the host first reboots it into the bootloader.

```sh
python3 tools/fwctl.py dangerous-write bootloader/build/factory.bin --sha256 EXACT_SHA256
# After C0, explicitly restart through the newly installed reset entry:
python3 tools/fwctl.py reboot
```

The required SHA-256 selects the intended file on the host. The command refuses
anything other than exactly 2,097,152 bytes; it never pads or truncates a file.
Firmware checks transport CRCs, not an image signature or bootability. It accepts
arbitrary contents, so a successful write of invalid boot code can brick the
board. Restoration of a stock dump is exercised byte-for-byte in emulation;
the new writer has not been used to boot stock firmware on hardware. EEPROM
contents are unchanged, including any marker controlling stock recovery entry.

The bulk stream starts with eight little-endian u32 words: `DNGW` magic
(`0x57474e44`), protocol version 1, raw length `0x200000`, destination 0,
overwrite acknowledgement 1, raw-image CRC32, reserved 0, header CRC32 over the
first 28 bytes. It is followed by the raw image. All header fields, length and
payload CRC are checked before the first erase. CRC work is split into 4 KiB
chunks to keep USB status polling responsive. Unsupported/busy commands stall.

Code, stack and incoming data reside in RAM throughout programming. The writer
erases and verifies each 64 KiB sector from `0x10000` upward, programming and
checking its sixteen 4 KiB blocks. Only after all those sectors pass does it
erase `0x00000–0x0ffff`; it programs that sector's vector-containing first 4 KiB
last. A final CRC of the complete mapped flash must match before `C0` is returned.

**This is not an atomic update or a rollback mechanism.** Early interruption
preserves the original boot block, but that block might depend on data already
replaced elsewhere (especially during a layout migration). Power loss while
rewriting the boot block can remove both USB and I²C recovery and require SPI.
If programming fails while power remains on, the RAM writer stays available;
reboot and normal app writes are blocked until an explicit full-image retry
succeeds. Class reset does not clear that protection. The host never retries
or reboots automatically. Preserve power on failure and inspect `status`.

After a successful write the old RAM service remains running until an explicit
reboot. Software restart of the new open firmware is physically checked in the
[migration evidence](evidence/DANGEROUS_WRITE.md). A different firmware's reset
contract can require a manual power cycle. Cold-reset behavior of a restored
stock image is not established by the RAM writer's `C0` result.

The one-time migration app used INFO mode 3, version 2, address 0 and capacity
`0x200000`, product `MG2550s Installer`. That describes the installer interface,
not the layout previously installed in flash. It is not part of the normal
build and is deleted after successful migration, per the user's request.

I²C mailbox framing (all multibyte fields little-endian):

- Master writes `52 <offset:u32>`, then a combined pointer write `00` / 32-byte read.
- A data frame is offset + 24 bytes (tail padded `ff`) + CRC32 of the first 28 bytes.
- Offset `ffffffff` requests the header: `ffffffff`, `RSC1`, version 1, image
  file size, file CRC32, command ID, offer flag, then frame CRC32 (eight u32 words).
- Offer 1 is explicit first-stage maintenance, 2 is a main-app image, 4 is a
  command. Zero is unarmed. First stage accepts only 1; stage 2 accepts 2/4.
- Commands: 1 stay, 2 normal reboot, 3 rescue reboot, 4 app reboot.
- Master reports `44 <status:u32>`; ESP32 replies over serial with
  `DONE XX FLOATING`. It stops advertising the offer immediately on that result.

## Integrating a real main application

Link code at `0x20020000` using `example/app.ld`, put a BSS-clearing entry first,
and package it with an MGAP header. The bootloader leaves ARM supervisor mode,
interrupts masked, caches/MPU disabled, board/RAM initialized and an aligned stack.
It disconnects USB before handoff; the app initializes its own peripherals.

Keep the management handlers and poll `usb_poll()`, `mailbox_poll()` and
`service_poll()` in the application's event loop, or implement equivalent
commands in another USB stack. The demo does this in `src/service.c` under
`APPLICATION_BUILD`. A future app must preserve the warm-reset mailbox and use
the software-reset entry contract to retain automated rescue entry. An app that
hangs or omits these handlers cannot be commanded to reboot; the reset-time
bootloader remains reachable by a manual power cycle.

## Tests and evidence

After building:

```sh
python3 -m pip install -r bootloader/tests/requirements.txt
python3 bootloader/tests/validate.py
```

For the tested Linux-container path (useful when a native macOS Unicorn build
cannot execute this ARM CPU model):

```sh
docker build -t mg2550s-emu bootloader/tests
docker run --rm --network none -v "$PWD:/project:rw" mg2550s-emu bootloader/tests/validate.py
```

The self-contained tests execute the actual linked ARM instructions from reset
through USB register/DMA and I²C transactions. They cover five-second launch,
both USB identities/speeds, stay, coalesced bus events, valid/invalid uploads,
wrong destinations, partial-transfer timeout, read-back failure, header-last
commit, simulated power loss, reboot acknowledgment, application-originated
USB/I²C reboot and transport exclusion. No Canon dump is loaded. Hardware timer
rates, PHY signaling and peripheral readiness are modeled; emulation is not
electrical proof. `build/validation.json` pins the tested factory hash.

Register provenance is in [evidence/HARDWARE.md](evidence/HARDWARE.md). Current
layout design is in [evidence/LAYOUT64.md](evidence/LAYOUT64.md); installation,
whole-flash validation and remaining hardware limits are in
[evidence/DANGEROUS_WRITE.md](evidence/DANGEROUS_WRITE.md).
The earlier 128 KiB layout's live LED tests are in [evidence/LED_MODES.md](evidence/LED_MODES.md).
The original public bootloader timing and update measurements are in
[evidence/STATUS.md](evidence/STATUS.md),
separate from emulator assumptions. The counter used for the window is configured
as free-running at **96 MHz**, measured against the Mac's monotonic clock on this
board. The initial 48 MHz assumption produced a 2.5-second window and was rejected
by the physical timing test. Recheck the rate on materially different hardware.
