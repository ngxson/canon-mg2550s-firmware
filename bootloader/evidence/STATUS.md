# Public bootloader verification — 2026-10-23

This records the initial public bootloader revision. The subsequent
[LED-mode revision](LED_MODES.md) has its own image hashes and validation results.

The public bootloader and demo application are installed on the development
MG2550s. Normal USB and I²C application uploads, timed startup, persistent rescue
and explicit reboot commands passed physical testing. **No manual printer power
cycle was used for this migration or these tests.** The printer was left running
the demo application. ESP32 GPIO4/5 were left register-verified floating.

## Exact final build

Built with Arm GNU Toolchain 14.2.Rel1 (`14.2.1 20241119`). The
[manifest](verified/manifest.json) records every build input, linked symbols and
artifact hashes. [An isolated rebuild](verified/source_only.json) from a copy of
the public source tree reproduced all four images without a Canon dump or
private workspace input.

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `stage0.bin` | 65,536 | `1199c8e3d98ca8cc6c8e4176a296f3c223099c7203df18617127c2316b4f9e79` |
| `bootloader.bin` | 9,152 | `7c2ab312082bb45341e32134cbd811bd6d843bdd3481415ad849c2eee509ca6a` |
| `application.bin` | 7,296 | `999051199e6cd958857deef984c5f6da58ae3467a456ae5b1e127bde2062454e` |
| `factory.bin` | 2,097,152 | `a0de8248e5f0e85e41dc74a67899fce0c71ab5fbdb3d1368a8d70319e8623e05` |

Build products are generated locally and ignored by Git; the evidence records
are retained. The final bootloader and app were installed separately using the
operations below. No external SPI read-back of the complete final chip was taken.

## Emulator result

All **24 cases** in [validation.json](verified/validation.json) passed against the
final factory hash before its corrected timing code was installed. The model
executes linked ARM instructions from reset, using modeled USB DMA, I²C, SPI and
timers. It observes LED frames and verifies that normal updates do not change
the first 128 KiB. Cases include both USB speeds, coalesced bus events, five-second
handoff, invalid/missing app, STAY, USB and I²C updates, CRC/destination rejection,
timeout, transport exclusion, read-back failure, simulated power loss and reboot
acknowledgment. These are behavioral tests, not measurements of electrical timing.

## Physical results

The printer used its normal supply. Printer USB and ESP32 native USB were
connected to the Mac; the ESP32 connected to the printer only through SDA GPIO5,
SCL GPIO4 and GND. Both programmer 3.3 V feeds remained disconnected.

| Operation | Observed result | Evidence |
|---|---|---|
| Update the second-stage bootloader via first-stage I²C maintenance | Software reboot, `DONE C0 FLOATING`, new bootloader responsive; no manual reset | [Maintenance log](verified/maintenance_96mhz.log) |
| Upload final app over the new USB protocol | Bootloader reports `C0`, host requests reboot, application returns valid | [USB install](verified/usb_application_install.log) |
| Normal software reboot with no stay request | Countdown expires and application appears automatically | [Timing samples](verified/hardware_boot_cycle.json) |
| Rescue-directed USB reboot | Held rescue remains after 5.6 seconds; explicit app reboot works | [Boot cycle](verified/hardware_boot_cycle.json) |
| STAY sent during an initially unheld boot window | Still in held rescue after 5.5 seconds; distinct USB identities observed | [Identity/STAY test](verified/hardware_usb_identity.json) |
| Upload app over I²C while app is running | App resets itself, stage 2 updates with `C0`, I²C app reboot returns `C0` | [I²C install](verified/i2c_application_install.log) |
| I²C reboot commands | App → held bootloader → app, with no USB reboot command | [I²C commands](verified/hardware_i2c_commands.json) |
| ESP32 cleanup | Output-enable register zero; GPIO4/5 input, no pull-up/down; zero EEPROM/flash writes | [Register read-back](verified/final_gpio.json) |

The I²C tests used USB only to observe the printer's resulting identity/status;
they did not physically unplug that cable. The emulator separately covers the
I²C flow without a USB host. The new public build was not manually cold-cycled
in this run. Its frozen first stage had already been physically cold-booted in
the preceding milestone. LED programming is retained and exercised in the
emulator; no fresh human observation of LED colors/positions was recorded here.

USB bootloader identity is `04a9:176d`, **MG2550s Bootloader**. The app is
`04a9:12fe`, **MG2550s Demo**. Both report bcdDevice `0200`, manufacturer `OpenFW`
and serial `OPEN0001`.

### Counter calibration

The initial 48 MHz conversion was wrong: the countdown advanced approximately
2,000 reported milliseconds per host second, making the intended five-second
window last about 2.5 seconds. The
[rejected timing observation](verified/timing_initial_rejected.json) is retained.

With the corrected **96 MHz** conversion, the final test measured
**999.956 countdown milliseconds per host second**. The first readable INFO
reply had 4,750 ms remaining. The last bootloader sample had 125 ms remaining;
the app was visible 5.270 seconds after the first sample. Enumeration and the
100 ms disconnect interval account for time outside the remaining countdown.
This is calibration against host monotonic time, not a direct counter-clock
frequency measurement with an oscilloscope.

### Initial migration is a different image

The one-time legacy USB migration installed an earlier public build whose factory
SHA was `0b733c4e6a769946178ddbd7752edb4da0701b731f452224e34d1e04fd7fda76`.
It sent 4,718,676 encoded bytes and reported `C0`:
[physical result](verified/legacy_install_initial.json),
[migration emulation](verified/legacy_migration_emulation_initial.json).
The unchanged first stage then installed the final 96 MHz bootloader through
the explicit I²C maintenance route, and the new USB route installed the final
app. The legacy migration record must not be mistaken for a test of the final
factory hash.

## ESP32 bridge

The public C bridge was built and uploaded with flash hash verification:
[upload log](verified/esp32_upload.log),
[source/artifact manifest](verified/esp32_manifest.json).
Its completion messages reported `FLOATING` after every transfer and command.
The independent final ROM check left the ESP32 in ROM with its flash intact;
the next host I²C session can restart the installed bridge using RTS.

## Remaining limits

Software reboot requires the running app to service USB or the I²C mailbox. A
CPU hang still needs a manual reset. A damaged second-stage bootloader can be
replaced by first-stage I²C recovery; damage to the first stage still needs SPI.
The application has no rollback slot. CRC/read-back detects corruption and
incomplete writes but is not image authentication. Flash power-loss behavior
and read-back errors were injected in the emulator, not induced physically.
Large application throughput and other board revisions remain unmeasured.
