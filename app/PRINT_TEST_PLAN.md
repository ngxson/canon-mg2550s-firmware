# Physical test plan: print carriage, paper feed and head controller

Scope: bring the printing mechanism under the open application in small,
observable, abortable steps. CR is the print carriage motor, LF the paper feed
motor; neither is the scanner carriage. Register and API facts below come from
the traced stock behaviour summarised in `fw_dump/PROGRESS.md`, section 1i.
Everything marked *physical* has not been observed on hardware yet.

## Ground rules

- Emulate every new firmware step with a scripted model before flashing, as for
  the scanner. Emulation never proves motion, direction or electrical safety.
- Power-cycle the printer before and after every motor test. Motor blocks keep
  state across software resets (seen with the scanner motor).
- Every drive is bounded in firmware: fixed maximum duration, neutral command
  and disable on exit, Cancel key and USB stop (0x76) honoured, management polled.
- Start every actuation at the lowest stock-observed power and the shortest
  duration. Only increase one parameter at a time.
- Head heaters are never pulsed until the drive-pulse contract (voltage enable,
  pulse width, nozzle serializer protocol) is established. Firing a thermal
  inkjet heater with the wrong pulse destroys it.
- Keep uncapped time short. The carriage parks on the right over the cap station;
  every CR test that leaves the park position dries the nozzles until it returns.
- Both cartridges stay installed. The head connector carries the fuse ROM, the
  connection-response loop and the ADC thermistor lines used below.

## Observation setup

- Scanner lid open, front cover open enough to see the carriage and the encoder
  strip. Phone video at 60 fps or more for motion direction and distance.
- Ruler or tape on the chassis as a position reference for the carriage.
- `app/tools/fwlog.py` running in one terminal for the firmware log.
- Paper: a few sheets in the rear tray for LF tests, none for CR tests.
- Abort: USB request 0x76 (`MOTOR_STOP`) and the panel Cancel key both must stop
  any new drive; verify this on the first short pulse before any longer run.

## Register map used by the plan

| Item | CR (carriage) | LF (paper feed) |
|---|---|---|
| Companion drive block | `0xc0c50000` | `0xc0c52000` |
| Primary block | `0xc0c51000` | `0xc0c53000` |
| Enable | `50000 = 0x102` (0 disables) | `52000 = 0x102` (0 disables) |
| Period | `50004` (stock API writes `x<<2`) | `52004` |
| Drive command | `50008`, neutral 2000 | `52008`, neutral 2000, minimum 4 |
| Position counter | `51100` | `53100` |
| Event mask / status | `51400 / 51404` | `53400 / 53404`; companion `52100 / 52104` |
| Period capture | not mapped | `53200..0c`, reset through `53210` |
| Stock fixed-PWM 20 % | `50008 = 2400` or `1600` | `0xd8010(q)`: `52008 = 40*q`, `q=47..53` |

The scanner motor uses the same family (companion `0xc0c56000`, primary
`0xc0c57000`) with the same enable word and neutral value, so the scanner's
C driver (`motor_control.c`, `scanner_motor.c`) is the template. The software
APIs differ (enable argument polarity, period scaling), so copy the register
behaviour, not the stock argument conventions.

## Stage A: passive reads, no actuation (zero risk)

Firmware: a USB request that returns, without writing anything: `50000`,
`50004`, `50008`, `51100`, `51400`, `51404`, `52000`, `52004`, `52008`,
`53100`, `53400`, `53404`, `52100`, `52104`, and the four `53200..0c` words.

1. Read at rest after power-on. Record the counters; expect stable values on
   repeated reads.
2. Power off. Push the carriage by hand about 20 mm to the left, then power on
   and read again. Repeat with 20 mm to the right. Result: whether `51100`
   counts at all without drive, its sign per physical direction and a first
   counts-per-mm estimate. With power on and the driver disabled (never
   enabled by our firmware), gentle hand movement is also acceptable and gives
   live readings.
3. Turn the paper feed roller by hand if it is reachable (through the rear tray
   or by pulling a sheet through gently). Watch `53100`.

Pass: counters change in a consistent direction and return near the start value
when moved back. Fail/unknown: counters stay fixed. Then the encoder input is
gated by something the stock init sets up, and Stage B needs the stock init
words first.

## Stage B: CR open-loop pulses

Firmware: bounded carriage jog mirroring the scanner jog. Sequence per pulse:
disable, write period and the stock init words (`primary control 0x0d`,
`P+4 = 0`, `P+8 = 1200`, position `0xf0000`, feedback `0xfffff`, event mask 0;
the exact register offsets of these five words need confirmation from the
trace before use), command 2000, enable `0x102`, command `2000 ± delta` for
`T` milliseconds while sampling `51100` every millisecond, command 2000,
delay, disable. Hard deadline 100 ms regardless of host parameters.

1. `delta = 400` (stock 20 %), `T = 20 ms`, direction sign chosen so the first
   pulse should move away from the right wall. Observe: carriage moves, log
   shows counter delta and sign. If the counter does not change but the motor
   hums, suspect a park lock or the cap cam: do not raise power above 30 %
   (`delta = 600`); report instead.
2. Opposite sign, same parameters. Confirms both directions and the mapping
   between command sign, counter sign and physical direction.
