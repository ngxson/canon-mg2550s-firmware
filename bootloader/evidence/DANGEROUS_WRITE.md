# Whole-flash USB writer and 64 KiB migration — 2026-10-23

**Installed and verified on the development printer.** The old 128 KiB
bootloader loaded a temporary version-1 application over USB. That app received
the complete new image, rewrote and verified all 2 MiB from RAM, and returned
`C0`. USB commands then restarted the printer into the new 64 KiB bootloader
and the demo application. No SPI connection or manual power cycle was used.

The user requested this order: add dangerous writes to the bootloader, compile
it, make a temporary installer app, then install the new bootloader. The
temporary source, binaries, objects and test script were deleted after success.
Only hashes and emulator/hardware reports remain. The new full image also
overwrote the old app slot containing the installer.

## Build identity

The permanent bootloader and demo remain source-built C, with the existing
isolated CPU/reset assembly. No stock executable bytes are production inputs.
The pinned 16 KiB first stage is unchanged from the layout-2 candidate.

[Manifest, symbols and source hashes](dangerous_write/manifest.json):

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `stage0.bin` | 16,384 | `2b68ecfc23016d708256318cff7d20338d2351d001b142743ac5b2d6f79b7995` |
| `bootloader.bin` | 10,080 | `6668d05064181aca880c4ed3cc9d6233a24702368ed2ca04fbbf5576216701a8` |
| `boot_region.bin` | 65,536 | `fb5b09443f5717aa81c8675db491a6b86db9f471130506768979a4b9726d9d8e` |
| `application.bin` | 7,392 | `455843d98ff329630b6ac623f0003b4f51578bbf49e5dbee49d95dfd7204b492` |
| `factory.bin` | 2,097,152 | `130fa197cd37b9e456737796b8600f2d2bde0f7cc2b4db2b112d33e27c0ca453` |

The deleted installer was a 9,792-byte version-1 MGAP package linked at
`0x20020000`, SHA-256
`66ce5f02e258ffde4c54247ed17efe97585c9be1fa5ebd3368c731654d8c7be4`.
It used the new RAM updater code, a permanently held service, a distinct INFO
mode/product, and rejected ordinary application uploads. It was uploaded to the
old app offset `0x20000`; its execution/staging RAM was outside all flash being
erased. [Installer hashes and symbols](dangerous_write/installer_manifest.json).

## Emulation before hardware

All **31 baseline cases** passed:
[validation.json](dangerous_write/validation.json).
All **22 dangerous-write cases** passed:
[dangerous_validation.json](dangerous_write/dangerous_validation.json).
Seven [host checks](dangerous_write/host_validation.json) also confirm that
wrong sizes/hashes are rejected before USB access, unsupported/busy devices
receive no write command, and the new status flags are decoded correctly.

The new cases execute linked ARM instructions and modeled USB/SPI/DMA. They
cover high/full-speed whole-flash writes and USB restart, all constrained header
fields, incorrect lengths and CRCs, partial upload timeout, rejection in the
demo, destructive read-back failure, reboot/normal-write inhibition after that
failure, explicit retry from RAM, and early/late power interruption. In the late
interruption test the vector page is erased, explicitly demonstrating the limit
of recovery during boot-block replacement. The optional stock-dump case verifies
byte-exact restoration, without claiming that stock code was then booted.

Run the permanent cases after building:

```sh
python3 bootloader/tests/validate.py
python3 bootloader/tests/validate_dangerous.py
# Optional local restoration fixture; not needed for a source-only build:
python3 bootloader/tests/validate_dangerous.py --stock /path/to/original-2MiB-dump.bin
```

The [exact migration simulation](dangerous_write/migration_validation.json)
also executed the archived, physically installed 128 KiB image. Its actual USB
updater accepted the temporary app while preserving the original first 128 KiB.
The CPU then rebooted into the installer, received the exact new factory image,
verified flash equality, and restarted into both the new bootloader and app.
There were no substituted firmware functions. Peripheral readiness, signaling,
timing and completion are modeled, as in the baseline tests.

## Physical sequence

1. [Temporary app upload](dangerous_write/installer_physical.json): old updater
   reported `C0`; USB reboot enumerated `MG2550s Installer`, held, mode 3,
   dangerous-write supported. Old bootloader bytes were not an upload target.
2. [Full flash](dangerous_write/physical_flash.json): 2,097,184 bytes of header
   plus raw image sent. Status moved from receiving/checking to destructive
   programming with reboot blocked, then `C0`, idle, reboot unblocked. Firmware
   performed exact per-block comparisons and a final whole-image CRC.
3. [Post-install verification](dangerous_write/hardware_status.json): USB reboot
   reached `MG2550s Bootloader`, `04a9:176d`, layout 2, app offset `0x10000`,
   capacity 2,031,616, dangerous-write capability present. A zero-length
   dangerous request stalled without starting a transfer. Held rescue remained
   active for more than 5.5 seconds.
4. A normal software reboot reported a 4,764 ms remaining window when first
   queried; subsequent samples decreased by 201 ms per approximately 201 ms of
   host time. It then enumerated `MG2550s Demo`, `04a9:12fe`, valid layout-2 app,
   no dangerous-write capability. The printer was left in the demo.
5. [Deletion record](dangerous_write/installer_deleted.json): temporary app
   source and all generated artifacts removed only after the checks succeeded.

The ESP32 was not accessed or reflashed for this migration. Its previously
verified floating GPIO4/5 state was not changed; no EEPROM write was performed.

## Behavior and limits

Normal USB/I²C app updates still guard the entire first 64 KiB. First-stage
maintenance still guards its `0x4000–0xffff` stage-2 slot. Whole-chip writes
require the separate USB request `44`, capability advertisement, a strict DNGW
header, and complete-image validation before erasure. The host requires the
exact raw image SHA-256; CRCs provide transport/read-back integrity, not image
authentication. See [the protocol](../README.md#explicit-whole-flash-writes).

Writes proceed sector-by-sector from `0x10000` through `0x1fffff`, then replace
sector zero, programming its vector-containing first 4 KiB last. All writer
instructions, stack and data stay in RAM. Keeping the old boot block until late
reduces exposure but does not provide atomic migration: old boot code may depend
on already-replaced sectors. Loss of power during boot replacement can require
external SPI. With power intact after failure, reboot is blocked and the RAM
writer remains available for an explicit retry. No automatic retry or reset is
performed by `dangerous-write`.

Cold power-cycle behavior of this revision has not been newly observed; the
verified resets here are software restarts through the reset entry. The LED
code retains the previously hardware-confirmed power-only rescue blink and
four-LED demo sequence; there was no new user visual observation in this run.
The layout-2 first-stage `20h` 4 KiB maintenance erase and the rebuilt version-2
ESP32 bridge remain emulation/build verified, not newly physically tested.
The ESP32 still needs its version-2 bridge build before new-layout I²C uploads.
Stock restoration is emulated, not performed on hardware by this command.
