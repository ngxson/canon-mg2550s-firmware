# Live stock telemetry — 2026-10-25

**Telemetry firmware installed; initial live USB collection verified.** The
stock-as-app with the telemetry shim remains installed. Before this update,
the user confirmed scanning and printing
through `hobby-printer-esp32`; this is [user-reported physical evidence](user_report.json),
not an assistant-collected scan/print trace. The Mac's scanner problem is not
evidence that the stock printer runtime failed.

The complete deployment and physical evidence are in [DEPLOYMENT.md](DEPLOYMENT.md).
Twenty live snapshots, request-history collection and normal stock identity
passed after installation; five further CLI captures are preserved.
[Scan coexistence](scan_coexist/README.md) is physically qualified: three
identical Python scans completed with no polling, with 2.5 Hz history polling
and with 10 Hz history-free polling. History polling slowed the image phase by
26 %; history-free polling had no measurable effect.
[Print coexistence](print_coexist/README.md) is qualified too: a 300 dpi page
without polling and a 600 dpi page with 10 Hz history-free polling both
printed completely, scanned back with exact scale. The first print attempt
failed because the Python port timed out OUT after 8 s while the printer was
NAKing at print speed and read the back channel without pacing; both are
fixed and the printer-side status is now logged.

## Transport and implemented scope

Use the existing stock USB control endpoint while its normal scanner/printer
interfaces remain available. The app adds one reviewed Thumb BL patch at
`0x51bf6`, replacing the native vendor-handler lookup with a C selector and a
small register/interworking wrapper. No bootloader change, allocator, extra RAM
reservation, persistent write, or arbitrary memory/MMIO command is added.

The additional shim occupies 368 bytes relative to the working build (1,404
bytes total including the existing startup/recovery code). Both boot stages are
byte-identical. The image still contains proprietary Canon runtime, RTOS and
USB drivers; the shim is an observation tool toward replacing those dependencies.

| USB request | Meaning |
|---|---|
| `bmRequestType=0xc0`, `bRequest=0xd0`, `wIndex=0`, `wValue=offset` | ABI header and allowed region map; ABI 2 is 72 bytes (magic, 2, 64, 7, then address/length pairs), read in two 64-byte pieces |
| `0xc0 / 0xd1`, `wIndex=region`, `wValue=offset` | Read 1–64 bytes wholly inside one allowed RAM region |

ABI 1 (regions 0–3, 48-byte map read at offset zero only) was the first
installed build; ABI 2 adds regions 4–6 below and is otherwise identical.

Malformed telemetry requests return the native STALL result. Other requests
retain the native vendor lookup. Recognized replies are copied into the stock
EP0 buffer at `0x100129e0`; stock handles packetization, DMA and status stages.
The native control object stores remaining length at `+0x0c` and its buffer
pointer at `+0x10`. This follows the stock GET_DEVICE_ID handler's pattern.

| Region | Address | Bytes | Stock evidence |
|---|---|---:|---|
| 0 | `0x1804099c` | 4 | USB state/mode/initialized/attached, also measured by bounded physical startup probes |
| 1 | `0x1803fcc4` | 8 | Native error byte, message counters, and SETUP ring indices; not every field's semantics is established |
| 2 | `0x1803fccc` | 1024 | First four bytes of the last 256 SETUP packets, written at `0x51b6a–0x51b74` |
| 3 | `0x180400cc` | 1024 | Last four bytes of those packets, written at `0x51b76–0x51b84` |
| 4 | `0x180404cc` | 2 | Filtered message ring write index and processed count (`0x50af0`, `0x50bb0`) |
| 5 | `0x180404ce` | 512 | Every dispatched message ID, 256 × u16, indexed by region-1 byte 1 (`0x50a7e–0x50a96`) |
| 6 | `0x180406ce` | 512 | Same IDs minus 14 housekeeping IDs (`0xf002/f006/f00c/f00f/f010/f01f–f023/f04f–f052`), written at `0x50aec–0x50af8` |

