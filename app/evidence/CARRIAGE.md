# Print carriage position control (2026-10-24)

Module `src/carriage.c`; tool `tools/carriage.py` (home / move / status / tune);
grid runner `tools/carriage_tune.py`; emulation `tests/validate_carriage.py`
(six cases: homing, left and right moves, refused targets, stall, USB stop).

## Contract used

Companion drive `0xc0c50000` (enable `0x102`, period 4000, command, 2000
neutral), primary `0xc0c51000` initialised with control `0x0d`, preset
`0xf0000`, `+0x148=1`, `+0x308=1`; position `0xc0c51100`, 19 bits, steps of 64.
Shared driver-IC serial wake first (`scanner_motor_shared_init()`).
Command above neutral = left (counter up). Measured travel `0x1d000` counts.

## Homing

Drive right at command 1400 until the counter is unchanged for 150 ms, take the
counter as the park reference (x = 0). Reproducible: `0x6ffc0` on every run
today (`home_1.json`, `home_2.json`, and every grid cycle).

## Closed-loop moves

1 ms ticks from the main loop; speed from the position delta over a 32 ms
window; cubic ramp from 10,000 counts/s to the cruise (default 30,000, max
90,000); linear slow-down over the last 8,000 counts to a 10,000 floor; done
within 192 counts, then 50 ms settle, neutral, drive off. Controller: effort =
wanted/150 + error/1200 + integral(error/8000, clamp 400), no braking, a 30 %
kick for the first 150 ms while speed < 2,000 (stiction off the cap), clamp
40 %. Stall after 400 ms without counter change; soft limits 0..116,736.

| Version | Behaviour on hardware |
|---|---|
| v1, scanner PI (coast only) | lands within 130 counts but bang-bang: speed 0..55k around a 30k target, output alternating neutral/40 % |
| v2, FF+PI with braking, gains 1/250 | smooth cruise 26k..34k at ~20 %, but stalls in the slow approach (stick-slip below ~15k counts/s), 4,000 counts short |
| v3, higher gains, floor 10k | completes exactly but oscillates 0..75k with brake/drive flipping |
| grid 1 (`tune_grid_1.json`) | no-brake / softer PI sets halve the cruise spread (sd ≈ 8–10k vs 19–20k) and remove approach stalls; low integral limit stalls |
| grid 2 (`tune_grid_2.json`) | longer speed windows (48/64 ms) add lag and worsen; Kp 1/1200..1/1600 best |
| final defaults (set F) | see `move_60000_final.json`, `move_back_final.json` |

Remaining roughness comes from the 64-count counter granularity in the speed
estimate; the stock servo uses the hardware period capture instead. Timing
constants (1 ms tick) are from the app clock, not from the stock timer units.
No head firing is involved anywhere in this module.
