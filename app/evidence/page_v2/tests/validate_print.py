#!/usr/bin/env python3
"""Emulate the firmware running the print receiver.

Triggers USB 0x60 (print_selftest) which decodes an embedded BJL band through
print.c in the ARM firmware, then checks the debug log shows a correct decode:
all 8 mandatory commands, the K plane, and the PackBits-decoded raster row.
"""
import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests'))
sys.path.insert(0, str(APP / 'tests'))
from model import setup            # noqa: E402
from model_printer import PrinterModel  # noqa: E402

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
    e.run(app_mode)
    e.requests.appendleft(('print', setup(0x40, 0x60)))
    for _ in range(40):
        e.requests.append(LOG)
    e.run(lambda x: b'emitted head bytes' in log_text(x), resume=True, budget=60000000)
    t = log_text(e)
    line = t.split(b'[print]')[1].split(b'\n')[0].decode('latin1')
    assert b'mandatory=0x000000ff' in t, t
    assert b'rowbytes=10' in t, t
    assert b'row0: 000000ff 000000ff 000000ff 000000ff 000000ff 00000000' in t, line
    print('PASS print_receiver_emulated: [print]' + line)

    # end-to-end: decoded dots reach the head-data block (0xc0c25000), observed by the model
    head = [v for a, v in e.head_writes if a == 0xc0c25000]
    expected = [0xff] * 5 + [0x00] * 5
    assert head == expected, f'head-data out {head} != {expected}'
    print(f'PASS stream_to_head_data_out: head port received {head}')
    print('ALL PRINT EMU ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
