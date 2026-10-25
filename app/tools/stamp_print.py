#!/usr/bin/env python3
"""Print text by stationary stamping with the stock black waveform (host-driven).

  stamp_print.py "HELLO WORLD" --x0 40000 --count 15 --output app/evidence/head_fire/hello.json

For each pixel column of the text (5x7 font, one pixel = one nozzle group
high and the same width) an entry (carriage position, two record words
selecting the column's groups: tools/stamp_plan.py with the nozzle map
measured by mask_map.py) is uploaded; the firmware's stamp job then crawls
the carriage across the text and fires one black record of --count pulses
as each entry's position is crossed (emulated in tests/validate_stamp.py). The sheet is picked up and placed as before (hold 8000
plus an advance of 8000 counts), ejected at the end, carriage parked.

Physically fires the black head. Do not run without the owner present; the
short-move mode of the carriage was emulated but not yet run on the printer.
"""
import argparse, json, struct, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools')); sys.path.insert(0, str(APP / 'tools'))
from fwctl import wait_device  # noqa: E402
from stamp_plan import NozzleMap, text_bitmap, plan, COUNTS_PER_COLUMN  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('text'); p.add_argument('--x0', type=int, default=100000, help='carriage counts of the first (leftmost) column; later columns go toward the park side')
    p.add_argument('--count', type=int, default=15, help='pulses per stop, 15..100')
    p.add_argument('--ambient', type=int, default=40); p.add_argument('--scale', type=int, default=1, help='pixel size in nozzle groups')
    p.add_argument('--crawl', type=int, default=10000, help='crawl speed in counts/s during the pass')
    p.add_argument('--map', type=Path, default=APP / 'evidence/head_fire/nozzle_map.json', help='JSON with NozzleMap fields (default: the measured map)')
    p.add_argument('--no-feed', action='store_true', help='sheet already placed; do not pick up or eject')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    nmap = NozzleMap(**{k: v for k, v in json.loads(args.map.read_text()).items() if not k.startswith('_')}) if args.map else NozzleMap()
    cols = text_bitmap(args.text); stops = plan(cols, nmap, args.x0, pixel_groups=args.scale)
    print(len(cols), 'columns,', len(stops), 'stops, span', stops[-1].x - stops[0].x, 'counts')
    import usb.util
    dev, info = wait_device('application')
    log = bytearray(); report = {'management': info, 'text': args.text, 'x0': args.x0, 'count': args.count, 'nozzle_map': vars(nmap), 'stops': []}
    s32 = lambda v: v - (1 << 32) if v >= 1 << 31 else v

    def drain():
        for _ in range(16):
            d = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
            if not d: break
            log.extend(d)

    def wait(req, n_words, length):
        t0 = time.monotonic()
        while True:
            w = struct.unpack('<%dI' % n_words, bytes(dev.ctrl_transfer(0xc0, req, 0, 0, length, timeout=1000)))
            if w[2] == 0 and w[3] != 1: return w
            if time.monotonic() - t0 > 30: raise TimeoutError(req)
            drain(); time.sleep(0.03)

    def feed(mode, units):
        dev.ctrl_transfer(0x40, 0x69, mode, units, b'', timeout=1000); w = wait(0x6a, 12, 48); assert w[3] == 2, ('feed', w)

    def cr_move(x):
        for attempt in range(3):
            dev.ctrl_transfer(0x40, 0x6d, x // 64, 0, b'', timeout=1000); w = wait(0x6e, 12, 48)
            if w[3] == 2: return s32(w[4])
            print('  carriage result', w[3], 'retrying', flush=True)
            dev.ctrl_transfer(0x40, 0x6d, max(0, x - 3000) // 64, 0, b'', timeout=1000); wait(0x6e, 12, 48)
        raise RuntimeError(('carriage move failed', x, w))

    def cart_read():
        before = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
        dev.ctrl_transfer(0x40, 0x62, 3 | 0x200, 0, b'', timeout=1000)
        while True:
            s = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
            if s[2] == 2 and s[5] != before[5]: return s
            drain(); time.sleep(0.05)

    def fire(word_a, word_b, keep=True):
        dev.ctrl_transfer(0x40, 0x83, word_a & 0xffff, word_a >> 16, b'', timeout=1000)
        dev.ctrl_transfer(0x40, 0x84, word_b & 0xffff, word_b >> 16, b'', timeout=1000)
        seq = struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))[17]
        dev.ctrl_transfer(0x40, 0x81, 0x2000 | (0x200 if keep else 0) | (args.count & 0xff), args.ambient, b'', timeout=1000)
        t0 = time.monotonic()
        while True:
            w = struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))
            if w[2] == 2 and w[17] != seq: assert w[3] == 2, ('fire', w); return w
            if time.monotonic() - t0 > 10: raise TimeoutError('fire')
            time.sleep(0.02)

    try:
        drain(); cart_read()
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); wait(0x6e, 12, 48)
        if not args.no_feed: feed(1, 8000 // 64); feed(3, 8000 // 64)
        # Upload the entries, then run the crawl job: the firmware fires each entry when the
        # encoder crosses its position (src/head_fire.c stamp job), smear = crawl speed x 2.4 ms.
        dev.ctrl_transfer(0x40, 0x86, 0, 0, b'', timeout=1000)
        for i, st in enumerate(stops):
            dev.ctrl_transfer(0x40, 0x83, st.word_a & 0xffff, st.word_a >> 16, b'', timeout=1000)
            dev.ctrl_transfer(0x40, 0x84, st.word_b & 0xffff, st.word_b >> 16, b'', timeout=1000)
            dev.ctrl_transfer(0x40, 0x85, i, st.x // 64, b'', timeout=1000)
        t0 = time.monotonic()
        dev.ctrl_transfer(0x40, 0x87, (args.count & 0xff) | 0x200, args.crawl // 100, b'', timeout=1000)
        w = wait(0x88, 12, 48)
        report['job'] = {'result': w[3], 'entries': w[4], 'fired': w[5], 'missed': w[6], 'ms': w[8], 'crawl': w[10]}
        print('job result', w[3], 'fired', w[5], 'of', w[4], 'missed', w[6], 'in', w[8], 'ms', flush=True)
        assert w[3] == 2, ('stamp job failed', w)
        seq = struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))[17]
        dev.ctrl_transfer(0x40, 0x81, 0x100 | 0x400, args.ambient, b'', timeout=1000)
        t0 = time.monotonic()
        while struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))[17] == seq:
            if time.monotonic() - t0 > 10: raise TimeoutError('release'); time.sleep(0.05)   # shut the interface down
        if not args.no_feed: feed(2, 0)
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); wait(0x6e, 12, 48)
    finally:
        try: drain()
        except Exception: pass
        report['firmware_log'] = log.decode(errors='replace')
        if args.output: args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(report, indent=2) + '\n'); print('saved', args.output)
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
