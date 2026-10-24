#!/usr/bin/env python3
"""Emulate the front-panel key probe (USB 0x70).

The firmware reads PANEL+0x28 (0xc0c8d028) each loop, debounces (>=3 equal
samples) and logs the value on change. This drives the model's 0xc0c8d028 read
value through a scripted idle -> press -> release sequence and asserts the log
reports each stable value exactly once, in order. This validates the probe's
sample/debounce/log logic; the physical bit<->button map needs real presses.
"""
import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests'))
sys.path.insert(0, str(APP / 'tests'))
from model import setup                   # noqa: E402
from model_printer import PrinterModel     # noqa: E402

KEY = 0xc0c8d028
LOG = ('log', setup(0xc0, 0x50, 0, 0, 256))


def app_mode(e):
    return bool(e.info()) and e.info()[2] == 2


def log_text(e):
    return b''.join(d for g, n, d in e.responses if n == 'log' and g == e.generation)


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes()
    app = (APP / 'build/application.bin').read_bytes()
    factory = (boot + app).ljust(0x200000, b'\xff')
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
          'symbols': {'bootloader': bs['bootloader'], 'application': asym}}

    e = PrinterModel(image=factory, metadata=md)
    e.regs[KEY] = 0                        # idle
    e.run(app_mode)
    e.requests.appendleft(('btn', setup(0x40, 0x70)))         # enable probe

    def pump(predicate, budget=80000000):
        for _ in range(30):
            e.requests.append(LOG)
        e.run(predicate, resume=True, budget=budget)

    # 1) debounce + report ordering with the Power key (no action bound)
    pump(lambda x: b'028=0x00000000' in log_text(x))
    e.regs[KEY] = 0x40                      # Power
    pump(lambda x: b'028=0x00000040' in log_text(x))
    e.regs[KEY] = 0
    pump(lambda x: log_text(x).count(b'028=0x00000000') >= 2)

    t = log_text(e)
    assert b'[btn] probe enabled' in t, t
    seq = [ln.split(b'=')[1] for ln in t.split(b'\n') if ln.startswith(b'[btn] 028=')]
    assert seq[:3] == [b'0x00000000', b'0x00000040', b'0x00000000'], seq
    print('PASS buttons_probe: reported', [v.decode() for v in seq[:3]])

    # 2) Cancel toggles continuous illumination (scanner path), motor untouched
    nmotor0 = len(e.motor_writes)
    e.regs[KEY] = 0x20                      # Cancel -> lamp ON
    pump(lambda x: b'illumination ON' in log_text(x))
    assert e.regs.get(0xc0c85614) == 1, 'scanner power not asserted on lamp ON'
    e.regs[KEY] = 0
    pump(lambda x: log_text(x).count(b'028=0x00000000') >= 3)
    e.regs[KEY] = 0x20                      # Cancel -> lamp OFF
    pump(lambda x: b'illumination OFF' in log_text(x))
    assert e.regs.get(0xc0c85614) == 0, 'scanner power not cleared on lamp OFF'
    assert len(e.motor_writes) == nmotor0, f'motor registers written during lamp toggle: {e.motor_writes}'
    print('PASS lamp_toggle: illumination ON/OFF via Cancel, 0xc0c85614 1->0, motor untouched')

    # The Copy keys are bound to hands-on paper-feed pulses for the owner's bench tests only.
    # Emulator coverage of the feed motor uses the USB request (validate_printregs.py), not keys.
    print('ALL BUTTON PROBE ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
