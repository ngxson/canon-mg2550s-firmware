# Stock panel-selected USB updater

2026-10-25. Analysis and emulation of the original MG2550s dump,
SHA-256 `4b032367cff5976aeb426c8723e08d3c431a38e622e245a614791a3e5067e533`.
The initial updater experiments were offline. The subsequent mapping pass
read the replacement application's live button log while the user pressed
Power. No physical firmware/EEPROM write, reset or motor command was sent.

## Finding

The stock boot block has a panel-input path into its RAM USB updater which
does not require EEPROM recovery marker `a5` or a software-update boot latch.
Both panel-selected modes completed a **full 2 MiB USB installation of our
source-built bootloader and application**, starting at the stock reset vector
with marker `00` and latch 0. The resulting flash matched the target exactly;
a separate replacement-model boot reached the app and its management reboot
into rescue. No EEPROM preparation was needed: stock itself wrote `a5` during
the update and `55` on success. See the full-image results below. The bits are
**Stop/Cancel = `0x20`**, **Power = `0x40`**, set when pressed. This is a promising
first-installation route, **not yet a physically verified stock-updater entry
recipe**. Its relationship to the commonly described service-mode sequence
remains unresolved; this boot routine does not count five/six Stop presses.

## Named buttons and physical evidence

Register: **`0xc0c8d028`** (`PANEL + 0x28`).

| Button | Bit / mask | Evidence |
|---|---|---|
| Stop / Cancel | 5 / `0x20` | Existing `app/src/buttons.c` maps `KEY_CANCEL` here and records physical verification on 2026-10-23. This mapping is also used by the tested Cancel actions. Not pressed again in this session. |
| Power / ON | 6 / `0x40` | Fresh physical observation on 2026-10-25: user asked to hold only Power for one second, then release, and replied “done”. Log changes `08 -> 48 -> 08`; XOR is `40`. |
| Copy B/W | 10 / `0x400` | Existing physically checked mapping in `app/src/buttons.c`; not used by this boot selector. |
| Copy Colour | 12 / `0x1000` | Existing physically checked mapping in `app/src/buttons.c`; not used by this boot selector. |

[Live Power log](stock_panel_power_live.json) was obtained using the existing
replacement app's vendor request `0x70` to enable logging and `0xc0/0x50` to
read it. Management INFO identified layout 2 / application / status `80`.
Power was held for approximately 1.25 seconds between logged transitions.
The idle bit `0x08` remained set throughout; its meaning is not established
here and it is outside this boot routine's `0x20/0x40` masks. “Set when pressed”
describes the MMIO input value, not the electrical voltage on a connector pin.

## Mapped sequence

At the initial reset-time panel check, **hold Cancel with Power released**.
Stock requires that condition in all 16 admission iterations, then writes
`0x20` to panel channel 0 at `0x20001e10` and commits the indication. It waits
indefinitely for Cancel and Power both to be released.

| After admission | Final boot mode | USB flags `+14/+15` |
|---|---:|---|
| Release Cancel without pressing Power | 3 | `01 01` |
| While still holding Cancel, press Power; release Power, then release Cancel | 4 | `00 01` |

Both modes enter the RAM USB updater. The second sequence is one convenient
ordering: the actual rule is simply whether Power was sampled pressed at least
once after admission and before both keys became released. Additional Power
presses do not increment any counter in this routine.

**Both buttons held at the initial check fail admission**, as do Power alone
or neither button. The initial check does not wait for a wrong combination to
become correct; it returns to normal boot. Pressing Cancel after that point
cannot enter this particular path until another reset.

The practical cold-start candidate is therefore: with stock installed, hold
Cancel while restoring mains power, leave Power released, wait for the early
panel indication, then release Cancel (mode 3). The optional Power press above
selects mode 4. This targets reset-time code; do not substitute a generic
soft-off service-mode recipe or claim a calibrated hold duration. The physical
indicator's label/colour and this cold-start procedure remain untested on stock.
The connected board still runs replacement firmware; it was not restored to
stock or power-cycled for this mapping.

## Instruction evidence

See `fw_dump/gpt/usb_update_ram.dis` and `usb_update_entry.dis`, relative to
the outer repository root. Addresses beginning `2000` are relocated RAM code.

- `0x20003440`: reads EEPROM, prioritizes marker `a5`, then latch 1/2.
- With neither condition, `0x200034a8` calls panel routine `0x20001d9c`.
- That routine samples `0xc0c8d028` four times per iteration for 16
  iterations. The admission condition is bit 5 set and bit 6 clear in the
  corresponding ORed samples. Otherwise it returns 1 (normal boot).
- On admission, it waits for panel busy to clear, writes output channel 0,
  and waits for both sampled input bits to clear. It remembers whether bit 6
  was seen set during that wait. Return is 2 if not seen, 3 if seen.
- Selector maps these returns to mode 3 / USB flags `01 01`, or mode 4 /
  flags `00 01`, respectively (flags at mode object offsets `0x14/0x15`).
