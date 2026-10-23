# Hardware evidence for the standalone implementation

Reference image: the previously tested intermediate `mg2550s_minimal.bin`,
SHA-256 `4edea85b66b4caf67d801391bb752853ae4c58857c2f64075cfbf9b932b5233d`.
Its early boot/updater still uses Canon code. The retained emulator observation
is [reference_transactions.json](reference_transactions.json), a numeric MMIO
trace, not an executable image. The earlier private observation harness is not
a public build dependency. The trace is RE evidence only; `build.py` does not
read it. Register settings are reviewable C constants in
[board_values.h](../include/board_values.h).

The emulator trace establishes what instructions write under modeled readiness
conditions. It is not a logic-analyzer capture. The reference's successful
physical cold boot and LED sequence support that overall configuration, but do
not establish the function or necessity of every register field.

## Startup

The stock reset entry `0x10c` uses internal SRAM near `0xe0000f00` before external
RAM initialization. Its cache teardown `0x1bc` cleans/invalidates four ways of
128 32-byte lines and disables I/D caches and MPU. The replacement uses the same
CPU operations in newly compiled code; it leaves caches/MPU disabled throughout
runtime. It needs no stock MPU region descriptors.

The old RAM updater's exit functions `0x200071fc/0x20007204` load PC with flash
`0x1804/0x1800`, respectively. The source image supplies two branch landings at
these addresses. The success path then reaches `0x1ac`; an I-cache-invalidate
landing at `0x1bc` also handles an already cached old continuation. Initial
installation emulation caught the missing `0x1804` entry before hardware use.

Observed startup sequence:

| Reference PC / block | Hardware behavior used by new C |
|---|---|
| `0x448..0x4b6` | Early pads/clocks and serial-read pin selection; poll `0xc0c50218` bit 0 clear |
| `0x4b8..0x532`, `0x628` | `0xc0aa0000` JEDEC `9f`; Winbond path `06`, status write `01`; quad-read fields `+68=0x32bbeb`, `+6c=0x311`, `+38=10`, `+3c=6`, `+28=1` |
| `0x6a8..0x6e8` | PLL configuration through `0xc0100000/4`; wait for both `0x80008000` lock bits; clock selection at `0xc0101000` |
| `0x6ee..0x7ba` | Two clock/reset configuration stages with delay between them |
| `0x7be..0x824` | Bus/DRAM-related settings at `0xc0c00200` and `0xc0ca0000`; poll `0xc0c82404` bit 0 |
| `0x82c..0xa12` | GPIO/pin defaults; board table records individual PC/address/value evidence |
| later boot | `0xc0c89000`, `+34`, `+40` set to 1 before runtime |

The new target is specifically the installed W25Q16, JEDEC bytes `EF 40 15`
(controller word `0x1540ef`). Other flash vendors/capacities are not supported.
Boot SPI command completion uses `+14 & 3 == 2`; runtime's job result uses
`+18 == 1`. General delays use `0xc0c82000`: clear control `+0c`, clear count
`+08` and configuration `+10`, start control, then poll the count. Counts are
preserved in the frozen first stage. The second stage and app keep this counter
free-running rather than resetting it for each delay. Its measured rate on this
board is 96 MHz; see [the timing results](STATUS.md).

## Panel and timer

Panel base is `0xc0c8d000`. Configuration `+38=0x1f`, `+3c=0x543210`, `+40=0x20`,
`+18=0x25a6`, `+1c=0x169`, `+20=6`; channel modes at `+100+8*n` are
`30,32,30,30,38,00` (hex). Controller enables at `+0/+4` are 1. Output words
are `+140+8*n`; `+14=1` commits a frame when `+70 & 0x1f == 0`.

The physically confirmed sequence from the preceding milestone is:

| Logical LED | Channel | Output register | Value |
|---|---|---|---|
| 1 (power) | 1 | `0xc0c8d148` | `0x10` |
| 2 | 0 | `0xc0c8d140` | `0x20` |
| 3 | 2 | `0xc0c8d150` | `0x20` |
| 4 | 3 | `0xc0c8d158` | `0x10` |

The user identified the **first sequence LED as power** on 2026-10-23. Rescue
now alternates channel 1 between `0x10` and zero, leaving channels 0/2/3 zero;
the demo app retains the full sequence. Other color/physical-position labels
are not yet independently recorded. Timer 1 is
`0xc0140100`, reset bit `0x80000000`, start bit 0, period `+8=0xb3b0`, `+10=1`,
`+14` low bits 3; expiry is observed by control becoming zero. Clock/reset
controls are `0xc0101314` and `0xc0101214`. Synthetic emulator expiry establishes
ordering/liveness only, not blink frequency.

## USB

