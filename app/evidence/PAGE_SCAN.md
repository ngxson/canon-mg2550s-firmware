# Full-page scan PoC — 2026-10-24

The user requested a page scan using the working home, forward-motion, capture
and illumination paths, and confirmed the paper was ready. The installed app
now joins those operations into a stop-and-capture sequence. **The physical
full-page scan completed successfully:** 880 rows captured, a recognizable
image saved, and the scanner returned home. Total host duration was 329.461 s.
The motor and illumination are off, and application USB INFO remains valid.

[Physical result and output hashes](page_v2/physical_scan_summary.json):

| Measurement | Result |
|---|---:|
| Raw image | 2,592 × 880 samples, 16-bit |
| Aspect-adjusted preview | 638 × 880 pixels |
| Completed rows | 880 / 880 |
| Row position error | 0..42 encoder counts |
| Maximum motion during capture | 1 count |
| Returned position relative to initial home | +26 counts |
| Final firmware state / error | 7 (done) / 0 |
| Original raw samples vs exported PGM | Identical |

Visual review confirms document text, page edges and layout are present. No OCR
or transcription was performed. The image remains an uncalibrated grayscale
PoC; the limitations below still apply.

## Installed build

Page v2: **31,008 bytes**, SHA-256
`28c045e55c74fec907f824c8f5b12d7fb3f3a77993c97cf5ecba11157c97605f`.
[Manifest](page_v2/manifest.json), [protected USB flash](page_v2/flash.txt),
[page state-machine validation](page_v2/validation_page.json).
The upload returned C0 and valid application INFO. Both bootloader stages remain
protected. No stock executable code, whole-flash update, ESP32 access or power
cycle was used. The app continues to use the previously verified mechanical
[homing controller](HOMING.md).

## Sequence and data ownership

1. Mechanically home using left seek, verified backoff and repeated left seek.
2. Capture the first row at the mechanical reference. Subsequent rows move to
   `origin + row * pitch`, so coast does not accumulate across steps.
3. Use the stock-derived positive-backoff PI controller, with speed capped at
   4,000 counts/s. Each forward step is at most 1,024 counts and has a 750 ms
   deadline. Motor drive is disabled before the 30 ms settle and capture.
4. Run the stationary capture with optical profile 0, AFE setup and scanner power.
   The tested 80 ms acquisition/drain wait remains; read-count alone is not
   treated as a DMA completion fence. Both DMA segments must contain new data,
   both channel events must be present, DMA addresses must read back correctly,
   and the allocation guard must remain intact.
5. Hold the row unchanged while the host reads it. Only an ACK after all chunks
   have been read permits the next move. A repeated ACK does not skip a row.
6. After the final acknowledged row, run mechanical homing again and switch off
   illumination. Report completion only after this return succeeds.

Cancel stops the whole operation, including while waiting for the host. A
15-second host timeout leaves the motor and lamp off. Firmware has a 15-minute
whole-operation deadline; each home has a 30-second orchestration deadline.
Wrong position, motion during capture, failed motor travel and capture failures
stop the pass. USB management remains live between bounded stationary captures;
the capture itself occupies about 110 ms with motor off. I²C is polled whenever
the motor is idle. Independent captures, jog/home commands and print transfers
are rejected while a page owns the scanner. Copy keys are ignored during a pass;
Cancel or both Copy keys abort it. No autonomous scan occurs on boot.

## Optical profile correction

The first integration used the older, physically exercised **profile-7
diagnostic** capture. It produced 149 saved rows with nearly uniform sensor
offsets/noise, despite successful forward motion, fresh DMA and transfer. It was
intentionally stopped to correct the optical configuration. A concurrent DATA
read then stalled, recorded as a host Pipe error. That pass is incomplete, not a
successful image: [v1 report](page_v1/scan.json).

V2 uses the stock-derived **profile 0** SCNIO sequence: ordered value substitutions
and six inserted exposure writes from `scanner_recipe_codex.md` B.2, at their
original positions in the write sequence. The old diagnostic profile and
illumination experiment remain available separately. Optical changes include:

- 2,592 halfword samples, in two 1,296-sample segments, instead of 3,072.
- Corresponding LDMAC destinations, segment dimensions and buffer limit.
- Optical input crop, waveform words, routing, exposure intervals and period.
- Existing type-0 AFE register configuration; no diagnostic root-clock switch.

These literals come from analysis of unchanged stock SHA-256
`4b032367cff5976aeb426c8723e08d3c431a38e622e245a614791a3e5067e533`.
The stationary optical combination is a replacement-firmware adaptation. It is
not a replay of stock's complete calibrated scan job.

## Host command and output

From the public firmware directory:

```sh
python3 app/tools/scan_page.py --out scans/my-page
python3 app/tools/scan_page.py --stop
```

Default geometry: **2,592 samples × 880 rows**, 512 counts between rows. Stock's
nominal 38,400 counts/inch gives 75 rows/inch and 297.688 mm between first and
last rows. Exact document origin and transverse sample geometry are uncalibrated.

- `.bin`: untouched little-endian 16-bit samples, rows in acquisition order.
- `.pgm`: untouched 16-bit samples in PGM's big-endian encoding.
- `-raw.png`: contrast-stretched 8-bit preview at native sample dimensions.
- `.png`: same preview resized horizontally to an assumed 216 mm sensor span.
- `.json`: every captured row's encoder start/end positions, state changes,
  completion/error status and image statistics.
- `.log`: physical firmware log.

The tool rejects an existing raw output filename, checks row identity and byte
count before ACK, saves partial images on failure, and sends STOP in cleanup.
`--rows 32` captures a short strip; `--pitch` controls nominal row spacing.
Images of the user's document are kept in the private workspace scan directory,
not copied into this public implementation's evidence folder.

## Validation

Before flashing v2, all six actual ARM emulator scripts passed:
`validate_page.py`, `validate_home.py`, `validate_app.py`, `validate_motor.py`,
`validate_buttons.py`, and `validate_scanner.py`.

Page tests exercise four distinct synthetic rows, their exact transfer bytes,
short final chunk, rejected early ACK, repeated DATA/ACK, absolute positions,
return home, missing DMA, abandoned host, stuck forward motion, Cancel during
motion/waiting, conflicting requests and management reboot. Home, jog, buttons,
legacy capture and recovery regression tests pass on the exact installed binary.
Models provide scripted position feedback and synthetic optical data; they do
not prove motor physics or image quality. Those require the physical scan.

Host export was checked by round-tripping synthetic 16-bit values through PGM
and verifying the raw file remained unchanged.

Remaining limitations: uncalibrated optical origin and shading, no colour
reconstruction, possible highlight clipping, approximate preview aspect ratio,
and low travel-axis resolution. This is a useful page-image PoC, not a finished
scanner protocol or calibrated production scan mode.
