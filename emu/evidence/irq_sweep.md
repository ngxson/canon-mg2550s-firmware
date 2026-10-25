# Stock interrupt sources: black-box handler map (2026-10-25)

Each VIC source 0..122 was forced once after the modeled boot reached idle
(`emu/run.py boot --probe-source S`, `stock/irqprobe.py`); the unmodified stock
vector, reader, handler and RTOS ran. Listed per source: the handler address
from the stock ISR table and the peripheral registers the handler touched
within two ticks (the periodic key, sense and ADC polls of other tasks are
filtered out). A forced source is evidence of what the handler does when it
is raised, not that hardware raises it. No handler posted an RTOS event in
this probe: with the modeled registers at zero the handlers found nothing
pending. Sources with no peripheral touch are listed in the second table.

| Source | Handler | Registers touched | New code (instructions, approx.) | Reading |
|---:|---|---|---:|---|
| 6 | `0xe8be9` | `0xc0c90020` | 39 | ADC block |
| 9 | `0xd369b` | `0xc0c51404` | 14 | CR motor primary status |
| 12 | `0xd8b51` | `0xc0c53404` | 10 | LF motor primary status |
| 14 | `0xd8a8f` | `0xc0c52104` | 14 | LF companion status |
| 25 | `0xf54a7` | `0xc0c5714c`, `0xc0d60158` | 87 | scanner motor + 0xc0d60158 |
| 26 | `0xf633d` | `0xc0c57404` | 19 | scanner motor primary status |
| 27 | `0xf634b` | `0xc0c56104` | 18 | scanner companion status |
| 28 | `0xf6367` | `0xc0c56008`, `0xc0c57100`, `0xc0c57160`, `0xc0c57200`, `0xc0c57204`, `0xc0c57208`, `0xc0c5720c`, `0xc0c57210`, `0xc0c57304` | 505 | scanner motor position/period block |
| 29 | `0xf54bd` | `0xc0120110` | 17 | scanner controller 0xc0120110 |
| 44 | `0x9f19b` | `0xc0c2101c` | 8 |  |
| 45 | `0x9f593` | `0xc0c25b04` | 8 |  |
| 47 | `0xca901` | `0xc0c29030` | 6 | head work engine status (the briefs' IRQ 0x2f) |
| 52 | `0x11188b` | `0xc0b20100` | 0 | EEPROM/I2C status (modeled) |
| 58 | `0x117f0f` | `0xc0180004`, `0xc0180014` | 124 | 0xc018xxxx block |
| 59 | `0x117f11` | `0xc0180014` | 10 |  |
| 67 | `0x51843` | `0xc0780014`, `0xc0780018`, `0xc0780838`, `0xc078083c` | 61 | **USB core: interrupt status/mask 0xc0780014/18 and 0xc0780838/3c** |
| 68 | `0x5184b` | `0xc0750084` | 14 | 0xc0750084 |
| 69 | `0x51853` | `0xc075008c` | 14 | 0xc075008c |
| 85 | `0x147d68` | `0xc0d00084` | 89 | 0xc0d00084 |
| 88 | `0x75d8d` | `0xc0d00170` | 33 | 0xc0d00170 |

Sources whose handler touched no peripheral in the probe window:

1:`0xdbf0d`, 2:`0xd5b7b`, 3:`0xddfdf`, 4:`0x1096a9`, 5:`0x1096a9`, 7:`0x1096a9`, 8:`0x1096a9`, 10:`0x1096a9`, 11:`0xd35c5`, 13:`0x1096a9`, 15:`0x1096a9`, 16:`0x1096a9`, 17:`0x1096a9`, 18:`0x1096a9`, 19:`0x1096a9`, 20:`0x1096a9`, 21:`0x1096a9`, 22:`0x1096a9`, 23:`0x1096a9`, 24:`0xf5495`, 30:`0x1096a9`, 31:`0x1096a9`, 32:`0x1096a9`, 33:`0x1096a9`, 34:`0x1096a9`, 35:`0x1096a9`, 36:`0x1096a9`, 37:`0x1096a9`, 38:`0x1096a9`, 39:`0x1096a9`, 40:`0x1096a9`, 41:`0x1096a9`, 42:`0x1096a9`, 43:`0x1096a9`, 46:`0x1096a9`, 48:`0x1096a9`, 49:`0x1096a9`, 50:`0x1096a9`, 51:`0x1096a9`, 53:`0x1096a9`, 54:`0x1096a9`, 55:`0x1096a9`, 56:`0x1541dc`, 57:`0x1096a9`, 60:`0x1096a9`, 61:`0x1096a9`, 62:`0x1096a9`, 63:`0x1096a9`, 64:`0x1096a9`, 65:`0x1096a9`, 66:`0x1096a9`, 70:`0x1096a9`, 71:`0x1096a9`, 72:`0x1096a9`, 73:`0x1096a9`, 74:`0x1096a9`, 75:`0x1096a9`, 76:`0x1096a9`, 77:`0x1096a9`, 78:`0x1096a9`, 79:`0x1096a9`, 80:`0x1096a9`, 81:`0x1096a9`, 82:`0x1096a9`, 83:`0x1096a9`, 84:`0x1096a9`, 86:`0x1096a9`, 87:`0x75d91`, 89:`0x1096a9`, 90:`0x1096a9`, 91:`0x1096a9`, 92:`0x1096a9`, 93:`0x1096a9`, 94:`0x1096a9`, 95:`0x1096a9`, 96:`0xe24df`, 97:`0x1096a9`, 98:`0x1096a9`, 99:`0x1096a9`, 100:`0x1096a9`, 101:`0x1096a9`, 102:`0x1096a9`, 103:`0x1096a9`, 104:`0x1096a9`, 105:`0x1096a9`, 106:`0x113785`, 107:`0x113785`, 108:`0x113785`, 109:`0x113785`, 110:`0x113785`, 111:`0x113785`, 112:`0x113785`, 113:`0x113785`, 114:`0x113785`, 115:`0x113785`, 116:`0x113785`, 117:`0x113785`, 118:`0x113785`, 119:`0x113785`, 120:`0x113785`, 121:`0x113785`, 122:`0x113785`


## USB handler (source 67) versus core status bits

Each run presets `0xc0780014` (core interrupt status) and `0xc0780018` (mask)
to one bit, then forces source 67 once (`--preset c0780014=V,c0780018=V`).
Bits are named after the replacement driver's use of the same core
(`bootloader/src/usb.c`); the stock reaction is observed, not decoded.

| Bit | Our driver's meaning | Stock handler reaction |
|---:|---|---|
| 7 (`0x80`) | — | three messages to task 60's mailbox 0x3f |
| 11 (`0x800`) | — | writes mask `0x18 = 0x80000000`, one message to task 60 |
| 12 (`0x1000`) | bus reset | reset path (about 340 new instructions), one message to task 60 |
| 13 (`0x2000`) | enumeration done | arms endpoint 0 for SETUP as our driver does: `0x880 = 8`, `0xb00 = 0x80000000`, `0xb10 = 0x6008e59f`, `0xb14 = 0xc07e0640` (SETUP buffer), one message to task 60 |
| 31 (`0x80000000`) | — | writes mask `0x18 = 0x800`, one message to task 60 |
| all others | — | status acknowledged (`0x14` written back), nothing else |

So task 60 is stock's USB worker (mailbox 0x3f); the three tasks waiting on
events 0x30..0x32 are not in the enumeration path.

## Forced attach: how far the stock USB stack runs in the model

`boot --usb --usb-source 67 --usb-force --usb-attach 300 --usb-mask 0xffffffff`
(stock never clears soft-disconnect, so the host attach and the core mask are
model assumptions). Observed: reset handled, enumeration-done handled, the
worker task 60 arms endpoint 0 OUT for SETUP exactly as our driver does
(`0xb10 = 0x6008e59f`, `0xb14 = 0xc07e0640`, `0xb00 = 0x80000000`,
`0x880 = 8`), the host delivers SET_ADDRESS into that buffer, the handler
acknowledges the endpoint interrupt (reads `0x838/0x83c` for the endpoint
bitmap, `0xb08`, `0x880`; writes `0x880 = 0`, `0xb08 = 8`) and returns
without waking the worker. Model additions made for this: endpoint summary
bits 18/19 in the core status with the bitmap at `0x818`, mirrored into
`0x838/0x83c`; SETUP status 9; packet and SETUP counts kept in `0xb10`;
endpoint buffers in the controller's address space kept readable. The
hand-off after a SETUP is the open question (brief 9).

## Hardware-in-the-loop boot (2026-10-26, build a2d4e589 on the printer)

`boot --tunnel host.docker.internal:7731 --forward c0c80000-c0c88000,c0c8d000-c0c8e000`
with `tools/tunnel_proxy.py` (reads only): 10,330 stock reads of the GPIO,
sense and panel registers were answered by the real board, mean 1.02 ms
each, no refusals, 2000 ticks in 4 min 48 s. Board values seen: key register
`0xc0c8d028 = 0x8` (idle; the modeled zero was wrong), `0xc0c83e18 = 1`,
`0xc0c85408 = 1`, `0xc0c84688 = 0`, `0xc0c8121c = 0x300`, `0xc0c840c4 = 0`,
`0xc0c81000 = 3`. The stock boot reached the same idle as with modeled
inputs: 106 motor writes, the single soft-disconnect write, the same task
waits (USB task 61 on event 0x30). So at rest the real inputs do not take
stock out of standby either; the exit input is not asserted at rest.

## Panel key bits measured through the tunnel (2026-10-26)

Polling `0xc0c8d028` on the printer at 12 Hz while the owner held keys:
idle `0x8`; Cancel held gives `0x28` (bit 0x20, as the replacement firmware
uses); **the power key held gives `0x48` (bit 0x40)**, the "fourth key" the
emulator matrix found. `0xc0c8d02c` moved from `0x48` to `0x68` at the first
press and stayed. `0xc0c83e28` pulses high for about 100 ms every one to
three seconds at rest (a periodic sensor or heartbeat line, not a key).
