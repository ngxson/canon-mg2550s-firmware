# Temporary boot-region audit app

Used with explicit user authorization on 2026-10-25 to identify the installed
bootloader after a whole-flash update lost USB confirmation. The original
source app has been restored. [Physical evidence](../../bootloader/evidence/vectors/PHYSICAL.md).

`build.py` compiles a temporary variant of the current app in
`app/build/boot-audit`, preserving production app sources and build artifacts.
It saves the original package, changes the USB product string to
`MG2550s Boot Audit`, and adds control-IN request `0x95`. Offset is
`wValue | (wIndex << 16)`; transfers must be aligned, 4–256 bytes and entirely
inside flash `[0,0x10000)`. Every other address and an OUT request are rejected.
It adds no flash write or motor operation.

`validate.py` emulates protected installation, two complete reads, rejection of
invalid reads and exact original-app restoration with both historical and new
boot regions. `validate_host.py` checks host request sequencing and error exits
using fake USB devices; its reports are not physical observations.

`hardware.py` has separate `install`, `read` and `restore` phases. Installation
and restoration use only the normal application update request `0x43`, starting
at `0x10000`, and reboot into the app only after `C0`, idle, valid-app status.
There is no dangerous-write request or automatic retry. Readback performs two
complete boot-region reads and compares actual bytes with both reference images.
Reports are written progressively and existing physical reports are not replaced.
The restore image is pinned to the exact original app used for this incident.

Build and offline checks from the repository root:

```sh
python3 app/boot_audit/build.py
# In the offline emulator container:
python3 app/boot_audit/validate.py
# With the host USB dependencies installed; fake devices only:
python3 app/boot_audit/validate_host.py
```

This is verification tooling, not a new permanent firmware dependency. It does
not prove independent recovery or exercise the application-vector ABI itself.

The separate `--stock-record` build writes only `app/build/stock-record`, uses
product `MG2550s Stock Record`, and adds fixed control-IN `0x96` (value/index
zero, exactly 32 bytes) for SRAM `0xe00008d0..0xe00008ef`. Run `validate.py` with
the same flag to exercise this reader's installation, record persistence,
request bounds and restoration. It was used after the
[stock startup smoke](../stockapp/smoke/README.md) and then removed by restoring
the exact original app; the historical audit artifacts remain separate.