Region 1 byte 1 is the all-messages write index and byte 2 the processed
count (`0x1803fcc5/6`); the recorder at `0x50a76–0x50bca` stores the u16 at
offset `0x1c` of each message before dispatch. Scan and print qualification
showed this counter reacting only to host USB transactions, which motivated
exposing the IDs themselves.

Disassembly is pinned to stock SHA-256
`4b032367cff5976aeb426c8723e08d3c431a38e622e245a614791a3e5067e533`.
The selector's prerequisites come from `0x51b02` (buffer reset), `0x51b16`
(SETUP worker), `0x5199c` (vendor lookup), `0x51c58` (completion dispatch), and
`0x51e8c` (GET_DEVICE_ID). D0/D1 are unused in this lookup. On completion the
native unknown-vendor path invokes no callback. This is disassembly/compiled
fixture evidence; initial physical USB delivery is now verified separately.

Snapshots are deliberately **not atomic**: IRQs/tasks can run between loads,
and split histories can change during collection. Their indices wrap modulo
256. Telemetry's own SETUP packets also enter the rings. The host preserves raw
data and before/after indices rather than claiming a consistent ordered trace.
No document/image buffers are exposed.

## Build and checks

```sh
python3 emu/stockapp/build.py --register-vectors --runtime --native-startup \
  --stay-awake --auto-power --telemetry --boot-dir bootloader/build/vectors \
  --output emu/output/stockapp/telemetry_v1
# Run inside the offline mg2550s-emu environment:
python3 app/stockapp/runtime/validate_telemetry.py emu/output/stockapp/telemetry_v1
python3 app/stockapp/runtime/startup_validation.py emu/output/stockapp/telemetry_v1
python3 app/stockapp/runtime/validate.py --directory emu/output/stockapp/telemetry_v1
```

Candidate MGAP SHA-256:
`380b98ca115955b8a19d410979a42374b219ced94eb03f5c7c715f28f787461e`.
[Manifest and exact source hashes](manifest.json).

- [Compiled hook validation](telemetry_validation.json): 4,088 ordinary vendor
  lookup comparisons against the unpatched call; user-mode execution and both
  stack alignments; bounded reads and rejection of invalid requests; native IN
  continuation/completion; host-reader round trip; only stack/control-buffer
  writes permitted, with MMIO entirely unmapped.
- [Native startup](startup_validation.json): 450 ticks/757 IRQs, real native
  init/attach once, no USB error, complete RAM clearing, intact recovery vectors,
  no flash/SPI writes. USB engine startup still has explicitly synthetic inputs.
- [Recovery/update regression](guard_validation.json): four reset guards,
  unrelated-event passthrough, actual BJL/XML parser fixtures, held recovery
  after real reset, protected full app upload with header committed last.

The emulator checks alone do not prove electrical USB delivery, cache/DMA
behavior, scan/print coexistence, or timing on hardware. A matching-source
[physical checkpoint](../bringup/telemetry_state_verified/physical.json) now
establishes initialized/attached USB with no native error. The subsequent
[runtime installation](../bringup/telemetry_v1/physical_install.json) establishes
live telemetry and native printer identity. Long-duration collection and
scan/print coexistence remain unqualified.

The direct-USB collector for the installed image is:

```sh
python3 tools/stocktrace.py --count 30 --interval 1 --output live-stock.jsonl
python3 tools/stocktrace.py --setup-history --output usb-setup-history.jsonl
```

It verifies stock USB identity and the exact telemetry ABI, makes only control-IN
requests, does not detach drivers/reset/configure the printer, and refuses to
overwrite evidence. It requires a telemetry-enabled stock app. Generated
snapshot files must be labeled with the installed
package hash and stimulus; VID/PID alone cannot identify an instrumented build.
The initial [idle CLI capture](live_idle.jsonl) has a separate
[provenance manifest](live_idle_manifest.json).

## ESP32 path

