# Mechanical homing — 2026-10-24

**Mechanical homing is physically confirmed on the connected printer.** On
2026-10-24 the user confirmed visible movement to the left/home end: “oh yeah it
homed” and “Yes—it reached home”. This supplements the recorded position and
velocity feedback from the installed source-built app. Optical strip calibration
is not implemented. No stock executable code is linked into the app.

Colour starts HOME, B/W requests a short forward jog, and Cancel stops motion.
There is no movement on boot. USB management remains available during motion;
I²C management and long scanner acquisitions resume when idle.

## Verified homing build and physical result

This build was subsequently superseded by the [page-scan application](PAGE_SCAN.md),
which retains this homing controller.

Candidate v2: **25,344 bytes**, SHA-256
`ff46d12271607442e0ea57e6d284d465a4bbe37ed5cfa423607cf41852ff93a9`.
[Manifest and source hashes](homing_v2/manifest.json),
[guarded USB installation](homing_v2/flash.txt).
The normal app uploader returned `C0`, then valid application INFO. It writes
from `0x10000` and preserves both recovery stages. The boot-region artifact
remains `fb5b09443f5717aa81c8675db491a6b86db9f471130506768979a4b9726d9d8e`.
No ESP32 access or power cycle was used for these homing candidates.

The [physical run](homing_v2/physical_home.json) and
[device log](homing_v2/physical_home.log) record:

| Observation | Value |
|---|---:|
| Initial relative coordinate | 7,340,032 |
| First stopped coordinate, after settling | 6,928,955 |
| Backoff endpoint, after settling | 6,939,548 |
| Verified positive backoff displacement | 10,593 counts |
| Repeated negative seek's stopped coordinate | 6,928,967 |
| Difference between stopped coordinates | 12 counts |
| Host duration of whole operation | 18.109 s |
| Final result / phase | 2 (mechanical reference) / 0 (idle) |

During the first seek, position decreased consistently and period-derived speed
was near the requested 28,000 counts/s. Backoff produced positive displacement;
the second negative seek stopped close to the first. Firmware disabled drive
before reporting the result. USB INFO still reported a valid running application.
These are measurements from the board, not emulator inputs. The user's
subsequent visual confirmation establishes that the scanner reached the home
end in this test. That observation is recorded separately from the device's
samples in the physical-run JSON; it is not inferred from the counter.

## Timer fault found on the first physical run

V1 was 25,216 bytes, SHA-256
`18a6c5f8c677aea6dbe00e6766cb16da232f5e4ffc709a1b7d207b88b0be8b36`.
Its [first physical attempt](homing_v1/physical_home.json) reported sustained
negative travel, but ~0.8 host seconds became 39,918 firmware milliseconds and
triggered the seek deadline. Drive stopped and management remained available.
The discontinuity corresponds to treating a 29-bit rollover as a 32-bit rollover:
`(2^32 - 2^29) / 96000 = 39146.837 ms` of spurious elapsed time.

V2 accumulates low-29-bit deltas in `app_clock_ticks()`. The shared runtime source
uses this only under `APPLICATION_BUILD`; bootloader compilation is unchanged.
Callers must sample more often than 5.59 seconds; the application's normal loop
and all motor/control waits do so. The software tick accumulator is 32-bit, so
phase deadlines use unsigned subtraction and remain shorter than its 44.74 s
wrap interval. `time_ms()` additionally accumulates milliseconds and fractions.

Before commanding motion with v2, a [12-second read-only clock measurement](homing_v2/physical_clock.json)
observed two raw wraps about 5.59 s apart. Extended deltas exactly matched raw
low-29-bit deltas; measured rate was **95,998,678 ticks/s** against host monotonic
time. The corrected clock passed across the wraps without a false deadline.
The successful subsequent homing run crossed multiple raw wraps as well.

## Source and stock evidence

- `src/motor_control.c`: source transcription of stock `f76ce` acceleration,
  `f8682` speed PI, and `f8772..f87ce` anti-windup, clamps and command reflection.
  Seek uses 400→28,000 counts/s over 50 ms, Kp 315/1000, Ki 500/10,
  normalized command 3500..5000. Backoff uses 1200→20,000 over 70 ms,
  Ki 1000/10, command 0..5000. Kd is zero in both profiles.
- `src/scanner_motor.c`: stock-derived shared setup and `f8a5c` hardware arming,
  a polled period/direction velocity observer based on `f9130`, and a new bounded
  homing state machine. Negative seek follows the stock `f4c56` mechanical
  sequence; a failed first seek is not automatically accepted as a reference.
- Initial seek spans 529,134 counts, with a 22 s limit. A 500 ms progress window
  detects the provisional stall. The app stops, settles 100 ms, requests a
  positive 10,582-count backoff, verifies travel and endpoint, then repeats a
  negative seek. Backoff and repeat seek each have 2.5 s limits. The repeated
  stopping coordinate must be within 3,000 counts of the first.
- The repeat seek and explicit deadlines are replacement-firmware checks, not
  a claim that the complete stock homing algorithm was copied. Backoff reduces
  demand to 4,000 counts/s in its last 3,000 counts. Stock instead continues to
  optical processing (`f4c2c` / `f4bec`), which this app does not implement.
- Faults, Cancel, overlapping requests and reboot disable drive. Serial setup
  waits are bounded and occur with motor outputs disabled. Other active motors
  cause shared setup to be refused. Deadlines depend on cooperative polling;
  they are not independent hardware cutoffs.

The private stock-analysis source is the unchanged dump SHA-256
`4b032367cff5976aeb426c8723e08d3c431a38e622e245a614791a3e5067e533`.
Workspace analysis is in `fw_dump/gpt/HOMING_RECHECK.md` and
`fw_dump/gpt/home_recheck_baseline.json`. Stock is an analysis/test oracle only;
it is not required to build the application and was not flashed to the board.

## Validation before installing v2

- [Controller differential execution](homing_v2/validation_home_controller.json):
  **2,522 comparisons** of actual replacement ARM code and unchanged stock
  instructions. Includes 122 acceleration points and 2,400 PI steps across
  both profiles, both directions, and frozen/moving/overspeed-then-stop inputs.
  Checks internal integral, correction, command, anti-windup and C+8 output.
- [Setup audit](homing_v2/validation_home_setup.json): all **37 stock-written
  scanner C/P fields** match at first arm. The report also exposes wider
  timer-clock, disabled-other-motor and AFE differences. Equality of selected
  last-written values is not proof of complete silicon state equivalence.
- [Homing and fault tests](homing_v2/validation_home.json): actual application
  behind the production bootloader, with explicitly scripted position feedback.
  Includes normal and already-at-end sequences, frozen feedback, stuck backoff,
  wrong direction/return endpoint, false completion, raw timer rollover,
  independent deadline, missed tick, serial failure, Cancel, USB STOP, live USB
  INFO/reboot and I²C rescue after failure. This is not a physical motor model.
- [Short-jog checks](homing_v2/validation_motor.json) and
  [app/management checks](homing_v2/validation_app.json) pass. Button and scanner
  validation scripts also passed on this same binary before installation.
  Protected flash prefix checks remain in the emulator tests.

All four earlier tiny-jog variants failed the user's visible-motion test:
[history](TINY_JOG.md). The user requested a working backward/home operation,
not restoration of the original forward-only app. The archived baseline
reconstruction was never installed. This implementation replaces fixed pulse
guesses with the stock-derived feedback controller and a verified sequence;
the contribution of each individual setup change has not been isolated on
hardware.
