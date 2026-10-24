# App foundation + USB debug log — evidence

Milestone: a standalone, fwctl-flashable application with a USB debug-logging
channel, built on the frozen installed bootloader. Recorded 2026-10-23.

## Build

Compiler: Arm GNU Toolchain 14.2.Rel1 (`arm-none-eabi-gcc 14.2.1 20241119`).

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `app/build/application.bin` (MGAP v2, load `0x20020000`) | 5600 | `f9685aa83c8c73844c3e6f4ae8abef124d7a37275adba6b3e170ce426fe24b00` |

Reused bootloader leaf drivers (unchanged): `src/entry.c`, `src/runtime.c`,
`src/panel.c`, `src/integrity.c`. No boot-region binary is produced.

## Emulation (required before flashing)

Ran on a factory image = **installed** `bootloader/build/boot_region.bin`
(stage 0 + stage 2, read-only) + this app, through `bootloader/tests/model.py`
in the `canonemu` image. `app/build/validation_app.json`:

- factory SHA-256 `ea027f8c86264ceda012b3a6c241524bf30ddef9acb6be1557e76d82ee023355`
- `app_boots_and_reports_management` — identities `[0x176d, 0x12fe]`; INFO =
  MGFW, version 2, mode 2 (application), flags = valid-app only, status `0x80`,
  app flash `0x10000`, capacity `0x1f0000`.
- `usb_debug_log_drains` — the `0xC0/0x50` vendor request returns the boot log
  (`[app]`, `usb up`, `app_flash=0x00010000`).
- `app_reboots_into_held_rescue` — `REBOOT` (wValue 1) → software reset → held
  bootloader. This is the path `fwctl flash` relies on, so the app is always
  reflashable.
- The model asserts the protected prefix (`0..0x10000`) is byte-identical to the
  source image: **the app never writes the boot region.**

## Physical hardware

Flashed with `python3 tools/fwctl.py flash app/build/application.bin`
(cooperative app → rescue → program → app; bootloader untouched):

- Upload returned **status `C0`** (firmware erase/program/read-back/CRC verified).
- Rebooted to **application** mode: `app_valid=true`, `status=80`, layout 2,
  app flash `0x10000`.
- USB debug log read back over `0xC0/0x50` from the running app:

  ```
  [app] MG2550s open app
  app_flash=0x00010000
  app_ram=0x20020000
  [app] usb up, entering main loop
  ```

### Limits / not proven

- No cold power-cycle observation of this app recorded here (software reboot and
  live enumeration were verified; the power-cycle webhook is available).
- LED demo visual not re-confirmed by eye this milestone.
- The debug channel is EP0 control-IN polling (fine for text); a future
  high-rate channel (e.g. raw CIS lines) may want a bulk IN endpoint.
