# Print mechanism Stage A: passive CR/LF register observation (2026-10-24)

Module `src/print_mech.c`; tool `tools/print_regs.py`; test
`tests/validate_printregs.py` (emulation only checks layout and that no motor
enable register is ever written non-zero).

## Requests

- `0x64` IN: read-only dump of 30 words (CR/LF drive, primary control,
  position, event and period-capture registers, scanner enable/position).
- `0x65` OUT: motor-off counter preparation. Variant bits: 1 CR primary init
  words (control `0x0d`, `P+4=0`, `P+8=1200`, position `0xf0000`, `P+0x200=0xfffff`,
  event mask/status 0, `P+0x148=1`, `P+0x308=1`); 2 shared signal routing as the
  scanner driver does; 4 same init for LF; 8 the scanner driver's complete
  motor-off shared setup (routing, driver-IC serial wake, scanner block init).
  Refused while any drive enable register is non-zero.

## At rest after power-on (`print_mech/regs_at_rest.json`)

Both drive blocks disabled, command 0, period `0x1fa0`; both primary control
words `7`; all position, event and capture words 0; scanner block also 0.

## Hand-push watches (carriage moved by hand, power on, all drivers disabled)

| Run | Preparation | Watch | Any register change |
|---|---|---|---|
| `hand_push_watch.json` | none | 120 s, carriage not moved during the window | none |
| `hand_push_watch2.json` | none | 180 s, carriage pushed back and forth | none |
| `prep1_hand_push_watch.json` | variant 1 | 180 s, pushed | none |
| `prep3_hand_push_watch.json` | variant 3 | 150 s, pushed | none |
| `prep9_hand_push_watch.json` | variant 9 | 150 s, pushed | none |

Observed facts:

- The CR position register accepts a preset and reads back masked to 19 bits
  (`0xf0000` → `0x70000`), consistent with the stock `& 0x7ffc0` mask.
- Control `0x0d` and `P+8=1200` persist; the drive enable stayed 0 throughout.
- With variant 9 the shared setup ran to completion (driver-IC serial wake
  succeeded, scanner block initialised to `0x200d` / `0x700000`), and the CR
  drive period/command took the shared values `4000` / `2000` while still
  disabled.
- In none of these states did `0xc0c51100` change while the carriage moved.

## Interpretation

Hand motion does not reach the CR counter in any motor-off state reachable from
documented contracts. Two candidates remain, not distinguishable without a new
contract or an actuation step:

1. The carriage encoder sensor is unpowered until the stock head/carriage power
   acquire (mode 4) runs. This is the same prerequisite that blocks the fuse-ROM
   read (`CARTRIDGE.md`).
2. The counter only runs while the drive is enabled (`0xc0c50000 = 0x102`),
   which is an energised-driver step and needs explicit authorization.

No motor was energised and no register outside the listed blocks was written.

## Stage D: first paper-feed (LF) motor pulses (2026-10-24, later)

Requests: `0x66` IN GPIO snapshot (896 words, `tools/gpio_snapshot.py`, with
`--diff`), `0x67` OUT bounded LF pulse (`tools/lf_jog.py`: raw command
1400..2600, 2000 neutral, 1..200 ms, refused while any drive enable is set),
`0x68` IN result with 1 ms position samples. Emulated in
`tests/validate_printregs.py` (scripted position, sequence and bounds).
Each physical pulse was preceded by a webhook power cycle (motor-test rule)
and, where noted, by `print_regs.py --prep`.

| Run | Preparation | Command / ms | Counter delta | Observation |
|---|---|---|---|---|
| `lf_pulse_2120_50ms.json` | none | 2120 / 50 | 0 (counter at reset) | not noticed |
| `lf_pulse_1880_50ms.json` | variant 4 (LF primary init) | 1880 / 50 | 0 | not noticed |
| `lf_pulse_2300_100ms.json` | variant 4 | 2300 / 100 | 0 | not noticed |
| `lf_pulse_2300_100ms_wake.json` | variant 12 (shared driver-IC serial wake + LF init) | 2300 / 100 | **+373**, smooth ramp | user: "something is moving" |
| `lf_pulse_1700_100ms_wake.json` | variant 12 | 1700 / 100 | **−341**, smooth ramp | moves |

