# Stock head-maintenance routines run as fixtures (hybrid shape B, 2026-10-26)

Emulator-only characterization of the stock cleaning, wiping, capping and
uncapping entry points named in RE brief 16
(`fw_dump/gpt/BRIEF_maintenance.md`), with the status-server and motor
message contracts from RE brief 17 (`fw_dump/gpt/BRIEF_status_server.md`).
Nothing was flashed or actuated. The tool is
[`emu/stockapp/fixture.py`](../../../stockapp/fixture.py); `fixture_summary.py`
prints a saved report. The compact report is
[`fixture_report.json`](fixture_report.json) (MMIO lists cut to 400 entries,
service logs complete).

```sh
docker run --rm --network none -v "$PWD:/project:rw" mg2550s-emu \
  emu/stockapp/fixture.py --output emu/output/fixture/maintenance.json
python3 emu/stockapp/fixture_summary.py emu/output/fixture/maintenance.json --collapse --mmio
```

## Method

Stock boots to its idle loop with the public `BootTrace` model (readiness 4,
`emu/inputs/eeprom.bin`, 8,432,633 instructions, 300 ticks). RAM and modeled
MMIO are snapshotted and restored between cases. Each case is a direct Thumb
call from a stopped return sentinel (SP `0x100fe000`, LR `0x100ff001`).

Two findings about the call convention itself:

- **System mode (`CPSR 0x3f`) is required.** Brief 16 §7 suggested SVC mode
  `0xd3`; with it the wipe trace reached the head-engine setup and then fell
  into the stock fatal handler at `0x400..0x4a0` (peripheral re-init, spin on
  `0xc0c82008`) because the kernel's own `svc #0` inside `0x107d42` corrupts
  the banked SVC link register. Brief 17 §7 confirms `0x3f`.
- **The kernel reschedules on its own.** Without stubs, `0x107d42`
  (blocking pool allocation, used by the NVM write path) issues `svc #0`
  and the stock scheduler switches to another task's stack
  (`0x150694`, SP `0x1876bc48`), after which the trace runs the mailbox-7
  task loop instead of the maintenance routine. The fixture detects a SP
  outside its own stack and the SVC stack and stops.

Services are intercepted at their stock entry addresses. In **stop** mode the
case ends at the first event wait, semaphore wait or mailbox receive (brief 16
§7). In **stub** mode each entry is logged and returned from with modeled
results:

| Entry | Role | Stub behaviour |
|---|---|---|
| `0x106d16` | event wait | report the requested bits; on event `0x33` (EEPROM completion) complete the modeled I²C transfer; on `0x1c` bit 31 (head work done) heat the cached head temperature by 4 |
| `0x106bf0`, `0x106cce` | event set / clear | log |
| `0x106720`, `0x106672` | semaphore wait / release | log, return 0 |
| `0x895c4`, `0x107170` | mailbox send (wrapper, raw) | log and decode the message (below) |
| `0x107286` | mailbox receive | answer from the status-server model (below) |
| `0x89490`, `0x107d42`, `0x107e28` | pool alloc, blocking alloc, free | scratch bump allocator at `0x100f0000` |
| `0x105dd2` | kernel query (`0x15` = state of task 21, the status server; brief 17 §1) | return 0, "running" |
| `0x1085be` | task delay | time passes: readiness countdown `u16 0x180d5720` decrements, cached head temperature cools by 1 toward 50 (25 °C) |

**Status-server model (brief 17 §1–3).** The requester at `0x9ad5a` builds a
`0x30`-byte message: command in the low half of word `+0x24` with bit 21,
reply mailbox in `+0x10`, a one-hot group selector in `+0x28`. The model
answers `f008` from `u32[0x180662ec + 4*ctz(selector)]` and `f009` from
`u32[0x18066468 + 4*ctz(selector)]`, writing the value into `+0x2c`;
`f004`/`f005` apply `new = (old & preserve) | (replacement & ~preserve)` to
those banks (three payload words) and `f004` recomputes the summary word
(`f008/0x20`). The reply reuses the request buffer with bit 22 and result 1
in bits 24..26. The banks are the stock RAM left by the emulated boot; the
model reads and updates them but nothing else refreshes them, so they are
not a physical mechanism state.

