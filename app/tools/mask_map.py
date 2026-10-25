#!/usr/bin/env python3
"""One-sheet mapping of the head work-engine record words to nozzle groups.

  mask_map.py run  --output app/evidence/head_fire/mask_map.json     # print the sheet (one run, one sheet)
  mask_map.py decode scans/mask_map.json --plan app/evidence/head_fire/mask_map.json   # read the scan back

Hypothesis under test (brief 8 numbers): the two 32-bit record words written to
0xc0c291e0 / 0xc0c291e8 select black nozzles eight per bit, cleared bit = fire
(record 1's ten cleared bits gave a 3.4 mm = 80-nozzle mark). The sheet has
three rows of 32 entries, 5 mm apart across the sheet, one bit per entry, each
entry one black record of `--count` pulses (stock black waveform, lanes 0/1)
fired by the firmware's crawl job as the carriage passes the entry (no stops):

  row 0: word A = all ones except bit i cleared, word B = all ones
  row 1: word A = only bit i set,               word B = all ones
  row 2: word A = all ones, word B = all ones except bit i cleared

Rows are 15 mm apart along the paper (1050 LF counts) so the sheet stays in
the feed nip (the placement leaves about 48 mm of margin). Under either
polarity every row is informative: a cleared-bit row shows one small mark per
group, a set-bit row shows the complementary column with a gap. Carriage
scale 2.6 um per count; stops start at --x0 counts (paper edge is near 29000).
`decode` groups the scan's marks by stop and prints, per bit, the mark
positions along the paper axis relative to the row's reference.
"""
import argparse, json, struct, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device  # noqa: E402

STOP_COUNTS = 1920         # 5 mm at 2.6 um per count
ROW_COUNTS = 1050          # 15 mm at 70 counts per mm
ALL = 0xffffffff


def rows(bit):
    return [(ALL & ~(1 << bit), ALL), (1 << bit, ALL), (ALL, ALL & ~(1 << bit))]


