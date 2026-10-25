# Stock-as-app startup investigation — 2026-10-25

These are app-only experiments. Every upload uses protected request `0x43` at
`0x10000`; neither boot stage is an update destination. All changed images were
validated in emulation before hardware use. This remains proprietary Canon
firmware with a source shim, an intermediate step toward the open replacement.

## Established cause and changes

Restoring CPU setup alone did not make USB enumerate. Full native application
startup restores RAM preparation, banked stacks, MPU and cache configuration.
Only its incompatible first-64-KiB checksum call is bypassed: the source loader
already validates the MGAP package, and its boot region is intentionally different.

With native startup, a bounded hardware run executed 2,048 IRQs and returned
`USB state = [0,0,0,0]`, error zero, PHY `0x101`, DCTL `2`. Thus the driver had
not been asked to initialize; both USB connection gates were disabled. This
was an observed standby state, not evidence of a CPU crash or USB-ready timeout.

In emulation a Power pulse takes the real panel/power/message path into native
USB initialization (`0x50144`, `0x4a1d4`) and connection (`0x509e8`). The patch
in `auto_power.c` adds one Power pulse after 64 panel samples, for 20 samples,
then stops at count 84. It preserves real button bits and drives no GPIO output.
The stock handler performs debounce and power-on. An observation-mode patch
makes the standby-veto function `0x113f16` return one, using its native early
exit before clock-down/IRQ masking. Standby power saving is intentionally
suppressed in this observation build.

The same patch physically returned USB state `[4,1,1,1]`, native error zero,
PHY `0x10101`, DCTL `0` after 2,048 IRQs. Initialization and connection succeeded;
the native object reached state 4. Live USB identity and command recovery
are recorded separately for the full runtime.

## Hardware sequence

| Image | Physical result | Evidence |
|---|---|---|
| CPU setup + USB-ready stop | Both actual PHY/core readiness waits completed; returned to rescue | [record](cpu_usb_checkpoint/physical.json) |
| CPU setup, full runtime | No USB identity observed; remote cycle recovered held rescue | [install](cpu_runtime/physical_install.json), [recovery](cpu_runtime/power_recovery.json) |
| Native startup + stay awake, IRQ 1,024 | Real idle LR `0x104da8`; PHY `0x101`, DCTL `2` | [record](native_awake_snapshot/physical.json) |
| Same, IRQ 2,048, state record | USB never initialized, no native error | [record](native_usb_state/physical.json) |
| Same + one-time Power pulse | USB initialized and attached, native state 4, error zero | [record](auto_power_state/physical.json) |
| Native startup + pulse, full runtime | Protected upload verified C0; stock USB identity `VER:2.050` responded | [installation](auto_power_runtime/physical_install.json) |
| Full runtime, scan and print | User confirmed both working through `hobby-printer-esp32` | [user report](../telemetry/user_report.json) |
| Same startup + telemetry, IRQ 2,048 | USB state `[4,1,1,1]`, error zero; returned to recovery | [record](telemetry_state_verified/physical.json) |
| Full telemetry runtime | C0 upload verification; 20 live snapshots, SETUP histories and native printer identity passed | [installation](telemetry_v1/physical_install.json) |

The last installed app package is
`380b98ca115955b8a19d410979a42374b219ced94eb03f5c7c715f28f787461e`.
See the [telemetry deployment record](../telemetry/DEPLOYMENT.md) for the
complete sequence and additional CLI captures.
The separate [BJL live-rescue test](auto_power_runtime/physical_verify-rescue.json)
timed out while stock remained responsive. Parser-fixture success therefore
does not establish physical runtime USB recovery. Independent reset-time I²C
recovery remains the physically verified fallback.

Each bounded probe returns to held source rescue and is followed by the
previously validated small SRAM reader. Both reads of each 32-byte record match.
Fresh ESP32 autodetection validated SDA4/SCL5 for the earlier bring-up, then
SDA5/SCL4 for the telemetry deployment after the user checked the wires, and
released both pins. No previous wire orientation was assumed.

A physical [complete boot readback](telemetry_state_verified/boot_after.json)
after the telemetry checkpoint and before the final protected runtime upload
matched all 65,536 bytes, SHA-256
`f22df711f7afd8c3804c43e2d491b8029af59a49f6190c2cafe1097a702e4b92`.

## Emulation limits and corrected interpretation

The old boot test's fixed 106 motor writes and matching task/MMIO traces did
not prove USB operation. Its stock peripheral model supplied PHY/core readiness
but omitted the proprietary USB engine startup response. It could finish with
DCTL deliberately disconnected. The native boot also revealed a low-power WFI
path without modeled wake events; this was initially suspected to be shutdown,
but disassembly showed WFI. No hardware shutdown claim follows from that stop.

Native startup now models shared 8-MiB DRAM aliases at `0x10000000`,
`0x18000000`, `0x20000000`, memory configuration inputs and bounded RAM-clear
DMA completion. These are explicit model assumptions, not new physical strap
measurements. Cache electrical behavior and timing remain unmodeled.

`startup_validation.py` requires actual native USB initialization and attachment,
no native USB error, complete native RAM clearing, the one-shot pulse ending,
intact exception registration, and no flash/SPI write. It supplies connected
VBUS, a synthetic USB-engine startup FIFO token and transmit readiness. The
later engine/global-NAK behavior is incomplete, so it explicitly does **not**
claim emulated USB enumeration. The physical test supplies that evidence.

The full runtime separately passes all native reset guards, actual BJL/XML
parser-to-rescue tests, unrelated-event passthrough and protected full-slot
upload. Printing/scanning now also have the user's physical success report;
long-term power management and live telemetry coexistence remain unqualified.
