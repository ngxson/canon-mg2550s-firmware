#!/usr/bin/env python3
"""Exercise the real motor poll across the 44.7 s tick-duration boundary.

Seed a running, frozen-feedback jog near that boundary with an extended test
deadline. This avoids emulating 45 seconds just to reach the arithmetic edge;
it does not claim physical motion or change production jog limits.
"""
import argparse
import re
import struct
from pathlib import Path

import validate_escl as v
from validate_home import HomeModel, C, TICKS_PER_MS


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build', type=Path, required=True)
    args = p.parse_args()
    factory, _, meta = v.factory_and_metadata(args.build)
    syms = {k: int(n, 16) for k, n in meta['symbols']['application'].items()}
    listing = (args.build / 'application.map').read_text()

    def motor_address(name):
        # Local 'started' also occurs in page_job.c in the old build. Select
        # this compilation unit explicitly for the negative regression.
        match = re.search(r'\.bss\.' + name + r'\s+(0x[0-9a-f]+)\s+0x4\s+\S*app_src_scanner_motor.c.o', listing)
        assert match, name
        return int(match[1], 16)

    def fixture():
        e = HomeModel(image=factory, metadata=meta)
        e.behavior = 'frozen'
        e.run(v.app_mode)
        e.requests.appendleft(('jog', v.setup(0x40, 0x73)))
        e.run(lambda q: q.drive is not None, resume=True)

        def put(address, value):
            e.uc.mem_write(address, struct.pack('<I', value))

        ms = 44738
        ticks = ms * TICKS_PER_MS
        e.counter = ticks
        for name, value in [('counter_sample', ticks & 0x1fffffff),
                            ('accumulated_ticks', ticks), ('previous_ticks', ticks),
                            ('milliseconds', ms), ('fractional_ticks', 0)]:
            put(syms[name], value)
        for name, value in [('last_ms', ms), ('limit_ms', 90000)]:
            put(motor_address(name), value)
        if 'move_ms' in syms:
            put(motor_address('move_ms'), ms)
            put(motor_address('tick_anchor'), ticks)
        else:
            put(motor_address('started'), 0)
        return e

    e = fixture()
    before = e.counter
    e.run(lambda q: ((q.counter - before) & 0xffffffff) >= 8 * TICKS_PER_MS,
          resume=True)
    assert e.drive is not None, 'tick-duration wrap falsely triggered the late-tick guard'
    last = struct.unpack('<I', e.uc.mem_read(motor_address('last_ms'), 4))[0]
    assert last >= 44745, last
    print('PASS motor elapsed milliseconds remain monotonic past 2^32 ticks', flush=True)

    # A real missed control interval must still stop the drive immediately.
    e.clock_jump = True
    e.run(lambda q: q.drive is None, resume=True)
    assert e.regs[C] == 0 and e.regs[C + 8] == 2000
    assert not e.erases and not e.programs
    print('PASS actual delayed control tick still stops the motor after rollover', flush=True)


if __name__ == '__main__':
    main()