- Flash wrapper `0x186c..0x187e` invokes updater `0x2000351c` for either mode;
  only mode 1 bypasses it.

The named-button mapping above comes from separate physical evidence. Reset
timing and admission sampling duration are not calibrated. The sample counts
below are MMIO read counts, not presses or milliseconds.

## Selector results

Using the existing `Updater` model, overriding only panel MMIO input, with
boot latch 0 and each initial EEPROM marker `00` and `55`:

| Input samples at `0xc0c8d028` | Returned mode | USB flags |
|---|---:|---|
| All zero | 1 | `00 00` |
| 64 reads of `20`, then zero | 3 | `01 01` |
| 64 reads of `20`, 4 reads of `40`, then zero | 4 | `00 01` |

All six cases passed. Selector execution performed no EEPROM writes, flash
erases or flash programming, and left the initial EEPROM marker unchanged.

## Continuous USB update results

The existing `USBUpdater` controller/DMA model starts at stock flash wrapper
`0x1838`, copies the updater itself, and replaces no firmware functions. Both
panel sequences above were run with initial EEPROM marker `00` and latch 0.
Its default synthetic transfer programs a 64 KiB region at `0x10000`, containing
64 test payload bytes and erased padding; this is not a bootable application.

Both runs produced:

- USB VID:PID `04a9:12fe`, printer interface 0.
- Terminal status `C0`, with final status acknowledgment accepted.
- One erase job and 16 programming jobs; test payload read-back matched.
- EEPROM marker `55` after the updater's normal success write.
- Return to stock continuation `0x1ac`.
- Updater instructions/constants unchanged.

These earlier small-payload runs only established stream acceptance. The
full-image follow-up below extends coverage to full-chip installation and a
separate boot of the resulting image. USB electrical behavior and physical
button entry remain untested. The USB, SPI, I2C and timer models retain the
assumptions of the historical harness.

## Full replacement-image results

[Summary](stock_panel_full_update/verified/summary.json),
[mode 3](stock_panel_full_update/verified/mode3.json),
[mode 4](stock_panel_full_update/verified/mode4.json),
[replacement boot](stock_panel_full_update/verified/replacement_boot.json),
[button controls](stock_panel_full_update/verified/button_controls.json).
All assertions passed on 2026-10-25.

The target is the snapshotted 64 KiB `bootloader/build/boot_region.bin`, followed
by the 93,792-byte `app/build/application.bin`, padded with `ff` to 2 MiB.
Artifact hashes were checked against their build manifests before testing.
Full-image SHA-256:
`b1c7abc8b7303e84fbde73ec88de02355cf810848486d30cb9dfe269294ec8b8`.
The [manifest](stock_panel_full_update/verified/manifest.json) records the
individual artifact and transport-stream hashes; copies of both build manifests
are saved alongside it. This is a complete first-install image, including both
replacement boot stages, rather than an ordinary app-only update.

Each run starts at PC 0 with the unmodified stock dump, EEPROM byte 9 = `00`,
boot latch 0 and the observed idle input bit `08`. At updater entry the test
asserts the expected mode, unchanged original flash, unchanged EEPROM marker
and no preceding EEPROM writes. The inputs are MMIO samples, not timed presses:

| Mode | Samples after masking out idle bit `08` | Result |
|---|---|---|
| 3 | `20` for 128 reads, then release | Full image accepted; `C0`; replacement reset reached |
| 4 | `20` for 128 reads, `60` for 16, `20` for 16, then release | Same |

For **each** mode, stock's real USB parser received all 4,718,676 bytes of the
Canon update stream through modeled controller/DMA transactions. Its complete
2 MiB staging buffer matched the image. Stock then erased 32 consecutive 64 KiB
sectors and programmed 512 consecutive 4 KiB blocks, including address zero.
The entire resulting flash matched the image byte for byte. The host received
`C0` and stock accepted its final status acknowledgment. USB was `04a9:12fe`,
printer interface 0, high-speed bulk packet size 512. The RAM updater's code
remained unchanged throughout.

Stock itself writes EEPROM bytes 8–9 as `ff a5` while updating, then `ff 55` on
success. Thus this removes the need to pre-edit EEPROM to enter the updater;
it does not mean stock performs no EEPROM writes during installation.

The warm exit reaches `0x1804 -> 0x1ac -> 0x1bc -> 0x100`, our reset entry,
with the cache-model correction described below. A **separate** emulator
instance then cold-boots exactly the flash produced by stock: replacement
bootloader PID `176d`, application PID `12fe`, management layout 2 / application
mode / status `80`. The app's USB management command successfully reboots into
held rescue PID `176d`, with flash unchanged. This verifies application boot
and management recovery in the model, not continuous old/new peripheral-state
handoff or physical I²C recovery.

The five-case button probe was also rerun: both accepted sequences passed;
both buttons initially held, Power only and neither button each returned to
normal boot without flash or EEPROM writes.

### Cache-model correction and limits

