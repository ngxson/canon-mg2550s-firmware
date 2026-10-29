# Staged candidates — ink / status / counters (test in one HW session)

Built by `app/patch/stage_candidates.py` into
`emu/output/stockapp/stage/<name>/`. Every candidate rides the known-good
scanning base (native_startup + runtime + stay_awake + auto_power + fake_door +
telemetry) and all pass emu boot validation (boot to idle, no fault, flash
unchanged). The *effect* of each patch is observable over USB on hardware
(status/service query, EEPROM dump, counter clear) — **no paper needed** for any
of the checks below.

Evidence / mechanism source: `fw_dump/gpt/BRIEF_ink_status.md` and
`fw_dump/gpt/BRIEF_counters.md` (answers), verified against the dump. The old
vtable-based `ink_clamp`/`paper_count` are **superseded** (reports read via the
registry family; page/scan/dot increments route through `0x3be30`, not the
vtable setter).

## Candidates

| name | adds | flashes |
|------|------|---------|
| `ink_report` | IL(...) → 15 (full nibble); `CTK:` status line dropped | `stage/ink_report/candidate.mgap` |
| `ink_report_detail` | + status-detail ink codes 6/7/8 → 0 (no `MarkerSupplyAttention`) | `stage/ink_report_detail/candidate.mgap` |
| `diag_counters` | + **stop tracking** (0x3be30 page/scan/dot, 0xcb7e8 cartridge) + **clear counters** (0xd3) | `stage/diag_counters/candidate.mgap` |
| `diag_ink_lamp` ⭐ | + **dark ink lamp**: ink-warning blink pattern `0x19941e` replaced with the default dark pattern (data-only, 6 bytes; covers BOTH low-ink and monitoring-off/"unknown" states — verified on HW 2026-10-29: lamps dark, job gate open). | `stage/diag_ink_lamp/candidate.mgap` |

Diag USB commands are on **every** candidate (telemetry base):
- **Dump EEPROM** — `0xd2`, reads the 1 KiB working cache `0x1878e200`:
  `python3 tools/dump_eeprom.py --out eeprom_dump.bin`
- **Clear counters** — `0xd3`, on `diag_counters` only (writes NVM):
  `python3 tools/clear_counters.py [--counter N]`

Key patch addresses (byte-exact, verified):
- IL clamp: `0xbc7f2` → `bl il_clamp_veneer` (forces the 4 nibble values to **15**).
- CTK line: `0x3f03a` `0fd5`→`0fe0`.
- detail 6/7/8: `0x38850`/`0x387b4`/`0x387be` → `0020`.
- stop-tracking: `0x3be30` `f8b5`→`4770` (all page/scan/ASF/TPAGE + ink dot-count), `0xcb7e8` `f8b5`→`4770` (CH_BK/CL).
- clear: per-id zero-writes via registry `0x10b2c0` over the counter list in `counter_clear.c`.

## How to run (per candidate)

Recovery if a candidate misbehaves: power-cycle webhook + `fwctl.py stay
--wait 90` (see `fw_dump/HOLD_BOOTLOADER.md`). EEPROM is recoverable from
`eeprom_backup_initial.bin` via the I²C bridge if a clear ever goes wrong.

```
python3 tools/fwctl.py flash emu/output/stockapp/stage/<name>/candidate.mgap
# wait ~74 s for "MG2500 series" to enumerate
python3 tools/stock_status.py
python3 tools/dump_eeprom.py --out before.bin
```

### Pass / fail

**`ink_report`** — `CTK:CL,LOW` gone from `stock_status.py`; IL(...) reads 15
per tank (needs the service-report path to see the IL line). MarkerSupply/
support code may still show (that is `ink_report_detail`).

**`ink_report_detail`** — status detail no longer `MarkerSupplyAttention` for
ink. WATCH: detail `0` still maps to `AttentionRequired` and the status *code*
(idle vs stopped) is a separate word we have not patched — record the exact
`state`/`detail`/support code to decide if the `0x388b4` status override is
needed next.

