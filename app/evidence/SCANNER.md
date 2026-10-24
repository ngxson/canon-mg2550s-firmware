> Historical stationary-capture evidence. Later user-authorized work physically
> verified [homing](HOMING.md) and added the [full-page scan PoC](PAGE_SCAN.md).
> The stationary-only motor restrictions below describe the old task, not the
> current scan authorization. Profile-7 varying data was insufficient evidence
> of a useful optical image; the page work now uses optical profile 0.

# Scanner (CIS) bring-up — RE + hardware evidence

Living record of the raw-CIS proof-of-concept. Recorded 2026-10-23.

---

# HANDOFF FOR THE NEXT AGENT — open questions & how to continue

**Mission:** get the Canon MG2550s scanner (CIS) to capture a real image under our
open firmware, dumped over the USB debug log, and verify it against the document
on the glass (`fw_dump/scan_ref.jpg`, a French pharmacy invoice — verify
*structurally*; do NOT copy its personal data into any file). First milestone:
one real sensor line. Longer term: full page + an eSCL-over-USB scan interface.

**🎉 MILESTONE (HW): the scanner capture pipeline works end-to-end on our open
firmware.** The full stationary capture (USB `0x59`, `scanner_full_capture`) runs
the codex recipe — A.1 IRQ-source init, LDMAC descriptors, the 171-write SCNIO
config, AFE serial power-on, arm/run, one request, bounded wait, stop — and the
**LDMAC DMAs a real sensor line into our RAM buffer** (3072 samples, both channel
events `0x7`, read-count advances). Variant `0x02` (AFE + lamp) yields **varying
per-pixel optical values** (~`0x64xx`), vs a constant `0x8888` with no AFE — so
the CIS is being read for real. At the **park position** the line is near-uniform
(margin/calibration strip, two channel-halves with different offsets), so imaging
the actual document needs the carriage moved over it (motor) + exposure tuning.
Variants: bit0=A.2 clock switch (writes clock controller — risky), bit1=AFE+lamp,
bits4-5=wait (0 fixed is best; poll-read-count stops too early). Fixed 80 ms wait
lets the full line DMA. Retrieve the buffer with USB `0x5a` (256-byte chunks).
Recipe transcribed via `app/src/scnio_seq.h` (generated from `scanner_recipe_codex.md`).

**Capture + light path perfected (HW).** Variant bits now: 0x01 clock switch
(risky), 0x02 AFE serial, 0x04 lamp/scanner-power (`0xc0c85614`), 0x08 exposure,
0x30 wait. **Lamp verified:** power OFF → flat `0x8180` (nothing read); power ON →
real varying `~0x64xx` per-pixel data, so `0xc0c85614` is the scanner analog
power that gates any real read. Capture is **stable, ~12.6-bit effective** (24
captures, per-pixel temporal std ~10), uniform across the line (reading the white
calibration strip at the park position; two sensor halves offset ~65 counts —
flat-field calibration will correct that during a real scan). Host tool
`app/tools/scan.py` runs a capture and saves `.pgm` + raw `.bin`.

**Motor: deferred to a supervised lab session.** Codex RE (`scanner_motor_codex.md`,
verified) concluded no crash-safe autonomous "move N steps" recipe exists: motion
is an IRQ `0x1c`/timer-4 control loop (our firmware is IRQ-masked), there is no
home/limit sensor, and `stop` (`0xc0c56000=0` + emergency tail) is a command not an
overshoot bound. Carriage motion stays OFF until debugged physically with the user
present. Direction: positive distance = away from home (scan dir). Emergency stop:
`c0c56000=0; c0c56008=0x7d0; c0c5711c=0; c0c56100=0; c0c57400=0; c0140400=0x80000000`.

**Earlier context:** a Codex (gpt-6-astra) RE pass — **verified against the dump** —
corrected several earlier conclusions. Read `fw_dump/gpt/scanner_codex_answers.md`
in full. The corrections below supersede parts of this doc's lower sections.