**Other fixture assumptions.** The executor's readiness wait `0xe97b2`
stores a count from `0xe9844` (95) in `u16 0x180d5720` and polls it, byte
`0x180d57e7` and the cap state between 20-tick delays. The pre-phase loop
`0xe80ba..0xe80d8` waits while `s16 0x1807acd4` (BK temperature,
half-degrees) is at or above `2 * s16[0x18079ab0]`, up to 1000 delays. The
emulated boot leaves the maintenance parameters at `0x18079aa8..` zero; the
preset `maint-defaults` installs the values brief 16 attributes to `0xe8446`,
including LF-stroke enable `0x18079abc`. **Brief 16 reversed the polarity of
the abort byte: `u8[0xe0000172]` must be 1** (brief 17 §4); the preset is
`permission`.

## Results

Expectation cases from brief 16 §7 (`--verify` passes):

| Case | Result | Brief 16 §7 |
|---|---|---|
| `0xe8324(1,0,0,1,0,0)`, enable byte 0 | returns 1 after 17 instructions, no MMIO | 1 after 18, no MMIO |
| same, enable byte 1 | stops at `0x106d16(0x1a,0x2010,0,…)` from `0xd1ae5` after 24 instructions | same, after 25 |
| `0xe818a(1,1)` | stops at `0x106720(0x13,-1)` from `0x9ad7b` after 245 instructions | same, after 246 |
| `0xe8610()`, `0xc763e()` | return after 7 and 25 instructions, no MMIO | 8 and 26 |

Stubbed traces. Presets for the cleaning cases: executor enable, cap enables,
permission, guards clear, ambient, maintenance defaults; cap/uncap/wipe
without the executor presets. After the emulated boot, status bank A
(`f008/0x40`) holds `0x1000` and the summary `0x2`; the `clearA` variants
zero both.

| Routine | Return | MMIO writes | What ran |
|---|---|---|---|
| Clean BK type 1 | 1 | 0 | sync wait, 2 × 95 readiness delays, `f005(2, ~2, set)`, NVM group 3 record and mirror, gates `S20/S200/S400`, **CR `0x4041` target 2688 profile 3**, one head list, status checks. Heating skipped. |
| Clean BK type 1, bank A cleared | 1 | 3,503 head engine | as above plus 31 heating list runs (8 slot ids on `0xc0c29d48`, three 16-word tables, record A `0xfffffc00`, interval `0xc6`, start), cool-down loop |
| Clean BK type 2 | 3 | 50 | as type 1, then **LF `0x4031`: 50,000 Ulf, fraction 0/6, direction 0, profile `0xc0000002`, no reply**, 33 head list runs with `f008/0x20` polls, LF-completion wait, wipe (CR `0x4043` kind `0x2003` option 1, **CR `0x4041` target 16640 profile 3**, kind `0x2002`, release `0x4042`), `f004(0x200, ~1, set)`, notification `0x1005` to mailbox 7, NVM group 5 record, 0xc9-tick delay, two list runs. Returns 3 because the `f004` publication sets summary bit 0 and nothing clears it in the fixture. |
| Cap `0xe7dd6(2,1,1)` | 1 | 0 | six gate queries, `S20`, sync wait, `S200/S400`, **CR `0x4041` target 2688 profile 3**, CR `0x4043` kind `0x2005`, `f008/0x2000` and `f009/0x80` pointer reads, `f004(0x2000, 0, 0x1807ad64)` pointer publication, notification `0x1066` to mailbox 7, `f009/2`, `f005(2, ~1, set)`; cap state byte 0 → 1 |
| Uncap `0xe7a2a(0,1)` from capped state | 1 | 0 | `f008/0x20`, `f009/0x80`, sync wait, `f009/2`, `f009/4`, `f005(2, ~1, clear)`, NVM group 3 record and mirror, head-power owner wait `0x1b:1`, `f008/0x20`; cap state byte 1 → 0, **no CR request** either way (bank A set or cleared); brief 16 §4's release retries at 2688 were not reached, the decision presumably comes from the 24-byte object behind `f009/0x80` |
| Wipe `0xe8668(0,1)` | 4 | 0 | gated out after reading `A`, `B`, `C` |
| Wipe, bank A cleared | 1 | 50 | sync, CR `0x4043` kind `0x2003` option 1, **CR `0x4041` target 16640 profile 3**, kind `0x2002`, release, head power `0xc0c40000`, `0xc0c20000`, CR controller init `0xc0c50000..0xc0c51400`, head-engine init, `f004(0x200, ~1, set)`, notification `0x1005`, NVM group 5 record, 0xc9-tick delay, two list runs |

