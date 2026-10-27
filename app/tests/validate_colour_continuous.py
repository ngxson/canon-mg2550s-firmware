#!/usr/bin/env python3
"""ARM regressions for continuous RGB output, with synthetic optics.

Checks 75/150/300 dpi, the n=1 pixel average, reuse of the colour ring, and
the stock C=3 routing preconditions before the model supplies any pixels.
DMA traversal follows ScanHost's model; this does not verify physical colour
order, cold AFE startup, IRQ cadence or optical quality. No printer access.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path

import validate_escl as v
from validate_escl_scan import ScanHost, expected_row, fetch_while_polling, document_lines
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3, UC_ARM_REG_SP, UC_ARM_REG_LR,
                               UC_ARM_REG_PC)


class ColourHost(ScanHost):
    def _scan_dma(self, cpu):
        if self.regs.get(0xc0d6000c) == 7:
            # Stock finite C=3 fixture values, independent of output pixels.
            fields = ((0xe0, 0xffffffff, 7), (0x130, 31, 2),
                      (0x1a8, 7, 2), (0x1b8, 7, 2),
                      (0x1c0, 0xffffffff, 0x24924924),
                      (0x1c4, 0xffffffff, 0x49249249),
                      (0x1c8, 0xffffffff, 0x92492492))
            for offset, mask, expected in fields:
                value = self.regs.get(0xc0d60000 + offset, 0) & mask
                assert value == expected, ('C=3 setup', hex(offset), hex(value), hex(expected))
            if self.regs.get(0xc0d6011c, 0) & 0x10000:
                assert self.regs.get(0xc0d60164, 0) & 255 == 2
        super()._scan_dma(cpu)


def call(e, name, *args, budget=3_000_000):
    """Call compiled firmware without substituting any of its functions."""
    e.predicate = lambda _: False
    saved = e.uc.context_save()
    try:
        e.uc.reg_write(UC_ARM_REG_SP, 0x207fd000)
        e.uc.reg_write(UC_ARM_REG_LR, 0x207fe000)
        for reg, arg in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            e.uc.reg_write(reg, arg)
        e.uc.emu_start(int(e.meta['symbols']['application'][name], 16), 0x207fe000, count=budget)
        if e.error:
            raise e.error
        assert e.uc.reg_read(UC_ARM_REG_PC) == 0x207fe000, name
        return e.uc.reg_read(UC_ARM_REG_R0)
    finally:
        e.uc.context_restore(saved)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, default=Path(__file__).resolve().parents[1] / 'build')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--long', action='store_true', help='add a full-width 75 dpi page of 700 lines (board froze near line 500)')
    args = parser.parse_args()
    build = args.build.resolve()
    factory, app, meta = v.factory_and_metadata(build)
    meta['build'] = str(build)
    e = ColourHost(image=factory, metadata=meta)
    e.run(v.app_mode)
    symbols = meta['symbols']['application']
    report = {'scope': __doc__, 'app_sha256': hashlib.sha256(app).hexdigest(), 'cases': []}

    def record(name, **details):
        report['cases'].append({'name': name, **details})
        print('PASS', name, json.dumps(details), flush=True)

    def phase(q):
        return struct.unpack('<I', q.uc.mem_read(int(symbols['page_phase'], 16), 4))[0]

    # Admission only: no simulated full-platen travel is claimed here.
    for lines in (32, 3507, 3508):
        call(e, 'scan_page_set_components', 3)
        assert call(e, 'scan_page_start', lines, 128, 0, 1), ('300 dpi rejected', lines)
        call(e, 'scan_page_cancel')
    assert not call(e, 'scan_page_start', 32, 128, 0, 0), 'vendor limits unexpectedly broadened'
    assert not call(e, 'scan_page_start', 3680, 128, 4096, 1), 'travel bound lost'
    record('300dpi_admission_and_bounds', rows=[32, 3507, 3508])

    capacity = call(e, 'scanner_line_capacity', 3)
    # Include a complete 75 dpi page: shorter tests missed the physical hang
    # around row 555. Synthetic DMA cannot establish its hardware cause.
    for res, count in ((300, 16), (75, 24), (150, 24), (300, 32), (300, capacity + 12), (75, 876)):
        # The short first region crosses both sides of the measured reference
        # notch. It catches calibration averaging that brightens central columns.
        x, y, width = (0, 0, 2480) if count == 876 else (1100, 0, 400) if count == 16 else (120, 0, 180)
        e.reference_notch_probe = count == 16
        step = 300 // res
        pixels, height = width // step, count * step
        before = len(e.lines)
        status, location, _ = v.create_job(e, 0, v.settings(
            mode='RGB24', res=res, region=(x, y, width, height)))
        assert status == 201, ('RGB job rejected', res, status)
        status, _, body, _ = fetch_while_polling(e, 0, '/eSCL/' + location.split('/eSCL/', 1)[1] + '/NextDocument')
        assert status == 200, (res, status)
        w, h, ctype, rows, _, depth = v.decode_png(body)
        assert (w, h, ctype, depth) == (pixels, count, 2, 8)
        optical, _ = document_lines(e, before, y, count)
        for j, line in enumerate(optical):
            planes = [expected_row([max(raw - 2000 * k, 0) for raw in line],
                                  x, step, pixels, plane=k) for k in range(3)]
            expected = bytes(b for i in range(pixels) for b in (planes[0][i], planes[1][i], planes[2][i]))
            if rows[j] != expected:
                log = b''
                for _ in range(80):
                    e.requests.appendleft(('log', v.setup(0xc0, 0x50, 0, 0, 256))); before_n = len(e.responses)
                    e.run(lambda q: len(q.responses) > before_n, resume=True)
                    part = e.responses[-1][2]
                    if not part: break
                    log += bytes(part)
                print('FIRMWARE LOG:', [l for l in log.decode('latin1').splitlines() if '[cal]' in l or '[line] start' in l][-8:])
                at = next(k for k, (got, want) in enumerate(zip(rows[j], expected)) if got != want)
                raise AssertionError(('RGB samples differ', res, j, 'input x', x + at // 3 * step,
                                      'byte', at, 'got', list(rows[j][at:at + 12]),
                                      'expected', list(expected[at:at + 12])))
        e.run(lambda q: phase(q) in (7, 8, 9), resume=True, budget=400_000_000)
        assert phase(e) == 7 and e.drive is None
        assert e.regs.get(0xc0c85614, 0) == 0
        assert not e.erases and not e.programs
        record('continuous_rgb', dpi=res, pixels=pixels, rows=count,
               ring_capacity=capacity, reuses_ring=count > capacity)

    # Negative control: prove the model refuses C=1 routing for mask 7.
    e.regs[0xc0d6000c] = 7
    e.regs[0xc0d601c0] = 1
    try:
        e._scan_dma(e.uc)
    except AssertionError as exc:
        assert exc.args[0][0] == 'C=3 setup', exc
    else:
        raise AssertionError('model accepted incorrect colour routing')
    record('incorrect_colour_routing_rejected')
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print('ALL CONTINUOUS COLOUR ASSERTIONS PASSED', flush=True)


if __name__ == '__main__':
    main()