def run(args):
    import usb.util
    dev, info = wait_device('application')
    log = bytearray(); plan = {'management': info, 'count': args.count, 'ambient': args.ambient, 'x0': args.x0,
                               'stop_counts': STOP_COUNTS, 'row_counts': ROW_COUNTS, 'stops': []}

    def drain():
        for _ in range(16):
            d = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
            if not d: break
            log.extend(d)

    def wait(req_status, n_words, length, idx_state=2, idx_result=3):
        t0 = time.monotonic()
        while True:
            w = struct.unpack('<%dI' % n_words, bytes(dev.ctrl_transfer(0xc0, req_status, 0, 0, length, timeout=1000)))
            if w[idx_state] == 0 and w[idx_result] != 1: return w
            if time.monotonic() - t0 > 30: raise TimeoutError(req_status)
            drain(); time.sleep(0.05)

    def feed(mode, units):
        dev.ctrl_transfer(0x40, 0x69, mode, units, b'', timeout=1000); w = wait(0x6a, 12, 48); assert w[3] == 2, ('feed', w)

    def cr_move(x):
        for attempt in range(3):                       # a short move can stall; back off 8 mm and retry
            dev.ctrl_transfer(0x40, 0x6d, x // 64, 0, b'', timeout=1000); w = wait(0x6e, 12, 48)
            if w[3] == 2: return w
            print('  carriage result', w[3], 'at attempt', attempt, '; retrying', flush=True)
            dev.ctrl_transfer(0x40, 0x6d, max(0, x - 3000) // 64, 0, b'', timeout=1000); wait(0x6e, 12, 48)
        raise RuntimeError(('carriage move failed', x, w))

    def cart_read():
        before = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
        dev.ctrl_transfer(0x40, 0x62, 3 | 0x200, 0, b'', timeout=1000)
        t0 = time.monotonic()
        while True:
            s = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
            if s[2] == 2 and s[5] != before[5]: return s
            if time.monotonic() - t0 > 15: raise TimeoutError('cartridge')
            drain(); time.sleep(0.05)

    def fire(word_a, word_b):
        dev.ctrl_transfer(0x40, 0x83, word_a & 0xffff, word_a >> 16, b'', timeout=1000)
        dev.ctrl_transfer(0x40, 0x84, word_b & 0xffff, word_b >> 16, b'', timeout=1000)
        seq = struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))[17]
        dev.ctrl_transfer(0x40, 0x81, 0x2000 | 0x200 | (args.count & 0xff), args.ambient, b'', timeout=1000)
        t0 = time.monotonic()
        while True:
            w = struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))
            if w[2] == 2 and w[17] != seq: assert w[3] == 2, ('fire', w); return w
            if time.monotonic() - t0 > 10: raise TimeoutError('fire')
            time.sleep(0.02)

    def stamp_row(row, dry=False):
        dev.ctrl_transfer(0x40, 0x86, 0, 0, b'', timeout=1000)                       # clear entries
        for bit in range(32):
            wa, wb = rows(bit)[row]; x = args.x0 + bit * STOP_COUNTS
            dev.ctrl_transfer(0x40, 0x83, wa & 0xffff, wa >> 16, b'', timeout=1000)
            dev.ctrl_transfer(0x40, 0x84, wb & 0xffff, wb >> 16, b'', timeout=1000)
            dev.ctrl_transfer(0x40, 0x85, bit, x // 64, b'', timeout=1000)
            plan['stops'].append({'row': row, 'bit': bit, 'x': x, 'word_a': f'{wa:08x}', 'word_b': f'{wb:08x}'})
        dev.ctrl_transfer(0x40, 0x87, (args.count & 0xff) | (0x100 if dry else 0) | 0x200, args.crawl // 100, b'', timeout=1000)
        w = wait(0x88, 12, 48)
        print(f'row {row}: result {w[3]} fired {w[5]} of {w[4]} missed {w[6]} in {w[8]} ms (crawl {w[10]})', flush=True)
        plan.setdefault('jobs', []).append({'row': row, 'result': w[3], 'entries': w[4], 'fired': w[5], 'missed': w[6], 'ms': w[8]})
        return w

    def stamp_interleaved(dry=False):
        """One pass, no feed: for bit i, word A with only bit i cleared at x0 + 1920*i, then word B
        with only bit i cleared 960 counts further. 64 entries; each small mark's vertical position
        is the group the bit selects (hypothesis: cleared bit fires)."""
        dev.ctrl_transfer(0x40, 0x86, 0, 0, b'', timeout=1000)
        index = 0
        for bit in range(32):
            for which, (wa, wb) in (('A', (ALL & ~(1 << bit), ALL)), ('B', (ALL, ALL & ~(1 << bit)))):
                x = args.x0 + bit * STOP_COUNTS + (0 if which == 'A' else STOP_COUNTS // 2)
                dev.ctrl_transfer(0x40, 0x83, wa & 0xffff, wa >> 16, b'', timeout=1000)
                dev.ctrl_transfer(0x40, 0x84, wb & 0xffff, wb >> 16, b'', timeout=1000)
                dev.ctrl_transfer(0x40, 0x85, index, x // 64, b'', timeout=1000); index += 1
                plan['stops'].append({'row': which, 'bit': bit, 'x': x, 'word_a': f'{wa:08x}', 'word_b': f'{wb:08x}'})
        dev.ctrl_transfer(0x40, 0x87, (args.count & 0xff) | (0x100 if dry else 0) | 0x200, args.crawl // 100, b'', timeout=1000)
        w = wait(0x88, 12, 48)
        print(f'interleaved pass: result {w[3]} fired {w[5]} of {w[4]} missed {w[6]} in {w[8]} ms', flush=True)
        plan.setdefault('jobs', []).append({'row': 'AB', 'result': w[3], 'entries': w[4], 'fired': w[5], 'missed': w[6], 'ms': w[8]})
        return w

    try:
        drain(); cart_read()
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); wait(0x6e, 12, 48)          # home
        if args.dry_run:
            stamp_interleaved(dry=True) if args.interleave else stamp_row(0, dry=True)
        elif args.interleave:
            feed(1, 8000 // 64); feed(3, 8000 // 64)
            w = stamp_interleaved(); assert w[3] == 2, ('stamp job failed', w); drain()
        else:
            feed(1, 8000 // 64); feed(3, 8000 // 64)                                           # placement used before
            for row in range(3):
                if row: feed(3, ROW_COUNTS // 64)
                w = stamp_row(row)
                assert w[3] == 2, ('stamp job failed', w)
                drain()
        # release the head interface the jobs kept prepared
        seq = struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))[17]
        dev.ctrl_transfer(0x40, 0x81, 0x100 | 0x400, args.ambient, b'', timeout=1000)
        t0 = time.monotonic()
        while struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))[17] == seq:
            if time.monotonic() - t0 > 10: raise TimeoutError('release'); time.sleep(0.05)
        if not args.dry_run: feed(2, 0)                                                        # eject
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); wait(0x6e, 12, 48)          # park
    finally:
        try: drain()
        except Exception: pass
        plan['firmware_log'] = log.decode(errors='replace')
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(plan, indent=2) + '\n'); print('saved', args.output)
        usb.util.dispose_resources(dev)


def decode(args):
    scan = json.loads(Path(args.scan).read_text()); plan = json.loads(args.plan.read_text())
    marks = scan['marks']
    # Stops are 2.5 mm apart across the sheet; scan x falls as counts rise. Fit x_mm = a - b*counts from the
    # two extreme populated stops is not possible without knowing which marks belong where, so cluster by x.
    xs = sorted(set(round(m['x_mm'] / 2.5) * 2.5 for m in marks))
    print('mark columns (mm):', xs)
    by_col = {}
    for m in marks:
        by_col.setdefault(round(m['x_mm'] / 2.5) * 2.5, []).append(m)
    for col in sorted(by_col):
        ms = sorted(by_col[col], key=lambda m: m['y_mm'])
        print(f"x={col:6.2f} mm: " + ', '.join(f"y={m['y_mm']:.1f} h={m['h_mm']:.1f}" for m in ms))
    print('Map stop index = (x_mm of the rightmost populated column - x_mm) / 5 to bit numbers; rows by y.')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run'); r.add_argument('--count', type=int, default=100); r.add_argument('--ambient', type=int, default=40)
    r.add_argument('--x0', type=int, default=35000); r.add_argument('--output', type=Path)
    r.add_argument('--crawl', type=int, default=10000, help='crawl speed counts/s'); r.add_argument('--interleave', action='store_true', help='one pass, no feed: A-cleared and B-cleared entries side by side per bit'); r.add_argument('--dry-run', action='store_true', help='one row through the stamp job without arming the head, no paper')
    d = sub.add_parser('decode'); d.add_argument('scan'); d.add_argument('--plan', type=Path, required=True)
    args = p.parse_args()
    run(args) if args.cmd == 'run' else decode(args)


if __name__ == '__main__':
    main()