USB base is `0xc0780000`; PHY/reset
setup also touches `0xc0c88000` and `0xc07f0194`. The replacement's C sequence
uses those observed transactions; it does not call the reference entry points.
The reference host-connect function at `0x200069c0` sets `0xc0c88024` bit 16;
disconnect at `0x20006a0c` clears it. This ASIC PHY enable is separate from the
core soft-disconnect bit at USB `+804`. The standalone test host additionally
requires this enable, correcting an omission in the earlier generic model.

| Offset | Used behavior |
|---|---|
| `+10` | Core reset bit 0, FIFO flush bits `0x30`, bus-idle bit 31 |
| `+14/+18` | Global W1C status/mask; reset `0x1000`, enumeration `0x2000` |
| `+800/+804/+808` | Address/speed configuration, disconnect bit 1, enumerated speed |
| `+83c` | Endpoint interrupt routing, `0x30005` enables IN0/IN2/OUT0/OUT1 |
| `+840+4*n`, `+880+4*n` | Per-IN/OUT endpoint event masks |
| `+900+32*n`, `+b00+32*n` | IN/OUT endpoint control blocks |
| EP `+08/+10/+14` | W1C completion/setup status, transfer size, DMA buffer address |

DMA buffers are 32-byte aligned in uncached RAM. Endpoint-0 transfers use
64-byte packets, setup reception uses `0x60080040` and a 64-byte buffer, bulk
uses one 64/512-byte packet per rearm. The driver tracks DATA/STATUS stages and
waits for the reboot command's status ACK before resetting. Update completion
is reported separately by the vendor INFO request; it does not trigger reset.
The host emulator consumes these DMA jobs; it never calls replacement parser
functions or sets their state variables.

### Control-endpoint repair, 2026-10-23

The first physical source-only image connected but macOS could not read its
device descriptor. Two sequencing problems were corrected in `usb.c`:

- A single poll can see both reset (`0x1000`) and enumeration (`0x2000`). The
  old code acknowledged both, handled reset and returned, losing enumeration
  and leaving EP0 unarmed. The new code handles reset, then enumeration.
- Stock `0x20005e84` calls the address-register writer before queuing status IN.
  The new C driver previously waited for the status-IN completion interrupt.
  It now sets `DCFG` before that DMA job. The upstream
  [Linux DWC2 driver](https://github.com/torvalds/linux/blob/master/drivers/usb/dwc2/gadget.c)
  likewise writes `DCFG` in `USB_REQ_SET_ADDRESS` before its reply; this is
  corroborating controller-family evidence, not an ASIC specification.

The earlier private repair tests executed reset through the loader and real
application, delivered reset/enumeration together and enforced address-before-
status ordering. The preserved old image failed those cases; the repaired image
passed them and a protected USB update. The public
[validation matrix](../tests/validate.py) retains those controller constraints
and checks both speeds. Address ordering is a stock-derived model constraint,
not a USB wire capture. Both corrections were installed together physically;
the test does not isolate which caused the original Mac failure. Current public
build results and exact image hashes are in [STATUS.md](STATUS.md).

## Flash programming

SPI base `0xc0aa0000`, DMA base `0xc0a90000`. Commands select manual mode
`SPI+34=0x101`, set opcode/shape/value at `+40/+48/+4c`, write flag at `+44`,
and trigger at `+54=1`. The reference manual-command routines wait for nonzero
`+18`, then validate the resulting ID or erased contents; their completion
test is not the DMA path's exact `==1` check. Capture the JEDEC reply at `+50`
before restoring memory mapping with `+10=0`, `+14=0`, `+34=0x100`.
The first standalone driver read the ID after restoration and used `==1` for
manual commands too. Both were corrected after the I²C hardware test returned
`A0`. The failing test did not isolate the two causes; subsequent C0 completion
and read-back verified the combined correction. Current USB and I²C programming
results are recorded in [STATUS.md](STATUS.md).

Normal app erase is WREN `06` then `d8` with shape 3 and a 64 KiB-aligned
destination. In layout 2, stage-0 maintenance instead uses `20` with shape 3
and a 4 KiB-aligned destination, confined to `0x4000–0xffff`. The W25Q16CV's
4 KiB erase opcode comes from the manufacturer instruction table; the same ASIC
manual-command path is used. This new combination is emulator-validated but
not yet physically tested; see [the 64 KiB layout record](LAYOUT64.md).
Program is an ASIC 4 KiB DMA job: source/length at `DMA+0/+4`, destination/length
at `SPI+5c/+60`, `DMA+10=0x3d`, trigger `SPI+58=1`. Both controller and DMA
results must report 1. New C verifies every word after erasing/programming.
No assumption is made that a 4 KiB job is a single physical page-program opcode.

No EEPROM marker or stock selector is required. Reset always enters first-stage
recovery, then the second-stage bootloader's timed window. The public
[bootloader guide](../README.md) specifies the app handoff and management protocol.
