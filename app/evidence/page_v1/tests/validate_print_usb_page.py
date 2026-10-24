#!/usr/bin/env python3
"""Emulate a realistic multi-plane CMYK page arriving over USB bulk OUT.

Builds a >512-byte BJL stream (4 planes CMYK, 2 rows/plane, incompressible row-0
ramps) with an independent PackBits encoder, delivers it over the printer-class
bulk endpoint, and asserts the firmware reassembles it across packet boundaries,
decodes it, and emits each plane's retained (last) row to the head-data port.

Runs at BOTH enumerated speeds -- high-speed (512-byte packets, multi-packet for
this stream) and full-speed (64-byte packets, many packets) -- so the bulk
accumulator is exercised either way.
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

LOG = ('log', setup(0xc0, 0x50, 0, 0, 256))


def pb_encode(data):
    """Independent Canon-variant PackBits encoder (oracle; see print.c decoder)."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        run = 1
        while i + run < n and data[i + run] == data[i] and run < 128:
            run += 1
        if run >= 2:
            out.append(257 - run)
            out.append(data[i])
            i += run
        else:
            start, lit = i, 0
            while i < n and lit < 128 and not (i + 1 < n and data[i] == data[i + 1]):
                i += 1
                lit += 1
            out.append(lit - 1)
            out.extend(data[start:i])
    out.append(0x80)                       # row terminator
    return bytes(out)


def esc(cmd, payload=b''):
    return bytes([0x1b, 0x28, ord(cmd), len(payload) & 0xff, len(payload) >> 8]) + payload


def build_page():
    """Return (stream, expected_head_bytes). Row 1 of each plane is retained/emitted."""
    raw = [[bytes((i * 7 + pl * 13) & 0xff for i in range(200)), None] for pl in range(4)]
    raw[0][1] = bytes([0xCC] * 5 + [0x10, 0x20, 0x30] + [0] * 12)
    raw[1][1] = bytes([0x4D, 0x4D] + [0x5A] * 10 + [0x01])
    raw[2][1] = bytes([0xFF] * 7)
    raw[3][1] = bytes([0, 0, 0, 0, 0x7F, 0x80, 0x81, 0xFF, 0xFF, 0xFF, 0xFF])

    s = bytearray()
    s += esc('I', b'\x01')
    s += esc('J', b'\x02')                 # 2 rows per plane
    s += esc('b', b'\x01')
    s += esc('c', b'\x30\x00\x00')
    s += esc('d', b'\x02\x58\x02\x58')
    s += esc('l', b'\x34\x00\x00')
    s += esc('p', b'\x00' * 46)
    s += esc('t', b'\x00' * 42)
    s += esc('L', b'CMYK')
    for pl in range(4):
        enc = pb_encode(raw[pl][0]) + pb_encode(raw[pl][1])
        s += esc('F', enc)
    s += b'\x0c'                           # end page
    s += b'\x1b\x40'                        # ESC@ end job
    expected_head = b''.join(raw[pl][1] for pl in range(4))
    return bytes(s), expected_head


def log_text(e):
    return b''.join(d for g, n, d in e.responses if n == 'log' and g == e.generation)


def run_once(factory, md, stream, expected_head, speed):
    e = PrinterModel(image=factory, metadata=md, bulk=stream, speed=speed)
    mps = 512 if speed else 64
    npk = (len(stream) + mps - 1) // mps
    e.run(lambda x: bool(x.info()) and x.info()[2] == 2)
    n = len(stream)
    e.requests.appendleft(('begin', setup(0x40, 0x61, n & 0xffff, n >> 16)))
    head_of = lambda x: [v for a, v in x.head_writes if a == 0xc0c25000]
    e.run(lambda x: len(head_of(x)) >= len(expected_head), resume=True, budget=400000000)

    head = bytes(head_of(e))
    label = f"{'high' if speed else 'full'}-speed mps={mps} packets~{npk}"
    assert head == expected_head, f'{label}: head {head.hex()} != {expected_head.hex()}'
    print(f"PASS usb_bulk_page_{'hs' if speed else 'fs'}: {label} stream={n}B "
          f"head={len(head)}B reassembled+decoded OK")
    # drain log for the decode diagnostics
    for _ in range(120):
        e.requests.append(LOG)
    e.run(lambda x: b'emitted head bytes' in log_text(x), resume=True, budget=80000000)
    t = log_text(e)
    assert b'mandatory=0x000000ff' in t, t
    assert b'planes=4' in t, t


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes()
    app = (APP / 'build/application.bin').read_bytes()
    factory = (boot + app).ljust(0x200000, b'\xff')
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
          'symbols': {'bootloader': bs['bootloader'], 'application': asym}}

    stream, expected_head = build_page()
    assert len(stream) > 512, f'stream {len(stream)} must exceed one high-speed packet'
    run_once(factory, md, stream, expected_head, speed=True)
    run_once(factory, md, stream, expected_head, speed=False)
    print('ALL PRINT USB PAGE ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
