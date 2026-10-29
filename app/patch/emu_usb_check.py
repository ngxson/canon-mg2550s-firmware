#!/usr/bin/env python3
"""Differential USB-enumeration check for app/patch in the emulator.

The emulator models USB readiness synthetically, so it cannot by itself
reproduce a real-host enumeration failure. What it CAN do is tell whether a
patch *changes* the stock app's USB bring-up: run two candidates the same way
and compare how far the USB device software advances.

For each candidate we boot from reset through the real loader and run the boot
message loop, then read:
  * USB software state block 0x1804099c  (state, mode, initialised, attached)
  * USB device counters 0x18040c88..0x18040cac (reset/addr/config/VBUS events)
  * USB PHY / connect gates 0xc0780804 / 0xc0c88024
plus the final PC / error / SVC + IRQ counts.

Run in Docker:
  docker run --rm --network none -v "$PWD:/project:rw" -w /project \
    --entrypoint bash mg2550s-emu -c \
    "timeout 600 python3 app/patch/emu_usb_check.py \
       --candidates emu/output/stockapp/patch_base emu/output/stockapp/patch_ink"
"""
import argparse, json, struct, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'emu' / 'stockapp'))
sys.path.insert(0, str(ROOT / 'emu'))
from probe import Boot  # noqa: E402

BOOT_BUILD = ROOT / 'bootloader' / 'build' / 'vectors'
EEPROM = ROOT / 'emu' / 'inputs' / 'eeprom.bin'

COUNTERS = [('HardPwrOn', 0x18040c88), ('SoftPwrOn', 0x18040c8c),
            ('DoPwrOff', 0x18040c90), ('VBusOn', 0x18040c94),
            ('BusRst', 0x18040c98), ('BusRstEnd', 0x18040c9c),
            ('SetAddrEnd', 0x18040ca0), ('SetCfgEnd', 0x18040ca4),
            ('VBusOff', 0x18040ca8), ('VBusOffEnd', 0x18040cac)]


def probe_one(candidate, msgs, instrs):
    emu = Boot(candidate=candidate, from_reset=True, boot_build=BOOT_BUILD, eeprom=EEPROM)
    run_err = None
    try:
        emu.boot()
        emu.run_boot(msgs, instrs)
    except Exception as exc:                       # keep whatever state we reached
        run_err = f'{type(exc).__name__}: {str(exc)[:200]}'

    def r32(a):
        try: return struct.unpack('<I', emu.uc.mem_read(a, 4))[0]
        except Exception: return None
    res = emu.result() if hasattr(emu, 'result') else {}
    return {
        'candidate': str(candidate),
        'run_error': run_err,
        'final_pc': res.get('pc'),
        'emu_error': res.get('error'),
        'stop_reason': res.get('stop_reason'),
        'svcs': res.get('SVC_exceptions'),
        'irqs': getattr(emu, 'irqs', None),
        'usb_state_block': list(emu.uc.mem_read(0x1804099c, 4)),
        'usb_counters': {n: r32(a) for n, a in COUNTERS},
        'usb_phy_0xc0780804': r32(0xc0780804),
        'usb_connect_0xc0c88024': r32(0xc0c88024),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidates', nargs='+', type=Path, required=True,
                   help='candidate output directories (each holds candidate.bin + manifest.json)')
    p.add_argument('--msgs', type=int, default=1500)
    p.add_argument('--instrs', type=int, default=80_000_000)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    reports = [probe_one(d / 'candidate.bin', args.msgs, args.instrs) for d in args.candidates]
    out = {'reports': reports}
    print(json.dumps(out, indent=2))
    if args.output:
        args.output.write_text(json.dumps(out, indent=2) + '\n')


if __name__ == '__main__':
    main()