Findings:

- The motor driver IC needs the shared serial setup (the scanner driver's
  `initialize()`: routing, `0xc0c502xx` serial commands) before any LF output
  has effect. Without it, three pulses up to 15 % produced nothing.
- With the LF primary block initialized (control `0x0d`, preset `0xf0000`),
  the LF encoder counts during motion. Hand motion was not tested for LF.
- Command above neutral increases the counter, below decreases it; both ramps
  start after about 2 ms and reach roughly 4 counts/ms at 15 % within 100 ms.
  Physical direction (feed versus retract) is not yet assigned.
- GPIO diffs around the pulses show no sensor change other than slots written
  by the shared setup itself (`0xc0c85628`). One earlier one-time change of
  group 0 slot 11 (`0xc0c840b8`, bank-0 input bit 11) between the pre-motor
  baseline and the first pulse remains unexplained; it persisted across power
  cycles.

### Direction and key-mapped bench pulses (2026-10-24, later)

A 2 s pulse at 2300 (`lf_pulse_2300_2s_wake.json`) gave +8724 counts at a
steady 4.3 counts/ms; the user confirmed the paper motor turns. For bench
observation the Copy keys were bound to slow bounded pulses (Copy B/W: command
2200 for 1.5 s; Copy Colour: 1800 for 1.5 s; first press runs the driver wake and
LF init). This binding exists only for the owner's hands-on tests; emulator
coverage of the motor stays on USB request `0x67`.

**User observation: Copy B/W (command above neutral, counter increasing) feeds
paper toward the printer.** Log of the presses: `lf_keypress_log.txt`.

| Press | Command | Counter delta over 1.5 s |
|---|---|---|
| B/W | 2200 | +2978, +3019, +2973, +3101 (four presses) |
| Colour | 1800 | −397, −436, −1 (three presses) |

At 10 % the reverse direction barely turns while forward runs at about 2 counts
per millisecond; at 15 % (earlier 100 ms pulses) both directions moved alike.
The reverse load is therefore higher at low command, consistent with a one-way
clutch or cam on the pickup/purge side. Not investigated further.

### Forward feed sequence and the fourth-channel probe (2026-10-24, later)

Six consecutive 2 s pulses at 2300 (`lf_feed_sequence_slot11.json`): about
9,000 counts each, 54,000 in total, no GPIO change, group-0 slot 11 stayed 1.
Forward rotation alone picks no paper and trips no sensor.

The user reports a separate "feeding" action that slides one sheet in. The
motor-family stride suggested a fourth channel at `0xc0c54000/55000`: it reads
all zeros at rest (`regs_ch4_at_rest.json`) and motor-off init words written to
it do not read back (`regs_ch4_after_prep.json`), unlike every real channel.
**There is no fourth motor controller in this ASIC**; the three channels are
carriage, paper feed and scanner, matching the stock trace in the memo.
Working hypothesis: pickup is the paper-feed motor through a gear train that is
selected by carriage position; the carriage must leave the cap first. The
bounded pulse request now accepts channel 2 = carriage with a 200 ms cap; no
carriage motion has been commanded.

### Stage B: carriage (CR) motor moves (2026-10-24, later)

Channel 2 of the bounded pulse (`lf_jog.py --channel 2`, later bound to the
Copy keys for the bench). Each run after a webhook power cycle and the shared
driver wake plus CR primary init (`print_regs.py --prep 9`).