**`diag_counters`** — the counter build:
- **Dump then clear then dump** (no paper):
  ```
  python3 tools/dump_eeprom.py --out before.bin
  python3 tools/clear_counters.py --counter 0      # clear ONE first (ASF All) to prove the mechanism
  # inspect; then
  python3 tools/clear_counters.py                  # clear all
  ```
  PASS if the targeted counter bytes go to 0 in the dump and the ER history
  (`0x230..0x239`) / event log (`0x2fc..`) are **unchanged**. `clear_counters.py`
  prints the byte diff and saves before/after.
- **Stop tracking**: power-cycle and dump — the hard-boot counter (`0x23a`) and
  P_ON should **still** increment (their write sites are not pinned; stop is a
  follow-up). The page/scan/dot/cartridge counters stop only after a print /
  cartridge insert (deferred verification).

## HW session results — 2026-10-28 (diag_counters flashed)

Flashed `diag_counters/candidate.mgap` to `0x10000` (bootloader held via
power-cycle; `status: C0`, app booted as `04a9:176d` "MG2500 series", ~>120 s
to enumerate — fwctl's app wait watches PID `0x12fe` so it false-timed-out; the
stock-as-app keeps `0x176d`).

- **Ink reporting — PASS (clean idle).** Raw `GetStatusResponse`:
  `<ivec:status>idle`, `<ivec:status_detail/>` empty, `<ivec:current_support_code/>`
  empty; `ReadData` has **no `CTK:`**; strings ctk/marker/attention/low all
  absent. The `0x388b4` status override is **not** needed. (Separate field
  `CIR:CL=000,BK=UK` still reports true remaining — not clamped; cosmetic.)
- **Dump EEPROM `0xd2` — PASS.** 1024 B read. Live counters:
  ASF All(`0x2ba`)=5, P_ON(`0x23c`)=19, hard-boot(`0x23a`)=32, LPT/ST/CH_BK/CL=0.
  Ink nibbles `0x15c=0xbd / 0x15d=0xbb` unchanged (clamp is report-only).
- **Clear counters `0xd3` — FIXED, PASS (2nd flash).** First attempt failed
  safely (`0x10b2c0` is a READ wrapper that rejected odd count=1 → -17, 0
  written). Per `BRIEF_counter_write.md`, `counter_clear.c` now uses the real
  scalar write `0x10b1bc(id, 0, 1, 1)` + flush `0x112020(1)`. Re-flashed and
  re-tested: `clear_counters.py --counter 0` → `1 written`, **`0x2ba: 05→00`**
  (ASF All), only 2 bytes changed (the other is a benign NVM-write-time byte at
  `0x220`); ER history (`0x230`) and event log (`0x2fc`) untouched. Clear-all
  not yet run (awaiting go-ahead).
- **Stop-tracking** — present in the image; not observable without a print
  (no paper). Deferred.

## Open / follow-ups
- **Report-full completeness**: whether `ink_report`+`detail` yields a clean
  idle status or still an `AttentionRequired`/non-idle code → maybe the
  `0x388b4` status override (observe first).
- **Job block on empty ink** — `BRIEF_ink_status.md` §5, unpinned.
  (2026-10-29: **job gate CONFIRMED OPEN** — StartJob/ModeShift/raster ACKed
  with INK_OFF set on both tanks; print jammed on paper pickup, unrelated.)
- **Stop power-on / last-print tracking** (`0x105028d`/P_ON `0x105028e`/LPT
  `0x1050292`): needs an id write-drop on the registry write path (`0x10d246`),
  a follow-up. Clear-counters zeroes them on demand meanwhile.
- **inkLevel write-drop** (keep nibbles from being rewritten at boot, req #1):
  same registry-path write-drop follow-up; `ink_report` already forces the
  *reported* value to full.
