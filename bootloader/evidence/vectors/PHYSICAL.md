# Vector bootloader hardware installation — 2026-10-25 UTC

**Verified: the installed 64 KiB boot region is byte-identical to the tested
vector-ABI build. The original source app has been restored and is responding.**
Two independent complete reads through a user-authorized temporary audit app
returned SHA-256
`f22df711f7afd8c3804c43e2d491b8029af59a49f6190c2cafe1097a702e4b92`, with zero
byte differences against the candidate. See the audit sequence below. The
original whole-flash operation still has no observed `C0`; its USB failure is
not explained by this later successful verification.

Before that audit, the user-requested [USB recheck](physical_usb_return.json)
found `MG2550s App`, layout 2, valid application, idle, status `80`, reboot
unblocked. How the device returned was not established by that check. No CPU
reboot or power cycle was requested by the assistant between destructive
programming beginning and the later, separately authorized audit. The
unsuccessful transfer and recovery checks below remain the historical record.

The user explicitly requested flashing the bootloader after the risks of
replacing stage 0 were explained. Before hardware access, the evidence collector
rechecked source/artifact hashes, reproducibility, 97 bootloader checks and six
stock emulator scenarios. The exact 2 MiB image is
`bootloader/build/vectors/install.bin`, SHA-256
`8701d5ac9b52afef966bab588632ad28bc5c70380bffcb3819918d764ace94a3`.
It contains the new stage 0, unchanged stage 2, and current source app
`47f078c2a3cc75dac1d67dd02c0531e9c85e47f67b0cfe0edfa471d9e582b15a`.
No stock-as-app image was sent.

The board initially answered as a valid layout-2 application. A management
reboot reached held bootloader mode, idle, dangerous-write supported. A serial
descriptor query briefly failed during re-enumeration; subsequent management
status succeeded before any flash write.

The canonical `fwctl dangerous-write` sent all **2,097,184 bytes** (header plus
image). Status changed to busy with reboot blocked, establishing that destructive
programming had begun. A later 1-second management control transfer timed out;
the host stopped polling without resetting the printer. The original
[transfer report](physical_flash.json) preserves that failure.

Follow-up status checks, including a
[10-second read-only request](physical_status_after_timeout.json), timed out.
The user reported that the power LED was still blinking. That suggests software
continues executing `leds_tick()` (including possible SPI wait loops), but does
not establish whether programming finished or where it stopped.

A [USB bus-reset attempt](physical_usb_recovery.json) also timed out. This was
not a printer CPU reset or power cycle. The writer's `bus_reset()` explicitly
preserves an already received image and ongoing programming. The user was asked
to reconnect only the printer USB cable while maintaining mains power.

After the user reconnected only printer USB with mains power retained, the
printer did not re-enumerate. A subsequent requested USB check also found no
Canon device or responsive management interface. No CPU reboot was requested.

The ESP32 was initially disconnected from host USB. It subsequently appeared at
`/dev/cu.usbmodem1201`. The [bridge check](esp32_check.json) returned `INFO RSC1
FLOATING` and both inputs high. At the user's request to check possibly swapped
SDA/SCL wiring, an eight-second [stay-only probe](esp32_stay_default.json) used
`COMMAND 1` (stay, not reboot) with the compiled SDA=GPIO5/SCL=GPIO4 mapping.
RX/READ/SENT stayed zero; SDA sometimes sampled low and SCL sampled high. No
successful handshake occurred. Both pins were floated at exit. This does not
distinguish reversed wiring from absent/unresponsive printer polling.

The user exchanged GPIO4/GPIO5 signal wires while keeping printer mains power
and GND connected. A second eight-second
[stay-only probe](esp32_stay_swapped.json) again had zero RX/READ/SENT and no
acknowledgement. Its low-rate STATS samples showed SDA high and SCL low.

