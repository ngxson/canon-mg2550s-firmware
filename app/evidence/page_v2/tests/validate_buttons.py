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

    # 3) Copy-BW jogs the carriage one bounded pulse: enable C+0=0x102 then stop=0,
    #    and the firmware returns (bounded) -- i.e. the pulse self-terminates.
    e.regs[KEY] = 0
    pump(lambda x: log_text(x).count(b'028=0x00000000') >= 4)
    e.regs[KEY] = 0x400                     # Copy-BW -> jog dir 0
    pump(lambda x: b'[motor] stop:' in log_text(x))
    cwr = [v for a, v in e.motor_writes if a == 0xc0c56000]
    assert 0x102 in cwr, f'motor output-enable (C+0=0x102) not issued: {cwr}'
    assert cwr[-1] == 0, f'motor not left stopped (C+0 last = {cwr[-1]:#x})'
    assert b'[motor] jog forward' in log_text(e), 'jog did not arm'
    print(f'PASS motor_jog: C+0 sequence {[hex(v) for v in cwr]} -> target move armed + bounded stop')
    print('ALL BUTTON PROBE ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
