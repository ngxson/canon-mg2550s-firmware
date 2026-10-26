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
  stream produced **no messages at all**. `f00f` bursts (excluded from the
  filtered ring) appear near status changes: ×4 and ×2 just before `LD`, ×3
  at `PR`, ×1 before `EJ`, ×11 plus `f006` at `ID`. After the `EndJob`
  acknowledgement: `f002 f026 f044 f063 f04d f04e f031 f003 f006`.

The printer's own status went `NO → WP → LD → PR → EJ → ID → NO`; it held
bulk OUT for 38 s in state `WP` before loading paper, with no messages in that
window other than status polls.

## What the rings are, per the answered RE brief 12

The brief answer ([fw_dump/gpt/BRIEF_message_ids.md](../../../../../../fw_dump/gpt/BRIEF_message_ids.md))
corrects the first reading above: the recorder at `0x50a76` belongs to the
**USB device task** and its mailbox `0x3f`, so every ID here is a USB
transport/control event, not engine progress. `f01f f021 f022 f023` are
posted by the USB control driver, `f03f f041 f042 f044 f04d f04e` by the
printer-class driver, `f05b–f05e f063 f064` by the scanner-class driver;
`f00f` is a buffer/data-availability notification carrying a pointer, length
and channel, so its bursts around `LD/PR/EJ` are status data being produced,
not an engine timer. `f003` on this mailbox is distinct from the raster
parser's `f003`. The event code is the low 24 bits of the word at message
`+0x1c`; the rings keep only 16.

## What this gives the emulator

The stock USB task's message sequence per host transaction is now known for
both interfaces. For engine state the answer names plain-RAM variables
instead: the reported-status cache (`0x1803a790`, DJS category/substate and
PID), the print geometry (`0x1806c974`, 1/600 inch), the ASF state byte, the
LF position word and the saved paper-edge coordinate. Those are exposed as
regions 7–11 in telemetry ABI 3 and read with `stocktrace.py --engine`.

## Limits

- Snapshots are not atomic; attribution across a boundary can shift by one
  snapshot. Overflow detection relies on the ring changing with an unchanged
  index and would miss exactly 256 IDs between snapshots.
- IDs are 16-bit tags only; payloads, senders and receivers are not exposed.
- One scan and one print; the print used the fixed transport with paced
  back channel, so the host stimulus differs from the ESP32 in timing only.

## Hardware re-enumeration capture — 2026-10-26

`host_reset_reenumeration.jsonl`: the Mac issued a USB port reset to the
running stock app (libusb `reset_device`); rings were read before and
0.14 s after re-enumeration, so the difference is the stock messages of one
real enumeration (49 IDs, 27 filtered). Decoded:

- Bus reset: `f030 f033`, then the class-driver group
  `f026 f044 f063 f04d f031 f035 f038 f036 f03c`. This is the reset
  reaction, which also explains the same group at scan-session and
  print-job boundaries (bus-level session changes).
- Each control transfer: `f01f f022 f023 f021`, with one `f023` per IN
  data packet (GET_DEVICE_ID shows four, so this enumeration ran at full
  speed with 64-byte packets).
- The host's SETUP sequence as recorded by stock: SET_ADDRESS 2, device
  descriptor, strings 2/1/3 (short then full), configuration (9 then 62),
  SET_CONFIGURATION 1, string 0, GET_DEVICE_ID. Stock answered all of them.

The emulator's reproduction of this sequence is tracked in
[usb_enumeration/README.md](../../bringup/usb_enumeration/README.md); as of
this capture it lacks the per-packet `f023` event and stalls at the
SET_CONFIGURATION status stage.