To exclude the bridge actively holding a line, only the ESP32 was entered into
its ROM loader. The existing GPIO-release routine verified output-enable zero
and no internal pull-up/down on GPIO4/GPIO5. In
[100 passive samples over 2.370 seconds](esp32_passive_swapped.json), GPIO4 was
high 33 times and GPIO5 high 100 times; 36 successive sampled pairs differed.
Thus the earlier low SCL observations do not establish a continuously stuck
line. This is low-rate level sampling, not an I²C waveform capture or proof of
correct wiring/protocol. The observed activity persisted with ESP32 outputs
disabled. Both pins were register-verified floating again and the ESP32 was
left in ROM. Its flash was not changed. No printer reboot or flash command has
been issued through the ESP32; neither orientation yielded a valid handshake.

## Temporary audit and original-app restoration

The user explicitly authorized flashing a one-time app to verify the bootloader.
[`app/boot_audit`](../../../app/boot_audit/README.md) builds a temporary variant
of the exact original app. It changes the USB product name and adds only a
bounded control-IN reader for `[0,0x10000)`; all production app sources and the
saved original build remain unchanged. No bootloader image is generated or
uploaded by that tool.

Before hardware, [emulation](audit/validation.json) executed complete protected
install/read/restore sequences against the old and candidate boot regions,
including high/full-speed USB, two complete reads per case, and ten rejected
invalid requests. Every sequence preserved the first 64 KiB and restored the
original app exactly. [Host checks](audit/host_validation.json) verified the
request sequence and that an update error prevents an app reboot.

1. [Protected installation](audit/physical_install.json): only request `0x43`,
   95,520-byte audit package SHA-256
   `d18a065350c76b6c6984ed068a166494d3999a8ba40ac978dca8d601f163f7c0`.
   Updater returned `C0`, valid app, idle. Software reboot enumerated
   `MG2550s Boot Audit`.
2. [Readback](audit/physical_read.json): 256-byte control-IN chunks, all 65,536
   boot bytes read twice. Both passes match the candidate exactly and differ
   from the old baseline at 2,736 bytes. Stage-0 SHA-256 is
   `b5383aa702982e413601684c9982257ab4aced095e83769799740aaa145c30e8`.
   Descriptor words are `3158564d 00000001 00000018 e0000880 000008cc 00000890`.
3. [Original app restoration](audit/physical_restore.json): only protected
   request `0x43`, exact original 95,328-byte package SHA-256
   `47f078c2a3cc75dac1d67dd02c0531e9c85e47f67b0cfe0edfa471d9e582b15a`.
   Updater again returned `C0`. Software reboot reached `MG2550s App`, valid
   application, ready status `80`. The temporary app is no longer installed.

This audit establishes installed bootloader bytes, management reboots and
ordinary app update/restoration on hardware. It does not establish the cause
of the earlier USB failure, full-chip contents outside the boot region, or
actual application exception forwarding.

## Subsequent independent first-stage I²C recovery test

The [automatic ESP32 wiring check](../../../tools/esp32/evidence/autowire/PHYSICAL.md)
established the present connection as SDA=GPIO4, SCL=GPIO5. It repeats on ESP
boot and before host image/command operations; no remembered wire order is
required. The app returned the expected rejection of the harmless command-zero
probe, proving both communication directions and acceptance of its framed CRC.

[Physical recovery test](stage0_physical.json): the bridge staged the exact
unchanged 10,464-byte second-stage package, SHA-256
`b73fe74a9a7e4698f03dc6efcd0f1322a23037f2347fa9dae8968c5d470de8f7`,
advertised the maintenance-only offer, and then the USB management interface
requested a software reset into held rescue. Only the independent first stage
accepts that offer. It returned `C0` after programming/verifying the three 4 KiB
sectors `0x4000..0x6fff`. Held USB rescue enumerated afterwards; a further
management reboot successfully started the original, valid open application.

This physically validates stage-0 maintenance transport, its 4 KiB erase/write
path and subsequent stage-2 startup on the connected board. Stage 0 and the
main app were not update destinations. It used a software reset and an already
valid second stage; a damaged-stage boot and interrupted physical write were
not induced. Those failure paths remain emulator observations.