### VERIFIED CORRECTIONS (confirmed first-party against the dump)
1. **`0xc0d601c0` is a calibration / parameter-search block, NOT the pixel DMA.**
   Its `+0x1c` is a **20-bit scalar** (setter `0xf3b16` masks `0x000fffff`), and
   callers feed it small calibration values from profile structs, not RAM
   addresses. There is **no `0x20000000` DMA base** — the earlier offset theory
   was a 20-bit-truncation artifact. Do not relax the dest guard on that basis.
2. **The real image DMA is a different block: `LDMAC` @ `0xc0d50000` and
   `BUFADM` @ `0xc0d70000`** (stock diagnostic name table recs `0x15fbf0`/
   `0x15fc44`; strings `SCNIO_LDMAC`/`SCNIO_BUFADM` — verified). Accessors
   `0xf200c`/`0xfbf12` keep full 32-bit args. THIS is where pixel capture lives;
   it is not yet traced (buffer/dest encoding, BUFADM interaction = open work).
3. **The "dark sub-blocks" conclusion was a measurement error.** The dpi field at
   `0xc0d60130` is **bits 0–4** (getter `0xf4230`), line-count `0xc0d60164` is
   **bits 0–7** (setter `0xf4354`) — verified. Our probes wrote `0x20` and `0xff00`,
   **outside** those fields, so the zero readbacks prove nothing. **Re-probe with
   a value in the low field** (see codex's table at end of its report) before
   claiming anything is unclocked.
4. **`0xc0133xxx` is interrupt-source control, not a clock gate** (ID→register
   mapper `0x109cac`; bit 4 = interrupt ack/W1C). There is **no missing parent
   clock** — that whole thread was a misread. The only real scanner-associated
   root-clock switch is `0xc010112c`/`0xc010102c` RMW in the test path `0xefdb6`.
5. **MOTOR IDENTIFIED (second Codex pass, verified):** carriage motor =
   **`0xc0c57000` (primary) + `0xc0c56000` (companion)**, IRQ src `0xc0133188`
   (binding at `f67e0`; start `c0c56000=0x102`, stop `=0`). `0xc0d60400` is
   `SHDDEC` (image shading), not motor. **Motor-safety rule: never write
   `0xc0c56000`/`0xc0c57000`.** A stationary capture is motor-free (stock's mode-4
   / no-motion branch clears SCNIO `0xc0d6011c` bit0 and issues no motor write).
   Full DMA/trigger/motor map + a stationary capture schedule:
   `fw_dump/gpt/scanner_dma_codex.md` (verified on the load-bearing points).
6. AFE **serial** setup IS required before capture (power-on routine `0xfa234`;
   `0xfa2d0` is power-OFF). Clock/power alone won't yield pixels.

### Revised immediate steps
- ✅ **RESOLVED: the sub-blocks were never dark.** HW probe `0x56` wrote `1` to the
  correct low fields: `0xc0d60130[0:4]=1` and `0xc0d60164[0:7]=1` both latched
  (with and without the SCNIO enable). The SCNIO config registers are fully live
  on our firmware — no clock-tree bring-up needed. (Earlier zero readbacks wrote
  bits outside the valid fields.)
- ✅ **LDMAC/BUFADM DMA mapped.** 4 LDMAC channels at `0xc0d50000/0x100/0x200/0x300`
  (flash base table `0x18be14`), BUFADM at `0xc0d70000` (`0x18be10`). Per-channel
  accessors `0xf200c`+ write full 32-bit values to `base[ch]+0x14/0x18/0x1c/0x20`
  (RAM addr/length/ctrl — exact field roles still to pin down). Channel config
  loop `0xfbf12` → helpers `0xf1f2e/0xf1f50/0xf1f62`. This is the real pixel sink.
  HW probe `0x57`: the channel enable is **`+0x08=0; +0x00=0; +0x00=1`** (`f1f18`);
  the first probe skipped it. **After the enable, the DMA registers latch full
  32-bit values** (`+0x14/0x18/0x1c/0x20 <= 0x20023040` all read back exactly;
  `+0x08` becomes `0x80`, the predicted pre-arm state). So the pixel-DMA dest is
  programmable from our app. Field roles (verified RE, `scanner_dma_codex.md`):
  **`+0x14` = initial RAM dest**, `+0x18` = buffer base, `+0x1c` = base+span
  limit, `+0x20/+0x24/+0x28` = packed 16:16 dimensions, `+0x2c..0x3c` = address
  increments, `+0x0c`/`+0x10` = event mask/status (write-complement to clear),
  `+0x04`/`+0x08` = channel config/arm. Arm = `+0x08[1:0]=1` (NOT done by probe).
  **HW probe `0x58` validated the full two-channel profile-7 descriptor
  programming** (`ldmac_descriptor()`): ch0 dest=R, ch1 dest=R+0xc00, both
  limit=R+0x3000, `+0x20=0x00010c00`, `+0x28=0x00010001`, `+0x08=0x80` — all read
  back exactly. DMA half proven; remaining = SCNIO/AFE config + run/request +
  completion (codex recipe `scanner_recipe_codex.md`: 163 ordered SCNIO writes +
  DMA + serial + request + stop, literal values, both chip branches).

### Building a working capture — key challenges (from the full recipe)
- **It's IRQ-driven; our firmware is polled (IRQ-masked).** Stock drives the
  request loop + completion via IRQ18/19 timing handlers (`f5494`/`f54a6`,
  `fa418`). Porting means either polling the source pending state and running the
  handler logic inline, or enabling real interrupts (a firmware-architecture
  change). codex could NOT establish a proven raw pending-bit poll for the timing
  sources — this is the main unknown/risk.
- **No proven hardware "line is in RAM" fence.** Best stock signal is a software
  timing event ("intervals elapsed"), not DMA-done. Ground truth = read the
  buffer (A.8 pattern check for the test pattern).
- **Cache: in our favour.** The bootloader keeps caches/MPU disabled for DMA
  coherency, so the recipe's cache-visibility TODO is already resolved here.
- ~200 register writes to transcribe carefully; chip type (`0x18030ecc`) — try
  type 0 or run the chip-type probe `0xfa342` first.
- **Assemble the capture pipeline** (the actual remaining work for pixels): SCNIO
  image-pipeline config (`0xc0d60000` family, live) + AFE serial power-on
  (`0xfa234`) + LDMAC channel setup (dest=our buffer, length, trigger) + BUFADM +
  lamp/exposure. For a stationary single line, test whether LDMAC+SCNIO can grab
  one line at the home position without motion.
- **Find the real carriage-motor block** BEFORE any capture that could move it.
  It is NOT in the `0xc0d5–0xc0d7` image blocks (name table = SCNIO/SUBS/EDIT/AVE/
  MAXMIN/SHDDEC/LUT1D/DPENC/LDMAC/BUFADM). It's a separate peripheral reached via
  the layered motion driver `0xf74fa`/`0xf7546`/`0xf7584` → `0xf7f40`/`0xf88aa`/…
  (object-based; MMIO base buried deeper). Deferred until a capture attempt.

## HARD RULES (read first)
- **Never flash stock firmware to the board.** Stock runs in the emulator ONLY.
  To check anything on hardware, add a probe to the app, flash the app, read the log.
- **Scanner work must never move the carriage motor.** The real motor block is
  **`0xc0c57000` (primary) + `0xc0c56000` (companion)** (verified at `f67e0`;
  IRQ source `0xc0133188`; init `c0c57000=0xa00d`; **start `c0c56000=0x102`**,
  stop `c0c56000=0`). **Never write `0xc0c56000`/`0xc0c57000` (or their sub-regs
  `c0c56100`/`c0c57400`/`c0c5711c`).** A stationary capture uses the stock
  no-motion branch (SCNIO `0xc0d6011c` bit0 = 0) and writes none of these.
- **All polls bounded** (timeout), so a non-completing engine can't hang the
  management loop (which must keep answering fwctl: INFO `0xC0/0x42`, REBOOT).
- **Guard any DMA:** only start a capture if the dest register (`0xc0d601c0+0x1c`)
  reads back our own buffer; else abort before `start` (see `scanner_capture`).
- **Emulate before every flash** (`app/tests/validate_app.py` in the `canonemu`
  docker image). **Do not git commit — the user commits themselves.**
- `pi` (GLM-5.3 CLI) is available for RE cross-checks but **hung 2 of 3 times** in
  the background here; if used, launch harness-tracked and monitor (elapsed vs CPU
  `TIME`). Prefer first-party RE with `fwtool.py`.

## OPEN QUESTIONS (prioritized — the actual blockers)
1. **Scanner clock-tree bring-up (THE blocker).** What enables the `0xc0133xxx`
   clock block? On our firmware `0xc0133100`/`0xc0133104` read `0` even after
   `|= 0x10`, so a *parent* power/clock enable is missing. Trace stock's platform
   init (`0x104318` → `0x113562` → `0x1168c8` region, and the scanner init
   `0xef868`/`0xfe420`) and the root clock controller `0xc0101xxx` for what gates
   `0xc0133xxx` and in turn `0xc0d60100`/`0x140`. Our bootloader's replicated
   early init is in `bootloader/include/board_values.h`; find what stock does that
   we omit. Goal: the exact ordered register writes so `0xc0d60130` (dpi) and
   `0xc0d60164` (line-count) latch. (Confirm on HW with probe `0x55`, extended.)
2. **DMA destination encoding.** `0xc0d601c0+0x1c` latched `0x23040` when we wrote
   `0x20023040` (dropped bit `0x20000000`) — it's a ~29-bit **offset**. Confirm
   the implied **base is `0x20000000`** (so offset = addr & 0x1FFFFFFF targets DRAM)
   and whether a separate base register exists. Needed before relaxing the dest
   guard to actually DMA into our `cap_buf`. Trace where stock's `0xffa18` dest arg
   originates (the line-buffer allocation) and `0xf3b16`.
3. **Timing (H/L) values.** The three 16:16 pairs at `0xc0d601c0+0x2c/+0x30/+0x34`
   (debug string `CIS(H=.. L=..)` @ `0xba6d8`). Where do they come from — constants,
   computed in `0xf6c7a` (64-bit muldiv), or the runtime dpi table `0x18030ef0`
   (RAM, not in the dump — may need a live read or tracing how it's populated from
   flash)? Give concrete values for one line at the lowest dpi (75).
4. **Mode semantics** of `0xc0d601c0+0x28` values `3 → 0x13 → 0x23` (retry/remainder),
   and which of `+0x20` vs `+0x24` is requested vs captured count.
5. **AFE config needed before capture?** The serial link `0xc0120000` (routines
   `0xf3732`/`0xf3138`, chip-type probe `0xfa342` sends `0x80`/`0x44`) configures
   the analog front end. Is any AFE serial setup required before the capture engine
   yields valid pixels, or is the engine self-contained once clocked?
6. **Lamp control.** Is `0xc0c85600+0x14=1` (scanner power) enough to light the CIS
   LED, or is there a separate lamp/LED-timing register (candidate bank `0xc0d60180`,
   `LEDTIME` string)? Needed for a non-black image.
7. **Minimal ordered capture sequence** for ONE stationary line: assemble answers
   to 1–6 into the exact register writes (clock tree → power → AFE? → dpi/line-count
   → mux → engine dest/count/timing → start → bounded poll → read DMA'd bytes).

## HOW TO DRIVE THE HARDWARE (the app's probe interface)
The app (`canon-mg2550s-firmware/app/`) exposes USB vendor requests. Build +
emulate + flash:
```sh
cd canon-mg2550s-firmware/app && python3 build.py
cd .. && docker run --rm --network none --entrypoint python3 -v "$PWD:/project:rw" \
  -w /project canonemu -u app/tests/validate_app.py        # must pass
python3 tools/fwctl.py flash app/build/application.bin
```
Trigger a probe and read the debug log (device `04a9:12fe`), single USB handle:
```python
import usb.core, usb.util, time, sys
dev = usb.core.find(idVendor=0x04a9, idProduct=0x12fe)
def drain(sec):
    out=bytearray(); end=time.time()+sec
    while time.time()<end:
        d=dev.ctrl_transfer(0xC0,0x50,0,0,256,timeout=500)   # 0x50 = drain log
        out += bytes(d) if d else b''
        if not d: time.sleep(0.05)
    return out
drain(1.2)
dev.ctrl_transfer(0x40, 0x55, 0,0, b'', timeout=1000)        # 0x55 = clkgate+enable+retest
sys.stdout.write(drain(2.0).decode('ascii','replace'))
usb.util.dispose_resources(dev)
```
Requests (all in `app/src/scanner.c`): `0x51` read-only register peek; `0x52`
engine config write-readback; `0x53` engine enable + scanner power + reprobe;
`0x54` guarded stationary capture (aborts unless dest latches); `0x55` clock-gate
+ enable + mux + re-test dark regs. Add new probes by following the same pattern
(pending flag → `scanner_poll` → `dbg_*` logging) and a new `0x4?` vendor request
in `app/src/usb.c`. Power cycle: `POST https://home.poet.cat/api/webhook/-oN-MSR_So4CVnI2WZ2XiL0Vp`.
Disassembler: `python3 fw_dump/gpt/fwtool.py dis <addr> <len>` and `refs <addr>`.

## WHAT'S RULED OUT (don't redo)
- The serial controller `0xc0120000` is **config only**, not the pixel path.
- Simple engine enable (`0xc0d60000=1`,`+8=1`) + scanner power + CIS mux does **not**
  wake `0xc0d60100`/`0x140` (tested on HW, probes `0x53`/`0x55`).
- `0xc0133100 |= 0x10` alone does not stick (that block is itself dark).
- The capture engine does **not** drive the motor (software-verified, see below).

---

## Stock-firmware RE (emulator/disassembly only — stock is never flashed)

The scanner serial controller base `0xc0120000` is held in stock global
`0x180d7f0c`; per-channel blocks are `base + ch*0x100`. Low-level routines
(`fw_dump/gpt`, via `fwtool.py`):

- `0xf31d2` — poll: read `[ch_block+0x10] & 1` (channel 1 → `0xc0120110`).
- `0xf31e8` — ack: write `0` to `[ch_block+0x10]`.
- `0xf31fa`/`0xf3208` — read data from `[ch_block+0x1c]` and `[ch_block+0x24]`.
- `0xf3732` — full byte transaction on the channel-1 block: writes `1` to
  `0xc0120108`, `0x21c` to `0xc0120114`; pulses strobe block `0xc0c840c4`
  (1 then 0); clears bits 8..9 of GPIO `0xc0c8121c`; shifts the command byte
  `((v<<4)|0x80)>>1` out.
- Chip-type probe `0xfa342`: send `0x80`, send `0x44`, read reply; high nibble
  `0x40`→type 0, `0x90`→type 1, else `0xff`. Stock's poll here has **no
  timeout** (the boot-hang-without-scanner bug) — our app always uses bounded
  polls instead.

## Hardware findings (our app, debug-log probes)

App `application.bin` with `src/scanner.c`; triggered over USB
(`0x40/0x51` peek, `0x40/0x52` active probe), read back over the debug log.

1. **Boot-time peek** — every `0xc0120000`-block register reads `0x00000000`.
   GPIO defaults: `0xc0c81200=0x00000fdf`, `0xc0c81204=0x00001020`,
   `0xc0c8121c=0x00000300` (matches `board_values.h`; stock clears bits 8..9 of
   `0xc0c8121c` at the start of a transaction, so `0x300` is the idle state).

2. **Active write-readback probe** — the writes **stick**:

   ```
   wr c0120108 <= 0x00000001  rd => 0x00000001
   wr c0120114 <= 0x0000021c  rd => 0x0000021c
   wr c012010c <= 0x00000001  rd => 0x00000001
   status 0xc0120110 => 0x00000000
   ```

   **Conclusion: the scanner serial controller is powered, clocked and writable
   from our application.** The boot peek read `0` only because nothing had
   configured it yet — not because the block is clock-gated. No separate
   clock/reset-enable hunt is required to start talking to it.

## Capture-engine live state (HW, non-actuating write-readback)

Write-readback of the `0xc0d60000` config holders with **no** scanner power, **no**
engine enable, **no** capture start:

| Register | wrote → read | verdict |
|---|---|---|
| `0xc0d601e8` (+0x28 mode) | `3` → `3` | latches |
| `0xc0d601e0` (+0x20 count) | `0x640` → `0x640` | latches |
| `0xc0d601ec` (+0x2c timing) | `0x12345678` → `0x02345678` | latches; **28-bit** (top nibble masked) |
| `0xc0d601dc` (+0x1c dest) | `0x20300000` → `0` | not retained |
| `0xc0d60164` (0x140 line-count) | `0xff00` → `0` | sub-block dark |
| `0xc0d60130` (0x100 dpi) | `0x20` → `0` | sub-block dark |

**Conclusion:** the engine core `0xc0d601c0` is powered/clocked (mode/count/timing
latch), but the `0xc0d60100`/`0xc0d60140` config sub-blocks are dark and `+0x1c`
won't hold a plain write. A bring-up/enable sequence is needed before a capture.

### Bring-up attempt (HW) — simple enable is INSUFFICIENT

Wrote engine enable `0xc0d60000=1` (latches ✓) and scanner power `0xc0c85614=1`
(latches ✓; `0xc0d60008` ack self-clears), 10 ms settle, then re-tested:

```
c0d601dc (+0x1c dest)      <= 0x20300000  rd => 0  (still dark)
c0d60164 (0x140 line cnt)  <= 0x0000ff00  rd => 0  (still dark)
c0d60130 (0x100 dpi)       <= 0x00000020  rd => 0  (still dark)
```

So enable + scanner power alone does **not** wake the `0xc0d60100`/`0xc0d60140`
sub-blocks. The full stock power-on/clock sequence must be traced — candidates:
a peripheral clock gate (clock controller `0xc0101xxx`), the CIS clock/timing mux
`0xc0d60280..0x2c0`, and the stock power-on `0xfa33a`→`0xfa2d0` (which also writes
AFE regs over the serial link) and engine-start `0xf6c7a`. **A full scan pass is
not attempted until these sub-blocks come live and `+0x1c` latches.**

## Motor-independence safety analysis (for a stationary capture)

The user's gate: a stationary capture must NOT move the carriage motor. Evidence
that it cannot (static RE of the scanner driver region `0xf2b00..0x102000`):

- **Motor = dedicated blocks `0xc0d60400` / `0xc0d60440`** (and `0x4c0`): confirmed
  by a cluster of 16-bit PWM-field setters at `0xf4410`–`0xf4bxx` (the only code
  that references these addresses — 25 + 33 literal-pool refs, all from that
  cluster's pools `[0xf4680]=0x…440`, `[0xf4be8]=0x…400`).
- **Capture engine = `0xc0d601c0`** (refs only in `0xf39e8`–`0xf3axx`) and the
  per-block capture `0xffa18`. Disjoint address range from the motor blocks.
- **Motion is programmed explicitly**, separately from capture: the scan loop
  `0xf6aaa` builds a motion profile via `0xf7584/0xf7546/0xf74fa` → the motor
  PWM blocks. The capture engine is **not** observed to write `0x400/0x440`.
- A stepper only moves when its phase/PWM registers are driven. Our capture code
  writes **only** `0xc0d60000/0x80/0x100/0x140/0x180/0x1c0/0x280/0x2c0` and
  scanner power `0xc0c85600+0x14` — **never** `0x400/0x440`. So it cannot command
  carriage motion **by construction**.
- **The capture function `0xffa18` is motor-free in software**: its body writes
  only `0xc0d60080`, `0xc0d602c0`, and `0xc0d601c0` (`[r7+0x14/0x1c/0x20/0x28/
  0x2c/0x30/0x34]`) and calls only colour-setup `0x151efc`, delay `0xf7394`,
  poll `0xf2a98`, status `0xf2aa4`, error/log `0xf5838/0x1085be`. **No motor
  reference, no call into the `0xf4xxx` motor cluster.**
- Residual (hardware) question: whether starting the engine auto-couples to the
  motor. Architecture argues no — the engine *consumes* motion timing, it does
  not *create* it, and a stepper needs its coil/PWM registers driven to move.
- Any wait-for-motion the engine does is covered by a **bounded poll timeout**:
  worst case is a stale/blank line, never motion.

**Safety verdict (confident):** a stationary capture that writes only capture-path
registers (never `0x400/0x440`) leaves the carriage still — by construction and
by the evidence above. (The intended `pi` cross-check hung and was abandoned;
this verdict is from first-party RE.)

### Functional readiness (separate from safety) — NOT ready for real pixels

The dpi (`0xc0d60100`) and line-count (`0xc0d60140`) sub-blocks are **genuinely
dark**: their setters `0xf423a`/`0xf4254` are plain RMW (`ldr; mask; orr; str`),
so the registers are normal R/W and *should* read back — but our direct writes
did not stick, meaning the sub-blocks are not clocked/enabled. The `0xc0d601c0`
core is live (mode/count/timing latch); `+0x1c` dest is a special RMW pointer
(bits 0–19) that need not read back.

Stock builds scan config in **RAM shadow objects** (`0x180d7d70`, `0x180d7d80`)
via `0xf285e`/`0xf2850`/`0xf286c`/… then flushes to hardware through a commit
path; the sub-block clock/enable is in that path and is **not yet located**
(a deeper multi-layer trace). **Consequence: a stationary capture run now is
SAFE but will very likely return blank/zero data until the enable is found.**

## Stationary capture — option-1 hardware result (SAFE abort)

A guarded capture (USB `0x54`): writes only capture-path registers, targets our
own `cap_buf`, and starts the engine ONLY if the dest register latches to that
buffer. On hardware:

```
dest wrote=0x20023040  read=0x00023040   -> abort before start (no DMA, no motor)
```

New facts learned safely:
- With the engine enabled (`0xc0d60000=1`), `+0x1c` (dest) **partially latched**:
  it kept `0x23040` and dropped only bit `0x20000000`. So `+0x1c` is an **offset
  register** (~bits 0–28) with an **implied DRAM base ≈ `0x20000000`**, not a full
  address. (Earlier, without the enable, it didn't latch at all — the engine
  enable changes register behaviour.)
- The guard worked: because the full address didn't read back, no capture was
  started and no DMA occurred. Carriage never moved (no motor register written).
- The `0xc0d60000` **control block** has its own registers at `+0x18..+0x34`
  (accessors at `0xf3910`+), separate from the `0xc0d601c0` capture engine. The
  dark pair remains `0xc0d60100` (dpi) and `0xc0d60140` (line-count).

Open: the exact clock/enable for the dpi/line-count sub-blocks, and confirmation
that the `+0x1c` DMA base is `0x20000000` (needed before relaxing the dest guard
to actually run a capture to `cap_buf`).

### The dark sub-blocks need a clock-tree chain (HW, option 2)

- Found the sub-block clock gate in stock: bit `0x10` of `0xc0133100` (set at scan
  start `0xf73ac`) and `0xc0133104` (set in scanner init `0xfa5c2`).
- But on our firmware those gate registers are **themselves dark**: after
  `0xc0133100 |= 0x10` / `0xc0133104 |= 0x10` they still read `0`. So a **parent**
  clock/power enable for the `0xc0133xxx` block is also missing. Engine `0xc0d60000`,
  mux (`0xc0d60294=0x1111`, `0xc0d602c0=0x7`) and engine `+0x1c` offset remain the
  only live parts; dpi `0xc0d60130` / line-count `0xc0d60164` stay `0`.
- Interpretation: our minimal bootloader brings up CPU/DRAM/USB/flash clocks but
  **not the scanner clock tree**. Fully waking the CIS needs replicating stock's
  scanner clock-tree bring-up (`0xc0101xxx`/`0xc0133xxx` parents → `0xc0d60100/0x140`)
  — a defined but multi-step clock-tree trace. **No safety change: still
  motor-safe, no flash writes; the capture just stays blank until this is done.**

## Pixel capture path (RE, verified on disassembly — NOT the serial link)

A deeper RE pass (GLM-5.3 via `pi`, then spot-verified against the dump at
`0xffa18`) found that **pixel capture is a different peripheral from the
`0xc0120000` serial controller**. The serial link only does AFE/register config
and the chip-type probe (8-byte transactions); no image data flows through
`0xc012011c/0x24`. Pixels come from an undocumented capture engine:

| Register | Role | Confidence |
|---|---|---|
| `0xc0d601c0+0x14` | capture start (write 1), poll bit0==0 until done | high |
| `0xc0d601c0+0x18` | result status; bit31 = OK | high |
| `0xc0d601c0+0x1c` | **capture destination RAM pointer** (engine DMAs here) | high (addr), DMA=inferred |
| `0xc0d601c0+0x20` | count/limit param | medium |
| `0xc0d601c0+0x24` | progress / captured count (bits12–31) | high |
| `0xc0d601c0+0x28` | mode: 3 → 0x13 → 0x23 (retry/remainder) | high (values) |
| `0xc0d601c0+0x2c/+0x30/+0x34` | three packed 16:16 timing pairs (`CIS(H=.. L=..)`) | high |
| `0xc0d60100+0x30` bits5–8 | dpi index into runtime table `0x18030ef0` (RAM) | high |
| `0xc0d60140+0x24` bits8–31 | total line count − 1 | high |
| `0xc0d60080+4` | cleared before each block | medium |
| `0xc0d60000`,`+8` | enable/ack (=1 start; `+8`=0x80000000 ack) | medium |
| `0xc0d60180` bank | lamp / AFE enable + gain (inferred) | medium |
| `0xc0d60280..0x2c0` | CIS clock/timing mux; per-colour RMW; `+0x294`=0x1111 | medium |
| `0xc0d60400/0x440` | motor/PWM compare (carriage/feed) | low |
| `0xc0c85600+0x14`=1 | scanner power on (`0xfa33a`) | medium |

One-block acquisition core `0xffa18` (per colour plane), verified structurally:
clear `0xc0d60080+4`; per-colour mux setup; write mode `+0x28`, dest `+0x1c`,
count `+0x20`, three timing pairs `+0x2c/+0x30/+0x34`; enable `0xc0d60000/+8`;
start `+0x14=1`; poll bit0; ack `+8=0x80000000`; read status `+0x18` bit31;
read progress `+0x24`, re-arm with mode `0x23` for the remainder. **No CPU
read-loop — the engine writes pixels to RAM itself.** Engine start/geometry in
`0xf6c7a` (line count, dpi, buffer sizes via 64-bit muldiv).

Full RE notes (facts vs inference, open questions): `fw_dump/gpt/scanner_re_pi.md`.

## Next steps

Target the capture engine `0xc0d601c0` directly (the serial-link chip-type
transaction is deprioritised — it is config, not pixels):

1. **Peek the `0xc0d60000` family** read-only first (extend `scanner_peek`) to
   see the live reset/enable/timing state on hardware.
2. **Bounded one-block capture PoC**: minimal AFE/lamp enable + scanner power
   (`0xc0c85600+0x14=1`), program `0xc0d601c0` (mode `+0x28`, a RAM dest buffer
   at `+0x1c`, count, timing pairs), set `0xc0d60000/+8=1`, start `+0x14=1`,
   poll bit0 **with a timeout**, read status `+0x18`. Dump the DMA'd bytes over
   the debug log and compare against `fw_dump/scan_ref.jpg` (an invoice on the
   glass — verify structurally, do not copy its personal data).
3. Iterate: dpi/line-count regs, flat-field calibration, then motor stepping for
   a full page.

Safety invariant: all scanner code stays **bounded** (timed polls) and never
blocks the management loop, and writes no flash — so a wrong guess cannot brick
the board. Motor (`0xc0d60400/0x440`) stays untouched until a single home-row
line is proven.

## Hardware notes

- The board dropped off USB once after rapid control-transfer open/close cycles
  (host-side glitch; recovered by replug, firmware was fine). Host log/probe
  tooling now reuses a single USB handle per run.
