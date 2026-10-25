# Running-stock observation app

2026-10-25: the native-startup, stay-awake, one-time Power-pulse build now
responds on hardware as Canon MG2500 series, `VER:2.050`. The previous attempt
started without a normal power-on transition. The bounded physical comparison
showed USB uninitialized before the pulse and initialized/attached afterwards,
with no native USB error. Both boot stages matched a complete physical readback.
[Bring-up evidence](../../../emu/evidence/stockapp/bringup/README.md).
The user subsequently confirmed successful scanning and printing through
`hobby-printer-esp32`. The separate BJL live-recovery attempt timed out; stock
remained responsive, but that USB-to-rescue route is not physically verified.

The [read-only telemetry build](../../../emu/evidence/stockapp/telemetry/README.md)
is now installed after emulator validation and a matching-source physical
checkpoint. Live snapshots, existing request histories and native printer
identity pass; scan/print coexistence with polling remains unqualified.

This runs beyond the smoke build's panel-entry stop, retaining stock runtime,
RTOS, USB, EEPROM and device drivers. It is proprietary stock firmware with a
small source shim, not the final open-source replacement. It installs only as
an MGAP app at `0x10000`, preserving the source recovery bootloader.

The physically tested smoke exception shim is reused, with a new SVC `fd` for
management entry. SVC 7/8, faults/FIQ and three native reset functions enter held
source rescue. The reset functions are intercepted before their calls into the
replaced stock boot block. Direct native app-restart entry `0x11004` is guarded
too. Other SVCs and IRQs still enter stock handlers.

The stock USB processing tasks remain active. BJL `@FROMUPMODE` (and XML
`FRomUpMode`) produces event `0x30007`; the new C selector intercepts this before
queuing it or writing a downloader latch, then requests our held rescue instead
of Canon's updater. Other events replay the two displaced instructions and
continue in stock. The caller LR was saved by stock before the hook; subsequent
code overwrites flags before consuming them, so the hook's temporary flags are
not an input to stock decisions. No new flash writer exists in the shim.

The proposed runtime USB recovery uses stock USB processing; its physical
delivery remains unverified. The reset-time I²C recovery path
remains independent and physically tested. While stock owns the EEPROM/I²C
driver, this intermediate build does not interleave the replacement app's I²C
mailbox polling with it. The canonical open app retains its original USB/I²C
polling unchanged. Replacing the stock parser/drivers with source components is
still required for the final architecture.

```sh
python3 emu/stockapp/build.py --register-vectors --runtime --native-startup \
  --stay-awake --auto-power --boot-dir bootloader/build/vectors \
  --output emu/output/stockapp/auto_power_runtime
# In the offline emulator:
python3 app/stockapp/runtime/startup_validation.py emu/output/stockapp/auto_power_runtime
python3 app/stockapp/runtime/validate.py --directory emu/output/stockapp/auto_power_runtime
```

The working observation build executes native application RAM preparation and
MPU/cache setup, bypasses only the incompatible boot-block checksum, suppresses
standby through the native veto branch, and supplies one Power pulse through
the existing panel handler. These stock dependencies are temporary; native
power management, USB microcode and drivers still need source replacements.
Scanning and printing have user-reported physical verification; long-term power
behavior and telemetry coexistence still require separate physical tests.

Validation separates native startup/attachment, parser/reset guards, protected
uploads and physical USB identity. The earlier fixed-trace boot/home comparison
is retained for its older candidates; it is not a USB success criterion. The
USB host model still lacks some engine/NAK behavior and does not claim complete
enumeration. Actual identity and recovery delivery need hardware evidence.

`hardware.py install` checks the source/artifact hashes and reports before
writing, waits for `C0`, then starts stock and requires its printer-class USB
identity with `VER:2.050`. `verify-rescue` is a separate phase that sends only the
guarded BJL request and restarts the same app after observing held rescue.
Neither phase invokes dangerous-write, automatically retries a transfer, or
restores the original app on successful completion; the requested stock app is
left installed.

The USB recovery host now includes the printer-stream BJL selector before the
command body. Its framing follows
[Gutenprint `canon_control_cmd`](https://github.com/koenkooi/gutenprint/blob/master/src/main/print-canon.c):
`1b 5b 4b 02 00 00 1f`, then `BJLSTART`, the guarded `@FROMUPMODE`
command and `BJLEND`, each on its own line. A separate parser experiment with
`ControlMode=Common` did not reach the recovery hook, so that line is omitted. The earlier parser
fixture entered the BJL parser directly and therefore did not test this envelope.
