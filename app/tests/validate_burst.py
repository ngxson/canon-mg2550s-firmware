#!/usr/bin/env python3
"""Diagnostic N-line burst capture and park, with a modelled IRQ19.

The model raises the IRQ19 group-pending bit (0xc0130020 bit 1) every few
polls while it is enabled in the controller, clears it on the source
acknowledgement (0xc0133104 bit 4), and on each line request DMA-writes the next
row at the 0x1440-byte stride that the static analysis of the stock finite path
describes. This checks our sequencing and accounting (N requests, N+4 events,
fresh rows, untouched reserved row and guard), not the hardware contract.
"""
import json
import re
import struct
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'bootloader' / 'tests'))
sys.path.insert(0, str(APP / 'tests'))
from model import checked, setup  # noqa: E402
from validate_page import PageModel  # noqa: E402
from model_scanner import ScannerModel  # noqa: E402
import validate_escl as v  # noqa: E402


class BurstModel(PageModel):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.polls = 0
        self.pending = False
        self.line = 0
        self.irq_events = 0

    @checked
    def read(self, cpu, offset, size, user):
        address = 0xc0000000 + offset
        if address == 0xc0130020:
            if self.regs.get(0xc0130028, 0) & 2:
                self.polls += 1
                if not self.pending and self.polls % 40 == 0:
                    self.pending = True; self.irq_events += 1
            return 2 if self.pending else 0
        return super().read(cpu, offset, size, user)

    @checked
    def write(self, cpu, offset, size, value, user):
        address = 0xc0000000 + offset
        if address == 0xc0133104 and value & 0x10:
            self.pending = False
        if address == 0xc0130028 and value & 2 and not self.regs.get(0xc0130028, 0) & 2:
            self.line = 0                    # a new acquisition starts at row 0
        result = super().write(cpu, offset, size, value, user)
        if address == 0xc013002c and value & 2:   # controller disable register
            self.regs[0xc0130028] = self.regs.get(0xc0130028, 0) & ~2
            self.pending = False
        return result

    def _scan_dma(self, cpu):
        assert self.drive is None, 'capture while motor powered'
        k = self.line; self.line += 1
        for ch in (0, 1):
            dest = self.regs.get(0xc0d50014 + ch * 0x100, 0) + k * 0x1440
            limit = self.regs.get(0xc0d5001c + ch * 0x100, 0)
            assert dest + 0xa20 <= limit, 'row beyond the programmed window'
            cpu.mem_write(dest, b''.join(struct.pack('<H', (0x4000 + k * 0x100 + ch * 0x10 + i) & 0xffff)
                                         for i in range(1296)))
        self.regs[0xc0d50010] = self.regs.get(0xc0d50010, 0) | 4
        self.regs[0xc0d50110] = self.regs.get(0xc0d50110, 0) | 4
        self.regs[0xc0d60010] = self.regs.get(0xc0d60010, 0) + 1     # SCNIO read count
        self.scan_captures += 1


def main():
    factory, app, metadata = v.factory_and_metadata(APP / 'build')
    symbols = metadata['symbols']['application']
    e = BurstModel(image=factory, metadata=metadata)
    e.run(v.app_mode)

    def log_until(pattern):
        text = ''
        for _ in range(400):
            e.requests.append(('log', setup(0xc0, 0x50, 0, 0, 256)))
            e.run(lambda x: x.requests == type(x.requests)() and x.current is None, resume=True)
            text += ''.join(d.decode('latin1') for g, n, d in e.responses[-1:] if n == 'log')
            if re.search(pattern, text):
                return text
        raise AssertionError('log pattern not seen: ' + pattern)

    def phase():
        return e.uc.mem_read(int(symbols['page_phase'], 16), 1)[0]

    # Park: home, hop to 5120 counts, stop there with drive off and no capture.
    e.requests.append(('park', setup(0x40, 0x7b, 20)))
    e.run(lambda x: phase() in (7, 8, 9), resume=True, budget=400_000_000)
    assert phase() == 7 and e.drive is None and e.scan_captures == 0
    home = struct.unpack('<I', e.uc.mem_read(int(symbols['home_position'], 16), 4))[0]
    offset = e.regs[0xc0c57100] - home
    assert abs(offset - 5120) <= 48, offset
    print('PASS park_at_offset', json.dumps({'offset': offset}), flush=True)

    for n in (1, 4, 8):
        e.requests.append(('burst', setup(0x40, 0x5b, n | 6 << 8, 12)))
        text = log_until(r'\[burst\] guard=\d')
        m = re.search(r'done=(\d) raw=(\d+) requests=(\d+)', text)
        assert m and m.groups() == ('1', str(n + 4), str(n)), (n, m and m.groups())
        rows = re.findall(r'row (\d+) ch(\d) sentinel=(\d+)', text)
        for k, ch, sentinel in rows:
            assert int(sentinel) == (0 if int(k) < n else 1296), (n, k, ch, sentinel)
        assert '[burst] guard=1' in text and len(rows) == 2 * (n + 1)
        chunks = []
        for chunk in range(0, (n * 0x1440 + 255) // 256):
            e.requests.append(('dump', setup(0xc0, 0x5c, chunk, 0, 256)))
            e.run(lambda x: x.responses[-1][1] == 'dump' and x.current is None and not x.requests, resume=True)
            chunks.append(bytes(e.responses[-1][2]))
        data = b''.join(chunks)[:n * 0x1440]
        for k in range(n):
            for ch in (0, 1):
                seg = data[k * 0x1440 + ch * 0xa20:k * 0x1440 + (ch + 1) * 0xa20]
                assert struct.unpack_from('<H', seg, 0)[0] == (0x4000 + k * 0x100 + ch * 0x10) & 0xffff
        print('PASS burst', json.dumps({'n': n, 'events': n + 4, 'requests': n}), flush=True)
    assert e.drive is None and e.regs.get(0xc0c85614, 0) == 0
    print('ALL BURST ASSERTIONS PASSED', flush=True)


if __name__ == '__main__':
    main()
