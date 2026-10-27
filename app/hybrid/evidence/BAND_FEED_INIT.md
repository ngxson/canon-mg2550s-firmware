# Raster-feed initialization missing from the hybrid

2026-10-26. This is a measured stock/hybrid difference and a candidate fix
for the blank band passes. Ink output has not yet been verified with this fix.

The unmodified stock boot executes `0xe5ba4`, initializing the raster-feed
block before any pass. The hybrid copied stock's RAM state but omitted these
peripheral writes. The executor configures the pass and starts the block; it
does not supply these boot defaults.

| Register group | Stock boot | Installed hybrid, read-only USB capture |
|---|---|---|
| `28180`, `28184` (all addresses prefixed `0xc0c`) | `0x1f`, `0x1f` | Both zero |
| `28300..28324` | `0,40,80,120,160,200,240,280,0,0` | All zero |
| `28400..28424` | Eight entries of 40, then two zeros | All zero |

The initializer first writes `28000=0`: run it while preparing a new band,
before the executor starts the feed block. The units and electrical meaning
of these fields remain unverified; the patch reproduces the observed stock
values and ordering without assigning them a physical nozzle interpretation.

`band_feed_init()` in `app/src/stock_shim.c` implements those 23 writes in C
at the start of `band_prepare()`. This adds no borrowed binary routine. The
existing allocator, descriptor builder, executor and motion routines remain
stock dependencies; this is still hybrid firmware.

Evidence:

- [Unmodified stock boot capture](band_feed_init/head_boot_reference.json):
  boot to tick 300, readiness level 4, no stock instruction/RAM patches.
  This is boot initialization evidence, not an emulated successful print.
