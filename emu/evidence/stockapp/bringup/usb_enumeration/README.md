# Emulated stock USB enumeration on the startup harness — 2026-10-26

Goal: make the offline emulator run a USB job against stock so engine
behaviour can be validated without paper. Tool:
`app/stockapp/runtime/usb_job_probe.py` (telemetry_v3 build, native startup,
synthetic VBUS, scripted host; experiment only, nothing asserted). Reports:
[probe_before_changes.json](probe_before_changes.json) (original harness:
native error 14 and soft-disconnect at 1,047 ticks) and
[probe_enumeration.json](probe_enumeration.json) (current model).

## What works offline now

Stock enumerates completely against the scripted host and reaches USB state
`[4,1,1,1]`, the idle state the hardware telemetry reports: bus reset,
stock's reset continuation (OUT-NAK request acknowledged by core bit 7,
`0xf004` cancel, `0x3002` endpoint stop), enumeration done, `0x3001(1,0x200)`
endpoint configuration, SET_ADDRESS 7 (DCFG `0x74`), the real device
descriptor (`04a9:176d`, bcd 0x0205), the 62-byte configuration, string 0,
and SET_CONFIGURATION 1 completed through stock's own continuation
(`0xf004`, `0x3003`, `0x3004`, **`0xf001(0x100018f0, 0xa800, 1)`** setting
up the printer's special receive buffer, `0x3302`, `0x3202`). The message
ring, SETUP ring and state bytes are readable exactly as on hardware.

## Host and engine rules in the model (RE brief 9 answer plus observation)

| Rule | Evidence |
|---|---|
| Liveness word `0xc07e0600` changes while connected | stock polls it every 200 ticks (`0x515a2`); four unchanged reads give native error 14 |
| Core bit 7 = global OUT-NAK acknowledged: DCTL `0x804` bit 9 sets status bit 3 and raises bit 7 a tick later; bit 10 releases | brief 9 §2 (`0x4aa56`, `0x4aabc`); stock enables mask bit 7 through `0x4b378` after reset and after SET_CONFIGURATION |
| Enumeration done as core bit 13 with the mask preset, delivered only after the reset continuation drained | stock arms EP0 only after handler event 8; delivered earlier, the continuation wipes the arming and DCFG |
| Host waits 10 ticks after enumeration before the first SETUP | USB reset recovery; same wipe otherwise |
| SETUP raises `0xb08` bit 3 only; DMA pointer advanced by 8 | stock acknowledges only 8; `0x4aff4` copies from `0xb14 − 8` |
| EP0 IN: host demand = `0x908` bit 13 → stock programs `0x910/0x914` and enables `0x900` → model moves that packet → `0x908` bit 0 | brief 9 §3; produces the hardware's `f01f f022 f023 f021` |
| Core mask bits 18/19 not software-clearable | handler clears them per endpoint event, nothing re-enables them (brief 9 leaves the hardware semantics open) |
| Engine: 32-byte descriptors in the rings at `0x1000c4f0`/`0x1000c570`, submit via `0xc0750080`, completion `0x80000000|descriptor|type` read once at `0xc0750084`, IRQ on VIC source 68; type 0 only for `0x3401/0x3402/0xf004/0xf005`, else type 1 | brief 9 §4; descriptor write-back is not modeled |
| Printer bulk OUT is endpoint 1 (second listed) | configuration lists the scanner interface first |

## Later in the day (2026-10-26, continued)

- GET_DEVICE_ID works once the request addresses the printer interface
  (`wIndex = 0x0100`, as the Mac does): the full 208-byte device ID string
  arrives in four 64-byte packets, giving the hardware's four `f023`.
- Flags-only engine commands (a write of 2 or 3 to `0xc0750080` with no
  descriptor) are answered by polling, not by interrupt; the model reports
  them that way.
- The printer session does not start on USB state alone. The USB context
  mirrors the machine mode word at `0x180409a0`; the printer start routine
  proceeds only when it reads the ready mode `0x07000007`, and the emulated
  stock stays in the power-on mode `0x0200000f` because its mechanical
  initialization is not modeled. With the mode forced to ready
  ([probe_forced_ready_mode.json](probe_forced_ready_mode.json)) the printer
  object armed at configuration, the data-available token produced
  `0xf003` start, `f00f f00b`, and about twenty ticks later stock cancelled
  with `f004 f005` and the job-end group seen on hardware, because no data
  reached it through the engine's special receive path.
- The special receive buffer (`0xf001`, 7 × 6 KiB at `0x100018f0`) is
  consumed through per-chunk trailers that the engine fills and stock
  scans; the exact handshake is the remaining gap before print bytes reach
  the stock parser offline.

## Open

- Machine readiness: an offline print job needs the mechanical
  initialization modeled (or the mode forced, as an explicit experiment) so
  stock reaches `0x07000007`.
- The engine's chunk handshake for the special receive path.
- Per-packet `f023` and the completion types per engine command rest on the
  brief's reading plus the hardware re-enumeration capture.