Observed request formats (decoded by the fixture, layouts per brief 17 §5–6):

- **CR move, mailbox `0x19`, `0x4041`, `0x8c` bytes:** `+0x28` s32 target
  in stock CR counts, `+0x2c` profile/flags; reply mailbox `0x1b`; the
  producer then sets event `0x1d:1` and waits `0x1a:0x10`. Seen: 2688/3
  (cleaning and cap position), 16640/3 (wipe position).
- **CR profile motion, `0x4043`, `0x38` bytes:** `+0x28` kind/flags
  (`0x2003`, `0x2002`, `0x2005`: low 6 bits select a motion profile from the
  14-entry table at `0x18089900`, `0x2000` a post-move hold), `+0x30` u8
  option. Not named destinations.
- **CR release, `0x4042`, `0x28` bytes, no payload**, waited on `0x1a:0x20`.
- **LF move, mailbox `0x18`, `0x4031`, `0x1f4` bytes:** `+0x28` s32 Ulf,
  `+0x2c/+0x2e` fraction, `+0x30` u8 direction, `+0x34` profile/flags
  (`0xc0000002`: bits 30 and 31 set by `0xd6158`/`0xd61a2`), reply
  mailbox `0x1b` unless the no-reply sentinel `0x20` was passed. Producer
  waits `0x1d:0x400` (accepted), completion is `0x1a:0x2000`.
- **NVM envelope, mailbox 2, `0x34` bytes:** `+0x1c` opcode 6 (write),
  `+0x28` data pointer, `+0x2c` EEPROM byte offset, `+0x30` length. Seen:
  group 3 at `0x140` (96 bytes) with mirror at `0x1a8`, group 5 at `0x220`
  (476 bytes).
- **Status requests to mailbox `0x17`:** `f008` selectors `0x20` (summary),
  `0x40/0x80/0x100` (banks A, B, C), `0x200/0x400`, `0x2000` (pointer);
  `f009` selectors `0x2` (cap bit 0), `0x4`, `0x80` (pointer to a 24-byte
  object); publications `f005(2, ~2, set)` at cleaning start,
  `f004(0x200, ~1, set)` after the wipe's CR release, `f005(2, ~1, set/clear)`
  for cap state, `f004(0x2000, 0, pointer)`.

## What this establishes and what it does not

- The software sequences of all five routines are now visible end to end
  with the brief 17 contract, and the gate rules of brief 17 §4 reproduce:
  with `0xe0000172 = 1`, cap issues the 2688 move; with it 0, cap returned 3
  after the seventh query (earlier runs).
- Status bank A bit 12 (`0x1000` after the emulated boot) blocks the wipe
  wrapper and head heating. Its meaning is unknown; the fixture clears it as
  an experiment only.
- The fixture never moves a motor, reads a sensor or refreshes the status
  banks; return value 1 means the stock code path completed with these
  answers. The status-server model, the time and temperature models, the
  EEPROM completion and the absence of motor-task publications (which is why
  type 2 ends with return 3) are all fixture assumptions.
- Instruction counts differ by one from brief 16 §7 because this harness
  counts from the first instruction of the call.
