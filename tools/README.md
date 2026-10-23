# Host tools

See [the bootloader guide](../bootloader/README.md) for wiring, build outputs,
protocol details and recovery limits.

- `fwctl.py`: normal application upload, status, stay and reboot over USB or an
  ESP32 I²C bridge; explicit `stage0-flash` for second-stage maintenance and
  USB `dangerous-write` for complete raw flash replacement, including stage 0.
- `install_legacy.py`: historical OpenFW 0.1 → 128 KiB layout migration only;
  it cannot install the current 64 KiB layout.
- `esp32/`: C bridge firmware, default/finished state GPIO4/5 floating.
- `float_esp32.py`: optional independent ROM register check of floating GPIO4/5;
  requires esptool 4.x or PlatformIO's `tool-esptoolpy` package.
- `requirements.txt`: Python host dependencies. USB also requires libusb.

Commands are run from the repository root:

```sh
python3 tools/fwctl.py status
python3 tools/fwctl.py flash bootloader/build/application.bin
python3 tools/fwctl.py flash bootloader/build/application.bin --transport i2c
python3 tools/fwctl.py reboot
python3 tools/fwctl.py reboot --mode application
```

Global options such as `--port` and `--serial` go before the subcommand. Normal
uploads accept MGAP main applications, never raw full-chip dumps. `--stay` keeps
rescue mode after uploading. I²C commands always request floating pins on exit.
Current uploads require version-2 packages and the 64 KiB layout. Status/reboot
also recognizes the older 128 KiB layout. Migration needs a whole-chip writer;
the old bootloader has no dangerous-write command. The development board used
a one-time RAM installer; see [migration details](../bootloader/evidence/DANGEROUS_WRITE.md).
Plain `reboot` sends the USB command to enter persistent download mode; use
`--mode normal` for a restart with the five-second window and automatic app boot.

For deliberate whole-flash replacement (including restoration of a stock dump):

```sh
python3 tools/fwctl.py dangerous-write path/to/full-2MiB-image.bin --sha256 EXACT_SHA256 --report result.json
python3 tools/fwctl.py reboot --mode normal
```

The second command is separate: the writer never reboots automatically. It
requires the exact raw image size and SHA-256, then checks the device's capability.
It receives and validates everything before erasing, commits the boot block last,
and reports `C0` only after read-back verification. It does not check whether the
image will boot. A power interruption during boot-block programming can require
external SPI recovery. On an error, keep power on: the RAM writer can accept an
explicit retry, and firmware blocks reboot after a destructive failure. See the
[wire protocol and limits](../bootloader/README.md#explicit-whole-flash-writes).

For register-verified floating pins without any I²C transaction:

```sh
python3 tools/float_esp32.py --report bootloader/build/floating_pins.json
```

This leaves the ESP32 in its ROM loader without changing its flash. The next
`fwctl.py` I²C command can restart the bridge automatically. Use a new report
filename for each check; existing evidence is not overwritten.
