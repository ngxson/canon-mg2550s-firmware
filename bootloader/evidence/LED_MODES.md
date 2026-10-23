# Rescue power LED and demo sequence — 2026-10-23

The USB bootloader now blinks **only the power LED**. The demo application
cycles all four LEDs in order and accepts a USB reboot into persistent download
mode. `python3 tools/fwctl.py reboot` now defaults to that destination; explicit
`--mode normal` and `--mode application` retain their previous behavior.

The user identified the first LED in the existing sequence as power. It is
channel 1, register `0xc0c8d148`, on value `0x10`. The other LEDs are channel 0
(`0x20`), channel 2 (`0x20`), then channel 3 (`0x10`). Rescue commits alternating
`[0, 0x10, 0, 0]` and `[0, 0, 0, 0]` frames in channel order 0–3. The existing
timer cadence and nonblocking panel-ready check are retained.

## Build and emulator verification

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `stage0.bin` | 65,536 | `1199c8e3d98ca8cc6c8e4176a296f3c223099c7203df18617127c2316b4f9e79` |
| `bootloader.bin` | 9,088 | `adeb71d609a6b9cd7dbe8eb8c78d93ceadf0863b149282b266a5a85e9adf87a4` |
| `application.bin` | 7,296 | `31e72048abfba9b781d071f85cb6eecdbd35a15037cc8ee77370c9d1e86bb9dc` |
| `factory.bin` | 2,097,152 | `1f6c13291f0d82d28daba2a08b2bdf7b854c7cb8596ba466d5ecc0ff506b6d5f` |

The [manifest](led_modes/manifest.json) pins all build inputs and the
[isolated public-source rebuild](led_modes/source_only.json) reproduces all four
images. The original first-stage panel implementation was copied unchanged to
`stage0/panel.c`; its compiled 64 KiB sector still matches the pinned hash.
This first stage retains its original sequence before the USB bootloader starts
and during explicit bootloader maintenance or second-stage failure recovery.

All **25 emulator cases passed before flashing**:
[validation results](led_modes/validation.json). The model now records committed
panel output frames by executing stage. Every bootloader frame must contain
only power-on or all-off. Full/high-speed held-rescue cases check repeated
alternation. The new demo test checks two complete four-LED sequences, sends
the USB reboot request, observes the distinct rescue identity and checks four
power-on/off cycles. Existing update, protection, timing and error cases pass.

## Physical installation and USB command

The board remained on its normal supply. Printer USB was connected to the Mac;
ESP32 GPIO5/4/GND were connected to SDA/SCL/GND, with programmer 3.3 V feeds
disconnected. No manual power cycle was needed.

- First-stage I²C maintenance installed the bootloader and returned
  `DONE C0 FLOATING`: [log](led_modes/bootloader_install.log).
- The new USB bootloader installed the demo app with `C0` and returned to its
  application interface: [log](led_modes/application_install.log).
- Plain `fwctl.py reboot` moved the running `MG2550s Demo` (`04a9:12fe`) into
  `MG2550s Bootloader` (`04a9:176d`), held with a valid app, still present after
  5.5 seconds: [USB observations](led_modes/usb_reboot.json).
- ESP32 GPIO4/5 were independently left as inputs with no internal pulls:
  [register read-back](led_modes/gpio_float.json). Its flash remains intact and
  the next I²C session can restart the bridge automatically.

The user visually confirmed the new rescue behavior: **"Yes, only power
blinks"**, in response to checking that the other three LEDs remained off. The
printer was then returned to the demo through USB:
[final state and observation](led_modes/final_state.json). The demo's four-output
sequence is verified by ARM emulation; this revision did not add a fresh human
observation of that sequence. No new physical cold-power-cycle test was performed.

To start the demo from held rescue:

```sh
python3 tools/fwctl.py reboot --mode application
```
