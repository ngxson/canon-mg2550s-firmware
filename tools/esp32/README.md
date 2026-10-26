# ESP32 bridge wiring detection

GPIO4 and GPIO5 can be connected to printer SDA/SCL in either order. GND must
still be connected correctly. Every ESP32 boot discards the previous mapping
and probes SDA5/SCL4, then SDA4/SCL5, for up to three seconds each. Both pins are
released between attempts and after detection. The bridge uses open-drain SDA,
input-only SCL, no internal pulls, and only slave address `0x42`.

The automatic probe advertises a zero-length **command-zero** offer, never an
image, reboot or stay command. The current open application and second-stage
loader reject that unsupported command with `90`, without a reset, flash write
or boot-window change. Detection requires an exact five-byte header request,
a subsequent read and that rejection. Noise, GPIO activity alone, an ACK alone,
or an unsolicited result is insufficient.

The independent first stage has no command handler. For recovery when stage 2
is broken, two exact header-request/read pairs instead establish the pin mapping
after the three-second observation window. The result reports the evidence:

- `WIRING VERIFIED SDA5 SCL4 PROOF=REJECT90 FLOATING`: request, CRC-checked reply,
  and rejection received from the cooperative printer firmware.
- `WIRING VERIFIED SDA4 SCL5 PROOF=HEADER_READ FLOATING`: repeated request/read
  traffic establishes routing, but does **not** prove response CRC acceptance,
  image transport or flash programming. This does not physically validate the
  first-stage recovery writer.
- `WIRING FAILED ... PROOF=NONE FLOATING`: neither orientation was proven; the
  shown pin numbers are only the last attempt, **not** detected wiring.

`ARM`, `COMMAND`, `LOAD`, and `LOADBOOT` refuse an unverified mapping. Nothing is
automatically uploaded after reconnecting. `FLOAT` cancels detection and clears
its proof. `DETECT` starts a fresh check, without exposing any staged image.
The host calls it again before every image load or management command, so moving
wires while the ESP remains powered does not silently reuse the host session's
old selection. Do not move wires during an armed transfer.

```sh
python3 tools/fwctl.py --port /dev/cu.usbmodem1201 detect
pio run -d tools/esp32
```

Detection needs polling from the open printer firmware. Silence cannot
distinguish a powered-off/busy/non-polling printer, broken ground or signal wire,
wrong connection point, or a transport fault. It is never treated as success.
The firmware does not scan EEPROM addresses or transmit master transactions.

Validation: `tests/wiring_test.c` exercises the shared C detector with malformed
and reordered events under native sanitizers; `tests/host_test.py` checks the
host write/reboot gates; `tests/probe_printer.py` executes command zero in the
ARM printer emulator. The first two are simulations, not ESP32 peripheral
emulation. Physical outcomes are recorded separately in `evidence/`.

## Passive I²C sniffing (added 2026-10-26)

`SNIFF` puts the bridge into a passive bus monitor on the verified (or
default SDA5/SCL4) orientation: both pins stay inputs with pulls disabled and
nothing is ever driven or acknowledged. A dedicated task polls the GPIO input
register in 4 ms bursts with interrupts masked, so 100–400 kHz edges are not
missed; the decoder prints one line per transaction:

```
I2C <us since boot> S <addr+rw><a|n> <byte><a|n> ... P|Sr
```

`STOP` ends the capture and reports `FRAMES`, `BYTES` and `DROP`. The mode
is refused while the slave is armed or probing. It was used to capture the
printer's boot-time 24C08 traffic
([evidence](../../emu/evidence/stockapp/telemetry/engine/i2c_sniff_power_cycle.json)):
the reset-time recovery loader's polls of the disarmed bridge appear as NAKed
address `0x84`, followed by stock's EEPROM reads and counter writes.