| Run | Command / ms | Counter | Observation |
|---|---|---|---|
| `cr_pulse_1700_100ms.json` | 1700 / 100 | 0 | nothing (toward the park wall) |
| `cr_pulse_2300_100ms.json` | 2300 / 100 | +64 (one step) | small sound |
| `cr_pulse_2300_500ms*.json` | 2300 / 500 | 0 | nothing; head interface on or off makes no difference |
| keys at 2300/1700, 1 s | 2300, 1700 | 0 | nothing |
| keys at 2600/1400, 1 s (`lf_keypress_log.txt`) | 2600 then 1400 then 2600 | `0x7c380` → `0x5f380` → `0x7c380` | **carriage moves: B/W (2600) left, Colour (1400) right** |

Findings:

- 15 % does not unseat the carriage; 30 % moves it. At 30 % one second is
  enough to cross the full width: the two end values differ by `0x1d000`
  (118,784 counts) and repeat exactly, so the carriage reached both end stops.
  Key pulses were shortened to 500 ms.
- Command above neutral moves the carriage left (away from the park wall) and
  counts up; below neutral moves right. The counter reports in steps of 64
  (`& 0x7ffc0`), so the full travel is about 1,856 steps.
- The carriage encoder counts only while the drive is active; hand motion with
  the drive disabled never counted (Stage A), and the head interface state does
  not matter for counting.
- The paper pickup question is unchanged: no fourth motor controller exists;
  the pickup gear train is expected to engage at a specific carriage position.

### Paper pickup, transport and the paper-end sensor (2026-10-24, later)

With paper loaded and the carriage parked (counter at the `0x70000` reference,
which the right end stop reproduces exactly):

| Step | Command / ms | Counter delta | GPIO change | User observation |
|---|---|---|---|---|
| carriage left, feed fwd 2 s | 2300 | +9078 | none | no feed |
| carriage middle, feed fwd 2 s | 2300 | +9120 | none | no feed |
| feed reverse 2 s, 15 % | 1700 | −3950 (loaded) | none | "the feeder moves a bit" |
| **feed reverse 2 s, 30 %** (`lf_reverse2s_30pct.json`) | 1400 | **−14986** | **`0xc0c84e08` bit 0: 1 → 0** (bank-7 input `0xc0c813cc` bit 0) | sheet picked up |
| feed forward 2 s ×3 (`lf_forward2s_after_pickup.json`, `lf_eject_1/2.json`) | 2300 | +7836, +8758, +8895 | bit back to 1 during the third pulse | "paper is in midway" |
| feed forward 2 s ×3 more (`lf_eject_3..5.json`) | 2300 | about +9000 each | none | "paper fully out" |

Conclusions:

- **Pickup is the paper-feed motor in reverse** (command below neutral) through
  a heavily loaded train that needs about 30 % to turn; forward is the transport
  and eject direction. Carriage position was not the switch; it stayed parked.
- **Paper-end sensor:** GPIO group 7 slot 0, read alias `0xc0c84e08` bit 0,
  mirrored as bank 7 input word `0xc0c813cc` bit 0. 1 = no paper, 0 = paper
  present. The bootloader board init already configures this group
  (`0xc0c84e0c = 0x10101`). Found by snapshot diff, no RE.
- Roughly 16,600 forward counts from the pickup end until the trailing edge
  clears the sensor; the leading-edge offset to the print zone is not measured.
- All pulses were open-loop and bounded; nothing closed the loop yet. A feed
  routine can now be: reverse at 30 % until the sensor reads 0 (bounded
  retries, as stock does), forward a measured distance, eject forward until the
  sensor reads 1 plus a margin.

### Closed-loop feed routine, first physical runs (2026-10-24, later)

`src/paper_feed.c` (USB `0x69`/`0x6a`, `tools/paper_feed.py`, emulated in
`tests/validate_feed.py`, seven cases): pickup in reverse at 30 % with three
bounded attempts gated on the paper-end sensor, a handoff distance past the
sensor, a settle pause, forward advance, eject until the sensor clears plus a
margin, jam stop after 30,000 covered forward counts, Cancel/USB stop.

