#!/usr/bin/env python3
"""One sheet: single-group fires under different lane-1 mask words (0xc0c27580/84/88) written through the tunnel.

  mask_lane1.py run --output app/evidence/head_fire/mask_lane1.json
Cases (word0, word1, word2) at 2500-count spacing, group 5, 15 pulses:
  all ones; low 16 of word0; high 16 of word0; word1 only; word2 only (0x1); bits 0..3 of word0;
  bits 0..1 of word0; bit 0 of word0. The scan shows which part of the 1.35 mm group each word
  enables. Masks are restored to all ones afterwards (the hardware default is unknown but non-zero).
"""
import argparse, json, struct, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device  # noqa: E402

CASE_SETS = {
    'first': [('all', (0xffffffff, 0xffffffff, 0xffffffff)), ('w0 low16', (0xffff, 0, 0)), ('w0 high16', (0xffff0000, 0, 0)),
              ('w1 only', (0, 0xffffffff, 0)), ('w2 only', (0, 0, 0xffffffff)), ('w0 bits0-3', (0xf, 0, 0)), ('w0 bits0-1', (3, 0, 0)), ('w0 bit0', (1, 0, 0))],
    # contiguous byte ranges: if the 96 bits map to nozzle sub-ranges, the marks shift within the group
    'ranges': [('all', (0xffffffff, 0xffffffff, 0xffffffff)), ('w0 b0', (0xff, 0, 0)), ('w0 b1', (0xff00, 0, 0)), ('w0 b2', (0xff0000, 0, 0)),
               ('w0 b3', (0xff000000, 0, 0)), ('w1 b0', (0, 0xff, 0)), ('w1 b1', (0, 0xff00, 0)), ('w1 b2', (0, 0xff0000, 0)),
               ('w1 b3', (0, 0xff000000, 0)), ('w2 b0', (0, 0, 0xff)), ('w2 b1', (0, 0, 0xff00)), ('w2 b2', (0, 0, 0xff0000)), ('w2 b3', (0, 0, 0xff000000))],
}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True); r = sub.add_parser('run'); r.add_argument('--output', type=Path); r.add_argument('--x0', type=int, default=36000); r.add_argument('--count', type=int, default=15); r.add_argument('--cases', choices=sorted(CASE_SETS), default='first'); r.add_argument('--pitch', type=int, default=2500)
    args = p.parse_args()
    import usb.util
    dev, info = wait_device('application')
    def wait(req, n, length):
        t0 = time.monotonic()
        while True:
            w = struct.unpack('<%dI' % n, bytes(dev.ctrl_transfer(0xc0, req, 0, 0, length, timeout=1000)))
            if w[2] == 0 and w[3] != 1: return w
            if time.monotonic() - t0 > 90: raise TimeoutError(req)
            time.sleep(0.05)
    def feed(mode, units): dev.ctrl_transfer(0x40, 0x69, mode, units, b'', timeout=1000); w = wait(0x6a, 12, 48); assert w[3] == 2, ('feed', w[3]); return w
    def poke(a, v): dev.ctrl_transfer(0x40, 0x90, a & 0xffff, a >> 16, b'', timeout=1000); dev.ctrl_transfer(0x40, 0x92, v & 0xffff, v >> 16, b'', timeout=1000)
    def cart():
        b = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000))); dev.ctrl_transfer(0x40, 0x62, 3 | 0x200, 0, b'', timeout=1000)
        while struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))[5] == b[5]: time.sleep(0.05)
    def job(x):
        dev.ctrl_transfer(0x40, 0x86, 0, 0, b'', timeout=1000); wa = 0xffffffff & ~(1 << 5)
        for i, xx in enumerate((x, x + 7000)):
            w = wa if i == 0 else 0xffffffff
            dev.ctrl_transfer(0x40, 0x83, w & 0xffff, w >> 16, b'', timeout=1000); dev.ctrl_transfer(0x40, 0x84, 0xffff, 0xffff, b'', timeout=1000); dev.ctrl_transfer(0x40, 0x85, i, xx // 64, b'', timeout=1000)
        dev.ctrl_transfer(0x40, 0x87, (args.count & 0xff) | 0x200, 100, b'', timeout=1000); w = wait(0x88, 12, 48); assert w[3] == 2 and w[5] == 2, w
    plan = {'management': info, 'cases': []}
    try:
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); wait(0x6e, 12, 48); cart()
        feed(1, 125); feed(3, 125)
        dev.ctrl_transfer(0x40, 0x93, 0x7d4e, 0, b'', timeout=1000)
        for k, (name, words) in enumerate(CASE_SETS[args.cases]):
            for j, v in enumerate(words): poke(0xc0c27580 + 4 * j, v)
            x = args.x0 + k * args.pitch; job(x); plan['cases'].append({'case': name, 'x': x, 'words': [hex(v) for v in words]}); print(name, 'at', x, flush=True)
        for j in range(3): poke(0xc0c27580 + 4 * j, 0xffffffff)
        dev.ctrl_transfer(0x40, 0x93, 0, 0, b'', timeout=1000)
        seq = struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))[17]
        dev.ctrl_transfer(0x40, 0x81, 0x100 | 0x400, 40, b'', timeout=1000)
        while struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))[17] == seq: time.sleep(0.05)
        feed(2, 0); dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); wait(0x6e, 12, 48)
    finally:
        if args.output: args.output.write_text(json.dumps(plan, indent=2) + '\n')
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