The selected deployment uses direct Mac USB. The new
[Python scan/print tools](../../../../tools/STOCK_USB.md) can supply the same
job protocol without the OS imaging stack. An ESP32 HTTP telemetry relay is an
optional future transport, not a prerequisite for this deployment. The separate
GPIO4/5 ESP32 still provides independent recovery support.

The user's working printer host is `/Users/ngxson/work/hobby-printer-esp32`.
A read-only `/info` check found debug firmware 0.2.0, ELF prefix
`6f2bda0b00f0852c`, valid `ota_1`, no core dump. This is a different ESP32 role
from the GPIO4/5 I²C recovery bridge.

Its `src/usb_printer.c:ctrl_request()` already serializes EP0 transfers with a
mutex and works from tasks outside the USB event task. Its HTTP API currently
does **not** expose a telemetry route. The next host change should provide a
strict D0/D1 read-only wrapper and a bounded HTTP snapshot endpoint, then let
`stocktrace.Reader` use that transport. Do not forward arbitrary USB requests.
Retain the shared control-transfer lock and correctly drain/cancel a timed-out
transfer before reuse. Run that project's host/QEMU checks before an ESP32 OTA.
No ESP32 source, firmware or reboot was changed during this investigation.

For ordinary scan/print captures, ESP32 USB → Wi-Fi → collector avoids the Mac's
USB stack and keeps a single host attached. For enumeration capture, read a
pre-recorded RAM trace after enumeration; a USB transport cannot stream while
its own endpoint is not yet operational.

## Turning observations into emulator behavior

1. **Qualify coexistence:** scan is done ([evidence](scan_coexist/README.md)):
   no USB errors, image phase 5.48 s → 6.91 s with 35 EP0 reads per 0.4 s,
   unchanged with 2 EP0 reads per 0.1 s. Poll history-free during jobs.
   Print is done as well ([evidence](print_coexist/README.md)): 10 Hz
   polling through a 600 dpi page, no errors. Scan and print are invisible to
   the SETUP rings (bulk only); only the region-1 message-ring index reacts,
   and it counts host-driven USB transactions: 0 idle, 96 at scan session
   start, 0 during `0xda20` calibration polling, ≈ 6 per `0xd420` image round
   trip (≈ 390/s while the host busy-polls), ≈ 1 per print back-channel status
   reply (≈ 1,750/s when unpaced, ≈ 8/s paced). Engine/motor/head activity
   never shows in it. Use the independent reset-time I²C recovery;
   the current BJL live-rescue attempt was not successful.
2. **USB engine/IRQ ordering first:** capture command/completion values around
   `0xc0750000`, `0xc0750080/8c`, DCTL/global-NAK, `0xc0780014/18`, endpoint
   summary/mask `0xc0780838/83c`, and individual endpoint completions. Observe
   the values already loaded/stored by stock instructions, not extra reads of
   FIFO/read-clear registers. A separate trace buffer needs proven ownership,
   startup initialization, sequence numbers, overflow reporting and measured
   overhead before adding those hooks. No ring is reserved by this prototype.
3. **Task/message and motor/scanner transitions:** the stock message-ID
   rings are exposed since ABI 2 and the first scan/print captures are decoded
   in [messages/README.md](messages/README.md): fixed USB-task sequences per
   SETUP, scanner command and printer status read, job start/end groups, and
   `f00f` bursts at paper load/print/eject. Next correlate IRQ completion, DMA
   submission and native state changes with those IDs, starting from the
   sender of `f00f` and the handlers of `f026/f044/f063`.
4. **Replay as evidence:** preserve package hash, hook-site/version, raw values,
   event ordering, stimulus, host timing and drop counts. Add one peripheral
   rule at a time; require stock to reach its next state naturally. Include
   missing/delayed completion cases so the model does not simply force success.

The current USB model specifically lacks later microcode/DMA and global-NAK
completion behavior. Hardware traces should settle those transitions before
extending scanner/print mechanics. Successful scan and print establish a usable
baseline; they do not establish complete hardware coverage or emulator fidelity.
