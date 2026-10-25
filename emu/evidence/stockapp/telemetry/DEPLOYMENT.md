# Telemetry deployment record — 2026-10-25

Current status: **telemetry firmware installed and physically verified.** The
printer runs stock with the read-only telemetry shim. The protected runtime
update passed C0 verification; twenty live snapshots, both SETUP histories and
ordinary stock identity passed. The standalone collector subsequently saved
five additional successful captures. No bootloader update was performed.

All timestamps below are UTC. Initial live verification completed at 20:17:43
on 2026-10-25 (21:17:43 Europe/Paris, after that day's DST change).

This record contains implementation and deployment details. The short current
handoff is `fw_dump/PROGRESS.md`; its former full contents are preserved in
`fw_dump/PROGRESS_HISTORY_BEFORE_STOCK_TELEMETRY.md`.

## Prepared images and validation

The runtime image is the C telemetry selector and Thumb ABI wrapper described
in [README.md](README.md). It exposes only the four reviewed software-state
regions through stock EP0. It neither reads arbitrary MMIO nor writes flash.
It remains proprietary stock firmware with an open source observation shim.

- Runtime: `emu/output/stockapp/telemetry_v1/candidate.mgap`, SHA-256
  `380b98ca115955b8a19d410979a42374b219ced94eb03f5c7c715f28f787461e`.
- Bounded startup check: `emu/output/stockapp/telemetry_state/candidate.mgap`,
  SHA-256 `de8f1f0bb9dc8fcefcbea02b16f78a1536383e9f0d0e3f6a6c4f2dab1fdc489a`.
  Its same-source runtime returns through the real recovery vectors after
  2,048 IRQ entries, preserving a small USB-state record.
- Both retain the exact protected 64-KiB boot prefix, SHA-256
  `f22df711f7afd8c3804c43e2d491b8029af59a49f6190c2cafe1097a702e4b92`.
- The runtime's source/artifact hashes and three successful emulator reports
  were rechecked against the unchanged image. The newly built bounded image
  passed checkpoint-to-recovery and compiled-telemetry validation before any
  attempted deployment. Both candidates have identical firmware source hashes.

The bounded checkpoint model intentionally does not claim a physical USB state:
its synthetic peripheral model returns a different state/error record. The
physical checkpoint established initialization/attachment with no native error
before the full runtime was installed. No synthetic record was accepted as
that physical proof.

## Host deployment changes

`app/stockapp/runtime/hardware.py` now accepts an explicit `--startup-proof`
directory and optional `--from-held` entry. It still requires matching firmware
source hashes and flags, successful emulation, physical startup proof, the
historical independent first-stage recovery proof, and protected image checks.
A telemetry install additionally requires its compiled-hook/host-reader report.
No check was removed to work around the missing wiring proof.

After the successful runtime update it verified stock identity, read twenty
live snapshots and both existing SETUP histories, and then verified ordinary
stock printer identity again. Snapshots are not atomic. This qualifies initial
USB delivery only, not long-duration scan/print coexistence or complete MMIO
tracing. The Python scan/print tools have twenty independent C-reference test
groups passing; their physical qualification is still separate.

`hold_on_startup.py` watches only the exact source bootloader product and sends
STAY once its management identity is valid. It never resets or updates firmware.
The first watcher exited on a transient missing USB language descriptor during
power-off. It now treats that descriptor error as transient. A replacement
watcher caught and held the same startup; no second power cycle was needed.

## Physical sequence

1. User connected the printer to the Mac. Stock product `MG2500 series`,
   `04a9:176d`, device version `0x0205`, and printer identity `VER:2.050` responded.
2. User reconnected the recovery ESP32. Its firmware reports `AUTOWIRE=1`.
   The probe received no traffic while stock ran. Stock does not service the
   independent recovery mailbox, so that silence was not a wiring proof.
3. With the watcher armed, the previously authorized remote power switch was
   invoked once. The request was acknowledged. The replacement watcher held
   source recovery at `2026-10-25T20:03:20.693725+00:00`.
4. `checkpoint_hardware.py` entered its preflight, but fresh wiring detection
   failed in both orientations: RX=0, READ=0, EXCHANGES=0, DROP=0. It exited
   before `protected_upload_begin`; no erase/program request was issued.
5. USB management still reported layout 2, held=true, busy=false, app_valid=true,
   reboot_blocked=false, status `80`. A separate routing recheck was also silent.
   Silence can mean a missing connection or another electrical/polling problem;
   it is not evidence that either orientation was correctly wired.
6. After the user checked the wires, fresh detection passed:
   `VERIFIED SDA5 SCL4 PROOF=REJECT90 FLOATING`. The exact previously validated
   bounded image was copied to `telemetry_state_verified`, retaining the first
   failed attempt's evidence. Its protected upload began at 20:13:01 and passed
   C0 at 20:14:13. It returned to held recovery at 20:15:28.
7. The validated audit reader was uploaded and verified, then read the preserved
   32-byte checkpoint twice identically. USB state was `[4,1,1,1]`, native error
   zero, PHY `0x10101`, DCTL zero, at IRQ 2,048.
8. A complete physical 65,536-byte bootloader readback at 20:15:48 matched
   `bootloader/build/vectors/boot_region.bin` and the protected-prefix hash above.
   This readback preceded the final runtime app update; it is not a post-runtime
   readback. The final update used the normal protected app path.
9. The exact continuous-runtime package was uploaded at 20:15:55, passed C0 at
   20:17:06 and started immediately. Stock `VER:2.050` responded at 20:17:41.
   Twenty live snapshots showed initialized/attached USB; 2,048 history bytes
   were read, followed by another successful native GET_DEVICE_ID. The install
   result was `stock_telemetry_installed_and_verified` at 20:17:43.
10. The unmodified `tools/stocktrace.py` CLI collected five snapshots with full
    histories at 20:19:25–20:19:28. All showed `[4,1,1,1]` and error zero, with
    about 139–141 ms per complete snapshot/history collection. These timings
    describe this idle host run, not a scan/print performance qualification.

Evidence: [deployment preflight](deployment_preflight.json),
[power-cycle acknowledgement](../bringup/telemetry_state/power_cycle.json),
[initial watcher](../bringup/telemetry_state/power_recovery.json),
[successful held capture](../bringup/telemetry_state/power_recovery_retry.json),
[stopped-before-write checkpoint attempt](../bringup/telemetry_state/physical.json),
[successful checkpoint](../bringup/telemetry_state_verified/physical.json),
[boot readback](../bringup/telemetry_state_verified/boot_after.json),
[runtime installation and live checks](../bringup/telemetry_v1/physical_install.json),
[CLI capture](live_idle.jsonl), [capture provenance](live_idle_manifest.json).
Remote-switch credentials are not included in this record.

## Second deployment: ABI 2 message rings — 2026-10-25 21:35–21:42 UTC

After scan and print qualification showed the region-1 message counter
reacting only to host USB transactions, the allowlist was extended to the
stock message recorder (regions 4–6, ABI 2; see [README.md](README.md)).
The addresses were re-derived from the disassembly at `0x50a76–0x50bca`
before use. The same deployment flow was repeated with new images built from
the changed sources, because the install preflight requires a bounded
physical startup proof from byte-identical sources:

- Runtime `emu/output/stockapp/telemetry_v2/candidate.mgap`, SHA-256
  `aa7ac52fe77712cf15f34174db7f1cc2008a91a866c1ad0299dc688082f6008f`
  (shim 1,460 bytes). Bounded image `telemetry_v2_state`, SHA-256
  `29a4f2f90fe22042e0e4fa613392dc05f441657bda8000c5dd8349e3cf646874`.
- Emulator checks on the new images all passed: compiled-hook telemetry on
  both (4,088 passthrough cases, 21 boundary reads, 15 rejections, ABI 2 map in
  two pieces, message-ring round trip), native startup (757 IRQs, one USB
  init and attach), guard/update regression, bounded return after 2,048 IRQs.
- Physical sequence: watcher armed, webhook acknowledged (`200`), held rescue
  captured; checkpoint wiring verified at the first probe
  (`SDA5 SCL4 PROOF=REJECT90`); bounded image uploaded, C0 verified, returned
  to held rescue; audit reader read USB state `[4,1,1,1]`, error 0, PHY
  `0x10101`, DCTL 0 at IRQ 2,048; runtime uploaded, C0 verified, stock
  `VER:2.050` responded; 20 live snapshots and histories passed. Result
  `stock_telemetry_installed_and_verified` at 21:41:40.
- First idle ring read: the all-IDs ring holds the telemetry's own pattern
  `f01f f022 f023 f021` per request; the filtered ring was static.

Evidence: [watcher](../bringup/telemetry_v2_state/power_recovery.json),
[power cycle](../bringup/telemetry_v2_state/power_cycle.json),
[checkpoint](../bringup/telemetry_v2_state/physical.json),
[install](../bringup/telemetry_v2/physical_install.json),
[idle rings](live_idle_v2.jsonl). The ABI 1 build `380b98ca…461e` is no
longer installed.

## Remaining qualification and emulator work

The deployment is complete. The Python scan tool and scan/telemetry
coexistence were physically qualified on 2026-10-25 at 20:40–20:42 UTC
([scan_coexist/README.md](scan_coexist/README.md)); the print tool and
print/telemetry coexistence followed at 21:14–21:16 UTC after a transport fix
([print_coexist/README.md](print_coexist/README.md)). The user's earlier
successful scan/print report applies to the non-telemetry stock baseline.
The present allowlist provides USB software state and request history; it does
not expose engine/IRQ/DMA transitions needed to complete the emulator. Add such
hooks only with a proven buffer lifetime and measured overhead, following the
plan in [README.md](README.md). The BJL live USB-to-recovery command remains
unverified; independent reset-time I²C recovery has separate physical proof.