- [Compiled hybrid before the fix](band_feed_init/head_hybrid_baseline.json):
  none of these initialization registers written before CR submission.
  [Emulator control using stock's initializer](band_feed_init/head_hybrid_fixed.json)
  restores them. Those small probes did not model successful head acquisition.
- [Compiled-code comparison](band_feed_init/band_feed_init_validation.json):
  candidate `1bafb32b…6e46` emits exactly the same 23 ordered MMIO writes as
  unmodified stock `0xe5ba4`. Old package `55c77775…1d82` emits none at the
  same preparation boundary. Only `stock_shim.c` changed among firmware
  sources; the candidate's first 64 KiB match the previous build exactly.
- [Hardware before the fix](band_feed_init/hardware_before.json): direct USB
  reads confirm all these initialization words are zero on the installed
  hybrid. No peripheral write, motion or firing was requested by this capture.
- The existing full hybrid validator reaches `PASS hybrid` with the candidate.
  Its stock-CR case deliberately lacks physical movement and still reports
  modeled wait errors; this does not certify the first electrical pulse.

Reproduce the compiled-code check (inside the project's emulator container):

```sh
python app/hybrid/validate_band_feed_init.py --build PATH_TO_NEW_BUILD \
  --baseline PATH_TO_OLD_BUILD --output band_feed_init_validation.json
```

The canonical `app/hybrid/build` now contains the tested fix; the old build
is preserved for this session at `/tmp/canon-band-feed-baseline`. To capture
the original stock boot again, use `emu/run.py boot --eeprom
emu/inputs/eeprom.bin --ticks 300 --budget 12000000 --output stock_boot.json`
in the emulator container and inspect its MMIO write history.

## Planned hardware confirmation, recorded before the run

1. Install the tested app package with the normal protected USB updater.
2. Prepare a fresh band and read the three groups above. Require an exact
   match before the executor runs. This is the predicted register change.
3. Use the existing stock loader/CR/executor/eject sequence for one labelled
   sheet. Preserve its trace and the post-pass registers; record whether ink
   actually appears. A successful return or a motion event alone is not ink.
4. If still blank, keep this confirmed initialization fix, but do not call it
   the complete cause. Return to reference comparison before another change.

## First hardware attempt: loader stopped before band preparation

Package `1bafb32b…6e46` was installed with protected USB update status `C0`.
The CLI's 15-second app wait expired, but the app subsequently enumerated.
With the user's sheet ready, `band_live.py --label INIT40` homed and read both
cartridges, then stopped because the stock loader left ASF state 0. It had
issued one CR request and **no LF request**. No band was prepared or fired.
See [pass log](band_feed_init/hardware_pass.log),
[loader trace](band_feed_init/loader_stopped.json) and
[flash log](band_feed_init/flash.log).

The first two CR profile records were entirely zero on hardware
([readback](band_feed_init/cr_profiles_before.json)); the sparse snapshot
contains none of the 14-record table at `0x18089900`. Its initialization flag
at `0x1807b0fe` is also zero, not a false "already initialized" flag.
The direct move route does not run stock's separate `0xd3e06` initializer.
The previous shim copied this ROM table **only in `band_run()`**, so the
first paper load after a reset could use empty profiles.

The follow-up moves that existing copy into `stock_ram_init()`. The new
validator checkpoint stops after RAM restoration and compares all 14 records
with ROM before any loader/band call: package `1bafb32b…6e46` fails this
check, while candidate `c7565b48…2c8ce` passes it. This is a separate startup
prerequisite, not evidence yet that the raster initializer produces ink.

## Hardware pass with both startup fixes

Installed `c7565b481ac27d708673fe2d5962140b06df96308b622b235158d303baf2c8ce`
with protected-update status `C0`. Before any motor request, a stage-1 RAM
restore and USB readback verified **all 3472 profile bytes** against stock
ROM ([readback](band_feed_init/hardware_cold_profiles.json)).

`band_live.py --label INIT40` then completed load, band, eject and home
([log](band_feed_init/hardware_pass_profiles.log)):

- Loader issued the expected 2050-Ulf pickup, 12950-Ulf search and 646-Ulf
  alignment; ASF reached 1. No open-loop paper-feed routine was used.
- The new host preflight verified the raster-feed defaults before upload
  and execution. All 25,600 uploaded bytes matched on readback.
- Pre-sweep owner-3 checkpoints held: state 4, owner 3, `87000=1`,
  `812c0=1`, `295f0=8`, `51400=1e`, `28000=1`, lane enables 3.
- Stock CR moved raw `19c0 → ed00` in 1247 ticks; executor returned 1 with
  no exception. The existing empty-mailbox/owner-wait errors remain (three
  cumulative errors); they are not relabeled as successful handshakes.
- After the pass, eject and home, both BK lane pointers `27000/004` and
  `27200/004` read **`202164c0`**, exactly **`B + 0x6400`** from uploaded
  `B=202100c0`. This is consistent with traversal of all 20 raster tiles,
  and narrows the DMA investigation. It does not by itself prove valid
  electrical firing or visible ink. All restored defaults persisted.
  [Post-pass capture](band_feed_init/hardware_after.json).

**User confirmed this sheet is completely blank.** The startup fixes and
pointer progression are real, but do not resolve firing. The carriage is
home and the sheet ejected. Further work must distinguish selected raster
data from head output before another paper pass.

## Counter diagnostic and planned same-sheet control

After the blank INIT40 pass, stock's `c5820` counter collection was executed
in emulation: its only peripheral writes select one of eight counters and
latch/reset it. The host reproduces that sequence; it does not arm the head
or move a motor. [Reference](band_feed_init/counter_reference.json).
Idle hardware returned **4140,4140,0,0,0,0,0,0**, then all zeros on the next
collection ([capture](band_feed_init/hardware_idle_counters.json)). There was
no before-pass baseline, so these counts cannot be attributed solely to
INIT40. The current bootloader disables CPU caches. The consumer B points
to raster tiles, not embedded-pointer descriptors; brief 19 is corrected.

Next diagnostic, recorded before execution: unchanged installed `c7565b48`
firmware, label `CNTCTRL`, stock load/CR/band/eject, with counter collection
immediately before and after the band. Then run the previously physically
qualified stationary black ID-24 prefire at the stopped carriage position
(two records, 100 pulses each), collecting its counters separately. A mark
from that control is **not** success of the moving band. If the control
prints and the band does not, paper/head output works at that location and
the band path remains the focus. If neither prints, check placement/head
output rather than treating nonzero ASIC counters as ink. No waveform or
firmware change is part of this test. The installed binary's stationary
sequence passed the existing emulator oracle (141 ordered engine writes,
two modeled completions); [validation](band_feed_init/control_mark_validation.json).

### CNTCTRL result and confirmed placement error

The diagnostic completed stock load, band, stationary control, eject and
home without exceptions. [Counters and control status](band_feed_init/hardware_counter_control.json),
[log](band_feed_init/hardware_counter_control.log).

- Before the band: 4000 per BK lane (activity from preparation/loader; not
  treated as a print count). The counter collection cleared that baseline.
- Band alone: **244 per BK lane**, colour zero. The uploaded CNTCTRL image's
  first eight storage rows contain exactly **244 even-row bits and 244
  odd-row bits**. INIT40's corresponding populations are 140/140, consistent
  with the earlier unbaselined 4140/4140 capture. This is strong evidence
  for valid raster reads in the existing `2021xxxx` allocation, not just
  pointer increments. It also shows that this selection covers only eight
  raster rows; the full uploaded label is not selected.
- Stationary control alone: **16800 per BK lane**, colour zero, result 2,
  two completed records, 16 ms per last record, at carriage x=57984.
- **User confirmed the entire sheet remains blank.** Electrical pulse shape
  and ink ejection still cannot be inferred from those counters.

A subsequent stock load-and-hold, without a print request, completed
([trace](band_feed_init/hardware_load_hold.log)). The user inspected the
stopped sheet and confirmed **its leading edge is short of the carriage**.
This is a physical placement failure, not an emulator assumption. The user
also requested trying more advance. The sheet is held inside while a bounded
stock-LF advance command is being validated; the next mark must follow a
physical check that paper has reached the head.

### Bounded advance candidate, before hardware execution

Routine 10 adds one forward request through existing stock resource/init,
`d614c(distance,6,0,0x20)` and `d707c` completion. The host supplies 1..1200
Ulf; live operation requires paper at PE. It does not submit a CR move or
head work. The full hybrid validator passes, including invalid-length
rejection, exact stock call arguments at 200/1200 Ulf, refusal without paper,
no new head-work arms, existing cold profile checks and band checks. Travel
is not established by that dry probe. Both bootloader stages compare equal
to the installed package. [Validation](band_feed_init/advance_validation_summary.json).

Planned first physical step: **200 Ulf** on the held sheet, profile 6,
direction 0. The request means **1200 stock encoder counts**; physical mm
remain to be checked. Capture raw position before/after and ask the user to
check whether the leading edge reaches the carriage before any further mark.

### Advance installed and first physical step

Installed **`38eb38c397afba695966471e3e10fe12f236c9924ef036c7a6763ebebf66a802`**,
protected update `C0`; canonical build matches. After restart PE initially
reported empty, and the host submitted **no step**. The user confirmed the
sheet had not moved. Normal carriage-home initialization changed PE from 1
to 0 without any LF command ([capture](band_feed_init/hardware_paper_preflight.json)).
This shows that a raw PE check immediately after reboot is insufficient
before the normal shared initialization. No paper-presence guard was bypassed.

The first **200-Ulf** forward step then completed with result 0, **zero
errors/exceptions**, and 82 stock LF ticks. Raw encoder went from `700000`
to `7004b4` (**1204 counts**, requested 1200); PE remained present. No band
or stationary work was requested. [Trace](band_feed_init/hardware_advance_200.log),
[firmware log](band_feed_init/hardware_advance_200_firmware.log).
The shim's generic `run routine 10 dry` label refers to its CR-live flag;
the LF trace correctly reports stock LF live motion. Visual position check
is pending before a mark.

The user saw **no paper movement** after the 200-Ulf step, despite the motor
encoder changing. They felt the earlier carriage motion and requested the
advance on a Copy button. Do not label the sheet as successfully advanced.
The candidate panel map uses Black Copy for one 400-Ulf stock LF step per
press, with no carriage homing, no repeat while held, and no head work.
Cancel stops the stock LF timer (`dbef6`) before stock neutral/disable
(`d8022`) and releases the wait as a cancelled result. The first panel press
performs the existing motor-off shared setup so a reboot does not require
carriage homing just to restore the PE input. This is a hybrid-only map;
the standalone app is excluded by its link address and snapshot signature.

Panel candidate **`5ab4404c2c3efee5480b7c5eabc7682d2e9faeafa73085d7149e1b8a28b66570`**
passes the full hybrid validator. Its panel cases execute actual debounced
key presses: one `d614c(400,6,0,0x20)` request, no repeat while held, then a
second press cancelled after its first servo tick. Cancel leaves the stock
LF timer stopped, command neutral and drive disabled; no CR request or head
work is emitted. The emulator's simple LF model still incurs its existing
motion/wait errors, so these are software-contract checks, not sheet-motion
validation. [Panel results](band_feed_init/panel_validation_summary.json).
The first cancellation test needed a fresh modeled held sheet after the
previous modeled move cleared PE; the guard correctly rejected that attempt.
The firmware did not change to bypass the guard.

The protected 64-KiB boot region compares byte-for-byte equal to the previous
candidate. Installation is in progress. No automatic paper movement or
firing is queued after restart; the user will operate Black Copy.

The panel package is now installed (`C0`) and running. Startup USB readback
confirms **map 2** and the Black Copy stock-advance binding
([ready capture](band_feed_init/panel_ready.json)). No movement/firing was
requested after reboot. The user has been told to press/release Black Copy,
wait for the step, and repeat as needed; Cancel stops. Physical button/sheet
feedback is pending. The original print goal remains unmet until visible ink
is confirmed.

### Paper beneath the carriage: held-sheet test

The user confirmed **"Paper is beneath the carriage"** after operating
Black Copy. The firmware recorded two 400-Ulf stock LF requests, both result
0 with zero errors/exceptions, no CR request and no firing. Encoder deltas
were 2401 and 2400 counts; those readings accompany, rather than replace,
the user's physical observation. The read-only preflight found PE present,
both motor drives disabled and no movement button held.
[Preflight and button trace](band_feed_init/hardware_placed_preflight.json).

The `PLACED` pass uses the existing installed and validated panel package:
home, head prepare, fresh band prepare, upload, band, then the same bounded
two-record stationary black control. `--no-feed` preserves sheet placement;
stock eject follows separately. No firmware or pulse parameters change.
Predicted observables: the band counters should match the selected raster
rows, the control should complete two records with its previous counter
signature, and physical inspection determines whether either leaves ink.
The paper placement correction is not yet a calibrated first-line solution.

The pass completed: [test log](band_feed_init/hardware_placed.log),
[counters/control status](band_feed_init/hardware_placed.json).

- Uploaded 25,600 bytes with no readback mismatches. The `18210580` host
  readback address aliases the `20210580` DMA allocation.
- Band result 1, no exception, **208/208** BK counts and zero colour counts.
  These exactly match PLACED's even/odd bit populations in storage rows 0..7.
  The existing owner wait `1b:1` timed out once; it remains unresolved.
- Stationary control result 2, two completed records, **16800/16800** BK
  counts, zero colour counts, carriage x=57984; last record 16 ms.
- Home completed. Separate stock eject returned 0, with no additional
  error or exception. Final PE is empty and CR/LF drives are disabled.
  [Eject trace](band_feed_init/hardware_placed_eject.log),
  [final readback](band_feed_init/hardware_placed_postflight.json).

The user has been asked to inspect this sheet. Visible ink remains unverified;
the counters must not be substituted for the physical result.

### PLACED scan: physical ink confirmed

The user requested a scanner check and confirmed the test sheet was face
down on the glass. The installed application's existing 75-dpi grayscale
scan completed. This was an optical capture of the physical sheet, not a
rendered raster. [Full scan](band_feed_init/hardware_placed_scan.png),
[enlarged crop](band_feed_init/hardware_placed_scan_crop.png),
[measurement report](band_feed_init/hardware_placed_scan.json),
[scan log](band_feed_init/hardware_placed_scan.log).

- A strong black vertical mark measures approximately **1.35 × 15.24 mm**,
  centred at **81.25, 25.27 mm** from the scan origin. It is consistent with
  the stationary control's black nozzle span; the two records at the same
  carriage position do not imply two separately resolved marks.
- A separate tiny segmented horizontal strip appears at scan row 71,
  around columns 193..209, with an isolated dot at column 231. Its layout is
  consistent with the top of the PLACED raster and its right-hand marker.
  Together with the exact 208/208 band counts, this supports actual moving
  band firing on paper. It is not a complete readable label or validation of
  all nozzle groups. The 75-dpi image resolves this strip as only one row.
- The default mark detector reports one mark because it deliberately rejects
  one-pixel-thick components. Visual inspection of the original and crop
  confirms the separate strip. Faint background lines/shadows are not
  attributed to this pass.

**The first-ink goal is met.** The placement correction produced ink with the
same band/control pulse parameters and head power preparation as CNTCTRL;
the intervening firmware additions were the bounded LF advance and panel
controls. The old blank sheets therefore cannot justify
increasing heater drive. This does not prove that every remaining band issue
is solved: fix the loader's first-line placement, extend the raster selection
beyond eight rows, verify geometry, and resolve the owner wait separately.
The scan is uncalibrated and the physical dimensions are approximate.

No further print/advance is queued. The sheet is on the scanner glass. The
hybrid still depends on the documented stock LF/CR/band routines; this result
is an intermediate milestone toward their source replacements.
