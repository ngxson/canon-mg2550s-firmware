#!/usr/bin/env python3
"""Validate the printer-side emulation scaffolding.

1. The existing scanner capture must still pass under PrinterModel (the printer
   model is a clean extension that models the motor + head-data blocks without
   breaking the scanner/management path).
2. Smoke-test the modeled motor completion mechanism (a running motor raises its
   done bit after a few polls) — the hook a future bounded motor-wait relies on.
"""
import json
import struct
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests'))
sys.path.insert(0, str(APP / 'tests'))
from model import setup                 # noqa: E402
from model_printer import PrinterModel   # noqa: E402

LINE_U16 = 0xc00


def app_mode(e):
    return bool(e.info()) and e.info()[2] == 2


def main():
    boot_region = (BOOT / 'build/boot_region.bin').read_bytes()
    app = (APP / 'build/application.bin').read_bytes()
    factory = (boot_region + app).ljust(0x200000, b'\xff')
    boot_syms = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    app_syms = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    cap_buf = int(app_syms['cap_buf'], 16)
    metadata = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
                'symbols': {'bootloader': boot_syms['bootloader'], 'application': app_syms}}
    image = lambda i: (0x6400 + (i & 0xff)) & 0xffff

    # 1) scanner capture still works under the printer model
    e = PrinterModel(image=factory, metadata=metadata, scan_image=image)
    e.run(app_mode)
    e.requests.appendleft(('scan', setup(0x40, 0x59, 0x04)))
    e.run(lambda x: x.scan_captures >= 1, resume=True, budget=120000000)
    data = struct.unpack('<%dH' % LINE_U16, bytes(e.uc.mem_read(cap_buf, LINE_U16 * 2)))
    assert all(data[i] == image(i) for i in range(LINE_U16)), 'scanner capture broke under PrinterModel'
    print('PASS scanner_capture_under_printer_model')
    assert isinstance(e.head_writes, list) and isinstance(e.motor_writes, list)
    print(f'  (head-data writes observed: {len(e.head_writes)}, motor-family writes: {len(e.motor_writes)})')

    # 2) modeled motor completion: start the scanner carriage motor, then poll its
    #    done register; after `ticks` polls the completion bit must appear.
    m = e.motors['scanner_cr']
    e.regs[m['done']] = 0
    e.motor_writes.append((m['enable'], m['start'])); m['running'] = True; m['polls'] = 0
    done = False
    for _ in range(m['ticks'] + 2):
        e.read(e.uc, (m['done'] - 0xc0000000), 4, None)
        if e.regs.get(m['done'], 0) & m['bit']:
            done = True; break
    assert done, 'modeled motor never reported completion'
    print(f"PASS motor_completion_modeled (done bit raised after <= {m['ticks']} polls)")
    print('ALL PRINTER EMU SCAFFOLDING ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
