#!/usr/bin/env python3
"""Actual ARM page orchestration with scripted motor feedback and synthetic DMA.
Checks row ownership/ACKs, fresh images, positioning, return home and failure
cleanup. This verifies software contracts, not optical quality or motor physics.
"""
import hashlib
import json
import struct
from pathlib import Path
from validate_home import HomeModel, setup, TICKS_PER_MS
from model_scanner import ScannerModel

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'


class PageModel(HomeModel, ScannerModel):
    def __init__(self, **kw):
        self.capture_fault = False
        self.bad_destination = False
        super().__init__(**kw)

    def _scan_dma(self, cpu):
        assert self.drive is None, 'capture while motor powered'
        if self.capture_fault:
            return
        self.scan_image = lambda i: (0x3000 + self.scan_captures * 257 + i) & 0xffff
        super()._scan_dma(cpu)


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes()
    app = (APP / 'build/application.bin').read_bytes()
    syms = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    meta = {'layout': {'version': 2, 'application': [0x10000, 0x200000]}, 'symbols': {
        'application': syms,
        'bootloader': json.loads((BOOT / 'build/manifest.json').read_text())['symbols']['bootloader']}}
    report = {'assumptions': __doc__, 'app_sha256': hashlib.sha256(app).hexdigest(), 'cases': {}}

    def phase(e):
        return e.uc.mem_read(int(syms['page_phase'], 16), 1)[0]

    def request(e, name, packet):
        before = len(e.responses)
        e.requests.appendleft((name, packet))
        e.run(lambda x: any(n == name for _, n, _ in x.responses[before:]), resume=True)
        return next(d for _, n, d in e.responses[before:] if n == name)

    def booted():
        e = PageModel(image=(boot + app).ljust(0x200000, b'\xff'), metadata=meta)
        e.run(lambda x: bool(x.info()) and x.info()[2] == 2)
        return e

    def start(e, rows=4):
        request(e, 'start', setup(0x40, 0x77, rows, 512))

    def ready(e):
        e.run(lambda x: phase(x) in (4, 8, 9), resume=True)
        assert phase(e) == 4, phase(e)
        return struct.unpack('<16I', request(e, 'status', setup(0xc0, 0x78, length=64)))

    def clean(name, e, expected):
        assert phase(e) == expected, (name, phase(e))
        assert e.drive is None and e.regs[0xc0c56000] == 0
        assert e.regs[0xc0c85614] == 0
        assert not e.erases and not e.programs
        assert bytes(e.uc.mem_read(0, 0x10000)) == boot
        report['cases'][name] = {'phase': expected, 'captures': e.scan_captures,
                                 'protected_prefix_unchanged': True}
        print('PASS', name, flush=True)

    e = booted(); start(e)
    origin = None
    for row in range(4):
        s = ready(e)
        assert s[:4] == (0x4750534d, 1, 4, 0) and s[4:9] == (row, 4, 2592, 5184, 512), s
        if origin is None: origin = s[9]
        assert s[11] == s[12] == origin + row * 512
        # A premature ACK must stall, never advance the immutable row.
        stalls = len(e.stalls); e.requests.appendleft(('early_ack', setup(0x40, 0x7a, row)))
        e.run(lambda x: len(x.stalls) > stalls, resume=True)
        assert phase(e) == 4
        raw = b''.join(request(e, 'data', setup(0xc0, 0x79, chunk, row, 256)) for chunk in range(21))
        values = struct.unpack('<2592H', raw)
        assert values == tuple(0x3000 + row * 257 + i for i in range(2592))
        assert request(e, 'retry_data', setup(0xc0, 0x79, 0, row, 256)) == raw[:256]
        request(e, 'ack', setup(0x40, 0x7a, row))
        request(e, 'retry_ack', setup(0x40, 0x7a, row))
    e.run(lambda x: phase(x) in (7, 8), resume=True)
    clean('full_sequence_fresh_rows_return_home', e, 7)
    assert e.regs[0xc0c57100] == origin

    e = booted(); e.capture_fault = True; start(e)
    e.run(lambda x: phase(x) == 8, resume=True)
    clean('missing_dma_rejected', e, 8)

    e = booted(); start(e); ready(e)
    deadline = e.counter + 15100 * TICKS_PER_MS
    e.run(lambda x: phase(x) == 8 or x.counter >= deadline, resume=True, budget=180000000)
    clean('abandoned_host_stops', e, 8)

    for name, cancel in [('stuck_forward_step', False), ('cancel_during_step', True)]:
        e = booted(); start(e); ready(e)
        for chunk in range(21):
            request(e, 'data', setup(0xc0, 0x79, chunk, 0, 256))
        e.behavior = 'frozen'
        request(e, 'ack', setup(0x40, 0x7a, 0))
        e.run(lambda x: x.drive is not None, resume=True)
        if cancel:
            request(e, 'stop', setup(0x40, 0x76))
        else:
            e.run(lambda x: phase(x) == 8, resume=True)
        clean(name, e, 9 if cancel else 8)

    for name, button in [('usb_cancel', False), ('button_cancel', True)]:
        e = booted(); start(e); ready(e)
        if button:
            e.keys = 0x20
            e.run(lambda x: phase(x) == 9, resume=True)
        else:
            request(e, 'stop', setup(0x40, 0x76))
        clean(name, e, 9)

    e = booted(); start(e); ready(e)
    # Legacy captures and independent motion cannot clobber an owned row.
    for code in (0x59, 0x73, 0x74, 0x60):
        stalls = len(e.stalls); e.requests.appendleft(('conflict', setup(0x40, code)))
        e.run(lambda x: len(x.stalls) > stalls, resume=True)
        assert phase(e) == 4
    request(e, 'info_live', setup(0xc0, 0x42, length=32))
    request(e, 'reboot', setup(0x40, 0x41, 1))
    e.run(lambda x: x.generation > 1, resume=True)
    assert e.regs[0xc0c56000] == 0 and e.regs[0xc0c85614] == 0
    report['cases']['live_management_conflict_and_reboot'] = {'passed': True}
    print('PASS live_management_conflict_and_reboot', flush=True)
    report['all_assertions_passed'] = True
    (APP / 'build/validation_page.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL PAGE ASSERTIONS PASSED', flush=True)


if __name__ == '__main__':
    main()