| Run | Outcome |
|---|---|
| `paper_feed_closed_loop_1.json` | sensor at 8,211 reverse counts; switched to forward immediately (no handoff; an unsigned compare also skipped the advance). Sheet entered but stalled midway while the roller ran; timeout after 49,731 forward counts. User: "stuck midway while motor still running". |
| `paper_feed_eject_manual_sheet.json` | eject of a hand-inserted sheet: sensor cleared after 9,110 counts, margin, ok in 4.3 s |
| `paper_feed_closed_loop_2.json` | with handoff 7,000: pickup 15,222 counts, advance 4,000, eject 15,814, ok in 9.8 s. User: the feeder held the sheet until the mechanism returned to its rest position, then the sheet moved. |

Owner's description of stock: the transport motor runs forward continuously
and the "feeder" opens and closes immediately while forward stays on. That is
how a solenoid- or clutch-triggered pickup cam behaves, not a reverse-driven
one. The group-0 slot-11 input (`0xc0c840b8`) is the suspected cam home sensor.
The reverse-pickup routine works but is probably not the stock mechanism;
questions are in `fw_dump/gpt/BRIEF_paper_pickup.md`.

### Feed routine aligned with the stock ASF sequence (2026-10-24, later)

The third RE answer (`fw_dump/gpt/BRIEF_paper_pickup.md`) confirmed that stock
picks up in reverse (LF direction 1, command at or below neutral) with no
separate pickup actuator, and gave the distances (1 stock unit = 6 counts):
first phase 12,300 counts before the paper-end sensor is consulted, search up
to 90,000 total, post-edge continuation 7,968–12,018 by mode record, forward
alignment 627 units = 3,762 counts plus page corrections, forward swing-arm
reset 18,570 after a miss, three attempts, eject 21,576 past the trailing edge,
sensor read twice and debounced. It also confirmed the paper-end sensor
descriptor at `0xc0c84e08` (stock reader `0xc3028`) and could not identify the
group-0 slot-11 input.

`paper_feed.c` now uses those distances with fixed drive commands (1400
reverse, 2300 forward) instead of stock's servo profiles. Physical run
`paper_feed_closed_loop_3.json`: pickup 22,140 counts (sensor seen exactly at
the 12,300 check), advance 3,762, trailing edge after 16,021, margin 21,576,
result ok, 14.8 s. Emulation: seven cases in `tests/validate_feed.py`.

### Tuning against the owner's manual runs (2026-10-24, later)

Runs 3–7 (`paper_feed_closed_loop_3..7*.json`) completed but the owner heard
the sheet "grinding" while the pickup kept pulling after the sheet had reached
the transport nip. Shortening the post-edge continuation (9,840 → 4,000 →
1,500) and consulting the sensor early (first phase 12,300 → 2,048) shortened
but did not remove it.

Manual mode (USB `0x6b`, Copy keys toggle reverse/forward for the bench; each
transition logs elapsed ms, counts and the sensor): the owner's good pickups
were reverse 0.9–1.2 s (8,518–10,828 counts, sensor seen) then forward until
the sheet was out; reverse of 4,200–7,100 counts did not reach the sensor. The
routine was set to those numbers (phase 1 = 2,048, post-edge = 2,048).

Two failed starts followed (`paper_feed_closed_loop_9/10`): 90,000 reverse
counts without pickup, a forward swing-arm reset, a second attempt, Cancel. The
owner identified the cause: the swing arm was left mid-cycle by the aborted
runs and the 18,570-count reset was not enough. A 9.5 s forward run (about
40,000 counts) before the feed made the next cycle work at once
(`paper_feed_closed_loop_11_after_reset.json`: pickup 9,876, ok in 11.9 s).
The routine now homes the arm forward (37,140 counts) whenever its position is
unknown: at boot, after any aborted pickup or manual reverse; a completed eject
clears the flag.
