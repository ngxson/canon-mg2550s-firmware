#!/usr/bin/env python3
"""Actual ARM completion/cleanup regressions with synthetic scanner input.

The host deliberately leaves a completed RGB page unread until return-home
finishes. Also exercises stop between prepare and start. Motor feedback and
DMA are modelled, so these checks do not establish physical DMA safety.
"""
import argparse
import struct
from pathlib import Path

import validate_escl as v
from validate_colour_continuous import ColourHost, call


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build', type=Path, default=Path(__file__).resolve().parents[1] / 'build')
    args = p.parse_args()
    factory, _, meta = v.factory_and_metadata(args.build)
    meta['build'] = str(args.build)
    syms = meta['symbols']['application']

    def word(e, name):
        return struct.unpack('<I', e.uc.mem_read(int(syms[name], 16), 4))[0]

    def boot():
        e = ColourHost(image=factory, metadata=meta)
        e.run(v.app_mode)
        return e

    e = boot()
    status, location, _ = v.create_job(e, 0, v.settings(
        mode='RGB24', res=300, region=(120, 0, 180, 32)))
    assert status == 201
    line = int(syms['line'], 16)
    e.run(lambda q: bytes(q.uc.mem_read(line, 2)) == b'\0\1',
          resume=True, budget=400_000_000)
    # Scripted motor feedback can reach the endpoint faster than the synthetic
    # IRQ source. In that case queue a further bounded runout through the real
    # motor API, so the sequencer must handle completion during active travel.
    if not call(e, 'scanner_motor_busy'):
        here = call(e, 'scanner_motor_position')
        assert call(e, 'scanner_motor_scan_move', here + 8192, 37000)
        call(e, 'scanner_motor_poll')
    assert call(e, 'scanner_motor_busy'), 'fixture did not exercise active runout'
    call(e, 'scan_page_poll')
    assert word(e, 'page_phase') == 6, 'completed capture waited for motor endpoint'
    assert not call(e, 'scanner_motor_busy')
    assert e.regs.get(0xc0c85614, 0) == 0, 'lamp rail remained on after capture'
    assert e.regs.get(0xc0c57140, 0) == 0
    assert e.regs.get(0xc0d6011c, 0) & 0x11111113 == 0
    print('PASS capture completion stops forward travel and acquisition', flush=True)

    e.run(lambda q: word(q, 'page_phase') == 7, resume=True, budget=400_000_000)
    assert e.drive is None and e.regs.get(0xc0c85614, 0) == 0
    assert call(e, 'scanner_line_ready') == 32 + 156, 'return-home discarded unread rows'   # 156 run-in lines at 300 dpi precede the region
    status, _, body, _ = v.http(e, 0, 'GET',
        '/eSCL/' + location.split('/eSCL/', 1)[1] + '/NextDocument')
    width, height, ctype, rows, _, depth = v.decode_png(body)
    assert status == 200 and (width, height, ctype, depth) == (180, 32, 2, 8)
    assert len(rows) == 32 and all(any(row) for row in rows)
    print('PASS all RGB rows remain downloadable after return-home', flush=True)

    # Prepare starts timing before scanner_line_start sets line.running.
    e = boot()
    assert call(e, 'scanner_line_prepare', 3, 32, 128, 108360, budget=30_000_000)
    assert e.regs.get(0xc0c85614, 0) == 1
    # The hardware stop command must not depend on valid page bookkeeping;
    # the physical long-page failure overwrote application globals.
    e.uc.mem_write(int(syms['page_phase'], 16), b'\xff')
    call(e, 'scan_page_cancel')
    assert e.regs.get(0xc0c85614, 0) == 0
    assert e.regs.get(0xc0d6011c, 0) & 0x11111113 == 0
    assert e.regs.get(0xc0c57140, 0) == 0
    for ch in range(4):
        assert e.regs.get(0xc0d50000 + 0x100 * ch, 0) == 0
    assert not e.erases and not e.programs
    print('PASS cancel quiesces prepared acquisition even with invalid page state', flush=True)
    print('ALL SCAN FINISH ASSERTIONS PASSED', flush=True)


if __name__ == '__main__':
    main()