3. Cancel test: `T = 80 ms` and press Cancel or send 0x76 during the pulse.
4. Return to park with short pulses only. Verify a hand-pushed carriage returns
   to the capped position mechanically (it should click onto the cap).

Record: sign table (command above or below 2000 → left/right → counter up/down),
counts per pulse, coasting distance after neutral, time to visible motion.

## Stage C: CR closed-loop moves

Firmware: PI position controller from `motor_control.c` with CR parameters,
soft travel limits derived from Stage A/B, 1 ms tick, stall detection (no
counter change for 50 ms at non-neutral command → neutral and disable).

1. Move 10 mm left, hold, return. Then 50 mm. Then full width minus 10 mm.
2. Measure counts per millimetre with the ruler; derive the encoder pitch.
3. Verify repeatability: five round trips, final counter within a few counts.
4. Record maximum stable speed with the 20 % to 50 % command range.

Pass: moves land within a few counts, no stall detections, carriage re-caps.

## Stage D: LF open-loop pulses

Caution: on this class of Canon mechanism the paper feed motor also drives the
purge pump through a clutch selected by carriage position. Run LF tests with
the carriage moved off the cap by a Stage B/C move, so the pump cannot engage,
and keep the test short so the heads are not uncapped for long. If paper
feeds in one direction and nothing visible happens in the other, that other
direction may be the pump; stop and report.

Firmware: same bounded jog for `52000/52004/52008/53100`, neutral 2000, stock
low commands first.

1. `52008 = 2120` (`q = 53`) for 50 ms, then `1880` (`q = 47`). Observe the
   feed roller and `53100`.
2. With one sheet loaded, repeat the direction that turned the roller toward the
   output: the sheet should advance a few millimetres. Measure millimetres per
   count.
3. Period capture: reset `53210`, read `53200..0c` after a pulse; the stock
   getter sums four samples. Compare with the counter rate.
4. Cancel test as in Stage B.

## Stage E: head controller, read-only first

E1. Cartridge fuse ROM (implemented: USB 0x62/0x63, `app/tools/cartridge.py`).
Result decides whether the ASIC head block answers without the stock "mode 4"
head power state machine. If both heads return complemented zero (sense line
stuck high), the power/mux acquire path is required before any other head test.

E2. Cartridge connection response (stock `0xde4f8`): GPIO loop through the head
connector. Writes outputs `0xc0c85c24/34/44/54/64`, `0xc0c85e04/14`,
`0xc0c81608/48`, bank masks `0xc0c81584/815c4/81604/81644`, expects inputs
`0xc0c84638/48` to follow the three-phase pattern (1,1 → 0,0 → 1,1). Low risk:
logic-level signals only. Result: per-head connection good/bad, same as stock
error `0x0b` detection.

E3. Head thermistor ADC (stock `0xe8ce6`): byte writes `0xc0c90000 = channel`,
`0xc0c90004 = 2`, `0xc0c90000 = channel | 0x80`; halfword reads at
`0xc0c90030` (black, channel `0x12`) and `0xc0c90028` (colour, `0x11`); 16
samples averaged. Read-only. Record raw means at room temperature; warm a
cartridge slightly by hand and read again to confirm the channel assignment
and sign. No conversion to degrees is claimed.

E4. Head power rail observation (needs a multimeter, user action): identify the
head supply on the carriage flex and record whether it is present at idle under
the open firmware and after E1 to E3. Do not probe while the stock firmware runs
(it is not installed).

## Stage F: head drive (blocked until the contract is known)

Not to be executed with the current knowledge. Prerequisites, each with its own
emulator model before any hardware attempt:

1. The head power acquire/release sequence (stock mode 4) and its timer.
2. The nozzle serializer protocol in blocks `0xc0c27xxx/28xxx/29xxx`, the
   raster-to-print-buffer converter at `0xc0c25xxx` and their IRQs (`0x2f`).
3. Heater pulse width/voltage enable and the temperature protection thresholds.
4. CR encoder to head staging coupling (`0xc0c51118`, CR event bit 0).

First actuation after that: a single nozzle column fired once over the
maintenance absorber (carriage at the far left spit position), not over paper,
with the head thermistor read before and after.

## Firmware deliverables implied

| Request | Purpose | Stage |
|---|---|---|
| `PRINT_REGS` IN | dump the CR/LF registers above | A |
| `CR_JOG` OUT | bounded CR pulse: sign, delta, duration | B |
| `LF_JOG` OUT | bounded LF pulse | D |
| `CR_MOVE` OUT / `CR_STATUS` IN | closed-loop CR move and controller state | C |
| `HEAD_CONN` OUT, `HEAD_ADC` IN | connection response, thermistor means | E2, E3 |

Each gets a scripted emulator model (counter follows the command, completion
bits, stall case) and a host tool that saves a JSON report plus the firmware
log under `app/evidence/`.

## Open questions for the owner

- Is opening the front cover acceptable for observation, and can a camera be
  placed to see the carriage and the encoder strip?
- Is there a visible or audible carriage lock at the park position when the
  carriage is pushed by hand with power off?
- Is a short period with uncapped heads (minutes, not hours) acceptable for
  the CR/LF stages?
- Does a multimeter exist for Stage E4?
