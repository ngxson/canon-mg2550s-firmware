# Stock message-ID rings during scan and print — 2026-10-25

First captures from telemetry ABI 2 (package SHA-256
`aa7ac52fe77712cf15f34174db7f1cc2008a91a866c1ad0299dc688082f6008f`, installed
21:41 UTC; see [DEPLOYMENT.md](../DEPLOYMENT.md)). Regions 5 and 6 expose the
stock recorder's rings of 256 16-bit message IDs; `tools/stocktrace.py
--messages` samples them and `tools/stockmsgs.py` turns consecutive snapshots
into the IDs written between them. All files here were polled at about 5 Hz
(each snapshot is 20 control reads, i.e. 80 of the printer's own messages),
no snapshot overflowed the 256-entry ring, and USB state stayed `[4,1,1,1]`
with native error zero. Provenance: [manifest.json](manifest.json).

| Capture | Stimulus | IDs decoded |
|---|---|---|
| `idle_messages.jsonl` | 2 s idle | only telemetry's own pattern |
| `messages_during_scan.jsonl` + `scan_during_messages_trace.jsonl` | 50 × 50 mm, 150 dpi scan | 7,958 |
| `messages_during_print.jsonl` + `print_geometry_trace.jsonl` | `geometry_sheet.png` at 600 dpi | 30,734 |

## Patterns

- **Telemetry's own request:** `f01f f022 f023 f021`, four per control read.
  This is the vendor SETUP path; every snapshot cost shows up exactly.
- **Scanner bulk command round trip:** `f05b f05c f00e f05d f05e f00a`, six
  per command, with one extra `f05e` per additional 8 KiB IN chunk while image
  data streams. `f05b/f05c` follow the bulk OUT, `f05d/f05e` the bulk IN.
  207 round trips matched the host's command count; the per-second rate
  tracked the host's `0xd420` polling (up to 37/s).
- **Scan session start** (after `StartJob`/`ModeShift`/`0xdb20`): `f005 f026
  f044 f063 f064 f033 f004`, then `f00f f006 f006`; the same `f005 … f033`
  group recurs once at `AbortSession`/`EndJob`, followed by `f00a f006 f003
  f00f f006 f00f`. `0xda20` calibration polls add nothing beyond the six.
- **Printer back-channel status read:** `f041 f042`, one pair per bulk IN
  read of the print interface, four per second with the ESP32 pacing.
- **Print job:** `f03f f00b f006` once at `StartJob`; the 186 KiB raster OUT
  stream produced **no messages at all**. Engine transitions show as `f00f`
  bursts (a housekeeping ID excluded from the filtered ring): ×4 and ×2 just
  before status `LD`, ×3 at `PR`, ×1 before `EJ`, ×11 plus `f006` at `ID`.
  After the `EndJob` acknowledgement: `f002 f026 f044 f063 f04d f04e f031
  f003 f006`.

So `f026 f044 f063` is common to scan-session start, scan-session end and
print-job end, and `f006`/`f003` close both job types. The printer's own
status went `NO → WP → LD → PR → EJ → ID → NO`; it held bulk OUT for 38 s in
state `WP` before loading paper, with no messages in that window other than
status polls, so that wait is inside the engine, not the USB task.

## What this gives the emulator

The stock USB task's message sequence per host transaction is now known for
both interfaces, with the IDs that must be posted after SETUP, bulk OUT and
bulk IN completions. The engine side remains opaque: paper load, print and
eject only surface as `f00f` timer-style events and status fields, so the next
instrumentation step should be the sender side of `f00f` and the handlers of
`f026/f044/f063`, not more USB regions.

## Limits

- Snapshots are not atomic; attribution across a boundary can shift by one
  snapshot. Overflow detection relies on the ring changing with an unchanged
  index and would miss exactly 256 IDs between snapshots.
- IDs are 16-bit tags only; payloads, senders and receivers are not exposed.
- One scan and one print; the print used the fixed transport with paced
  back channel, so the host stimulus differs from the ESP32 in timing only.
