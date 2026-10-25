# Physical stock-as-app startup — 2026-10-25

**Passed: real stock startup reached the planned pre-panel stop, returned to
held source rescue, and left the complete boot region byte-identical. The exact
original open app is restored and responding.** This was the limited smoke
variant, not stock panel/USB operation or a print/scan job.

Before stock upload, the [ESP32 wiring firmware](../../../../tools/esp32/evidence/autowire/PHYSICAL.md)
detected SDA=GPIO4/SCL=GPIO5 through a complete request/read/rejection exchange.
The [independent first-stage test](../../../../bootloader/evidence/vectors/stage0_physical.json)
then reinstalled the exact unchanged stage-2 package, received `C0`, enumerated
held USB rescue and restarted the original app. The first stage was never an
update destination in either this recovery test or the stock experiment.

1. The protected app updater received the 2,031,616-byte smoke package and
   returned `C0`, idle, valid application. Package SHA-256:
   `9d2c2b91f9f1d55720df4f3e9a8b6e4e468288889c1e895e38b1575a4dff4238`.
2. A management reboot started the smoke build. The first 45-second host wait
   expired. No reset, resend or reflash followed that timeout. Subsequent
   read-only USB checking found **held rescue, status 80**; an independent
   command-zero I²C probe also completed. The timeout remains in
   [the initial report](physical.json), with [USB](usb_after_timeout.json) and
   [I²C](i2c_after_timeout.json) observations. Full-slot CRC validation happens
   before USB attachment on each loader startup and may contribute to this
   delay; its exact share of the physical startup time was not isolated.
3. Only after observing held rescue, [readout resumed](physical_resume.json).
   A validated temporary app read the checked SRAM record twice, identically:

   ```text
   31504d53 ceafb2ac 00000002 fffffffd
   000000fe ffffff01 0013576a 00000030
   ```

   Stage 2 means the callback requested return; reason `fe` is the planned stop,
   LR `0x13576a` identifies the patched instruction at `0x135768`, and SPSR
   `0x30` identifies Thumb user mode. This establishes the actual hardware path
   through the generic exception ABI into the smoke callback and back to reset.
4. The reader retrieved every boot byte at `0..0xffff`. Its SHA-256 is
   `f22df711f7afd8c3804c43e2d491b8029af59a49f6190c2cafe1097a702e4b92`,
   identical to the installed/tested boot region. No differences.
5. Protected upload restored the exact original 95,328-byte app, SHA-256
   `47f078c2a3cc75dac1d67dd02c0531e9c85e47f67b0cfe0edfa471d9e582b15a`.
   After `C0`, software reboot reached `MG2550s App`, valid/idle, status `80`.

The restore reinstates the original MGAP app bytes. It does not claim the unused
remainder of its flash slot matches the pre-experiment whole-chip contents.
No dangerous-write operation or printer power cycle occurred in this experiment.
The temporary record reader is no longer installed. The ESP32 retains the new
automatic wiring-validation firmware, with both signal pins released.

Offline checks, archived in [validation.json](validation.json): 11 stock startup/
exception cases, a full-slot protected upload with header committed last and
boot region preserved, three reader install/read/restore cases including USB
speeds and invalid requests, and host error-path simulations. Emulator counts
such as the 879 stock SVCs are not physical instrumentation measurements.

The mixed stock/source smoke package remains an observation tool. Full stock
management polling, safe stock updater entry, arbitrary interrupts, and actual
panel/USB/scanner/printer operation remain separate work toward the fully open
replacement firmware.