The [initial diagnostic run](stock_panel_full_update/diagnostic/mode3_failure.json)
already completed the full update with exact flash contents and acknowledged
`C0`, but its warm exit failed at `0x19fc`. The instruction trace continued
executing the **old** cache-teardown loop at `0x1c4..0x234`, although those flash
bytes had been replaced and the new instruction at `0x1c4` branches to reset.
Unicorn 2.0.1 retained old translated code across the SPI model's `mem_write`;
the emulated ARM I-cache invalidate did not remove those translations.

The evidence harness now stops immediately **after** executing the unchanged
`mcr p15, 0, r0, c7, c5, 0` at `0x1c0`, removes Unicorn's flash translations,
and resumes at the same next PC (`0x1c4`). Both the old and new image contain
the identical invalidate instruction there. No firmware bytes, registers or
branch targets are patched. Both warm exits then reach replacement reset.
This explicitly models the invalidate's effect; it is not a cycle-accurate
model of cache lines, prefetch or ASIC behavior.

No physical firmware, EEPROM, reset or motor command was issued for these
tests. A stock printer's physical button entry, timing and complete installation
still require verification before this is an established user-facing procedure.

## Reproduction

### Full replacement image

[Full-image probe source](stock_panel_full_update.py). From the outer workspace
root, with the bootloader and application build artifacts and their manifests
present, choose a fresh output directory (the probe refuses to overwrite an
existing manifest):

```sh
docker run --rm --network none --entrypoint python3 \
  -v "$PWD:/project:ro" \
  -v "$PWD/canon-mg2550s-firmware/emu/evidence:/evidence:rw" \
  canonemu -B /evidence/stock_panel_full_update.py \
  --harness /project/fw_dump/gpt \
  --project /project/canon-mg2550s-firmware \
  --output /evidence/stock_panel_full_update/new-run
```

The source snapshots the build outputs before running, so a concurrent rebuild
cannot change the tested image. Generated image snapshots remain local; JSON
manifests and reports are versionable. The historical harness is a required
local dependency, as with the smaller probe below.

### Reset-vector check with the mapped buttons

[Probe source](stock_panel_update_probe.py),
[saved results](stock_panel_update_probe.json). All five assertions cases pass:
Cancel/release -> mode 3 and `C0`; Cancel/Power press/release/Cancel release ->
mode 4 and `C0`; both held, Power only and neither -> normal continuation with
no flash or EEPROM writes. The model includes observed idle bit `08` throughout.
The positive cases execute the indicator write at `0x20001e10` and perform
the same small synthetic update described above. Every run starts at PC 0 and
stops at `0x1ac`, before the normal application is executed.

From the outer workspace root:

```sh
docker run --rm --network none --entrypoint python3 \
  -v "$PWD:/project:ro" \
  -v "$PWD/canon-mg2550s-firmware/emu/evidence:/evidence:rw" \
  canonemu -B /evidence/stock_panel_update_probe.py \
  --harness /project/fw_dump/gpt \
  --output /evidence/stock_panel_update_probe.json
```

This evidence companion explicitly depends on the historical USB harness;
it is not part of the self-contained public `emu/run.py` interface. Early
SPI completion, PLL locks, memory-ready and delay-counter inputs follow the
previous cold-boot model in `fw_dump/gpt/minimal/validate.py`. RAM is mapped in
advance; electrical RAM initialization and cache/DMA coherence are not modeled.

### Earlier wrapper-entry check

From the outer repository root, use the existing offline `canonemu` Docker
image and mount `fw_dump` read-only at `/work`. Working directory `/work/gpt`.
Run the following Python through `docker run --rm -i --network none
--entrypoint python3 -v "$PWD/fw_dump:/work:ro" -w /work/gpt canonemu -`:

```python
from emulate_usb_controller import USBUpdater
from emulate_usb_update import MODE_OBJECT
import struct

class PanelUSB(USBUpdater):
    def __init__(self, samples):
        self.samples, self.panel_reads = samples, 0
        super().__init__(latch=0)
        self.eeprom[9] = 0

    def read(self, cpu, offset, size, user):
        if 0xc0000000 + offset == 0xc0c8d028:
            i = self.panel_reads
            self.panel_reads += 1
            return self.samples[i] if i < len(self.samples) else 0
        return super().read(cpu, offset, size, user)

for samples, expected in (([0x20]*64 + [0]*4, 3),
                          ([0x20]*64 + [0x40]*4 + [0]*4, 4)):
    e = PanelUSB(samples)
    e.run()
    report = e.report()
    device = next(bytes.fromhex(r['data']) for r in e.responses
                  if r['name'] == 'device_descriptor')
    assert e.word(MODE_OBJECT + 12) == expected
    assert struct.unpack_from('<HH', device, 8) == (0x04a9, 0x12fe)
    assert e.printer_interface == 0
    assert e.last_status == 'C0' and e.final_status_acknowledged
    assert bytes(e.uc.mem_read(0x10000, len(e.payload))) == e.payload
    assert report['updater_instructions_and_constants_unchanged']
    assert e.stopped_at == 0x1ac and e.eeprom[9] == 0x55
    print(expected, e.last_status, len(e.erases), len(e.programs))
```
