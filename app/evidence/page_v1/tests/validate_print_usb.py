#!/usr/bin/env python3
"""Emulate a real BJL print stream arriving over the USB printer-class bulk OUT endpoint.

Drives PRINT_BEGIN (0x61) with the stream length, then the model delivers the BJL
band on bulk EP1. The firmware accumulates it, decodes through print.c, and emits
the decoded dots to the head-data port -- asserted via PrinterModel.head_writes.
This exercises the real transport (bulk OUT -> print_bulk_feed -> print_process),
not the embedded-band self-test.
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

# Same minimal BJL band the firmware embeds: 8 mandatory + L "K" + F raster + ESC@.
STREAM = bytes([
    0x1b, 0x28, 0x49, 0x01, 0x00, 0x01,                      # ESC(I 01
    0x1b, 0x28, 0x4a, 0x01, 0x00, 0x01,                      # ESC(J 01 (1 row/plane)
    0x1b, 0x28, 0x62, 0x01, 0x00, 0x01,                      # ESC(b 01
    0x1b, 0x28, 0x63, 0x00, 0x00,                            # ESC(c
    0x1b, 0x28, 0x64, 0x00, 0x00,                            # ESC(d
    0x1b, 0x28, 0x6c, 0x00, 0x00,                            # ESC(l
    0x1b, 0x28, 0x70, 0x00, 0x00,                            # ESC(p
    0x1b, 0x28, 0x74, 0x00, 0x00,                            # ESC(t
    0x1b, 0x28, 0x4c, 0x01, 0x00, 0x4b,                      # ESC(L "K"
    0x1b, 0x28, 0x46, 0x05, 0x00, 0xfc, 0xff, 0xfc, 0x00, 0x80,  # ESC(F raster
    0x1b, 0x40,                                              # ESC@
])

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

    e = PrinterModel(image=factory, metadata=md, bulk=STREAM)
    e.run(app_mode)
    n = len(STREAM)
    # PRINT_BEGIN (0x61): wValue|wIndex<<16 = total. Named 'begin' so the model
    # arms its bulk sender once the firmware acks it.
    e.requests.appendleft(('begin', setup(0x40, 0x61, n & 0xffff, n >> 16)))
    # Run until the decoded dots reach the head-data port -- the authoritative
    # end-to-end signal (the debug log is drained separately below).
    head_of = lambda x: [v for a, v in x.head_writes if a == 0xc0c25000]
    e.run(lambda x: len(head_of(x)) >= 10, resume=True, budget=120000000)

    head = head_of(e)
    expected = [0xff] * 5 + [0x00] * 5
    assert head == expected, f'head-data out {head} != {expected}'
    print(f'PASS usb_bulk_to_head_data_out: head port received {head}')

    # Now drain the debug log to confirm the decode diagnostics.
    for _ in range(80):
        e.requests.append(LOG)
    e.run(lambda x: b'emitted head bytes' in log_text(x), resume=True, budget=60000000)
    t = log_text(e)
    assert b'bulk begin total=' in t, t
    assert b'mandatory=0x000000ff' in t, t
    assert b'rowbytes=10' in t, t
    line = t.split(b'[print] r=')[1].split(b'\n')[0].decode('latin1')
    print('PASS usb_bulk_stream_decoded: [print] r=' + line)
    print('ALL PRINT USB BULK ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
