#!/usr/bin/env python3
"""One sheet, two measurements: pattern words inside a nozzle group, and paper feed per advance.

  fine_test.py run --output app/evidence/head_fire/fine_test.json
  fine_test.py decode scans/fine_test.json --plan app/evidence/head_fire/fine_test.json

Series 1 (no feed): for each pattern-word pair below, one crawl job with a
single entry that fires nozzle group 5 only (word A = all ones except bit 5),
15 pulses, at a distinct carriage position 2000 counts apart. The scan at
150 dpi shows how the mark's height and position inside the 1.35 mm group
depend on the pattern words. Pair 0 is stock.

Series 2 (feed ruler): a reference entry firing groups 0 and 9 (two dots
13 mm apart) at x = 60000; then repeatedly: advance the paper by --step
counts (advance-only feed, sensor ignored) and fire the same reference
1500 counts further across. Twenty steps. The scan gives the paper
displacement of each advance from the dot positions; steps after the sheet
leaves the feed nip show as short or missing displacements.

Placement with the firmware default (stock-like handoff). The head interface stays
prepared between jobs and is shut down at the end. Physically fires the
black head.
"""
import argparse, json, struct, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device  # noqa: E402

ALL = 0xffffffff
PATTERNS = [(0x03000300, 0x00030003), (0x01000100, 0x00010001), (0x02000200, 0x00020002),
            (0x00030003, 0x03000300), (0x0f000f00, 0x000f000f), (0x00000000, 0x00000000), (0x03030303, 0x03030303)]
GROUP = 5
REF_WORD = ALL & ~(1 << 0) & ~(1 << 9)


def run(args):
    import usb.util
    dev, info = wait_device('application')
    log = bytearray(); plan = {'management': info, 'patterns': [[hex(a), hex(b)] for a, b in PATTERNS], 'group': GROUP,
                               'series1': [], 'series2': [], 'step': args.step}

    def drain():
        for _ in range(16):
            d = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
            if not d: break
            log.extend(d)

    def wait(req, n, length):
        t0 = time.monotonic()
        while True:
            w = struct.unpack('<%dI' % n, bytes(dev.ctrl_transfer(0xc0, req, 0, 0, length, timeout=1000)))
            if w[2] == 0 and w[3] != 1: return w
            if time.monotonic() - t0 > 60: raise TimeoutError(req)
            drain(); time.sleep(0.05)

    def feed(mode, units):
        dev.ctrl_transfer(0x40, 0x69, mode, units, b'', timeout=1000); w = wait(0x6a, 12, 48); assert w[3] == 2, ('feed', w); return w

    def cart_read():
        before = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
        dev.ctrl_transfer(0x40, 0x62, 3 | 0x200, 0, b'', timeout=1000)
        while True:
            s = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
            if s[2] == 2 and s[5] != before[5]: return s
            drain(); time.sleep(0.05)

    def job(entries, count):
        # A far blank entry (no group cleared: nothing fires) keeps every crawl longer than the
        # carriage controller's short-move threshold (6000 counts), which stalls on hardware.
        entries = list(entries) + [(entries[-1][0] + 7000, ALL, ALL)]
        dev.ctrl_transfer(0x40, 0x86, 0, 0, b'', timeout=1000)
        for i, (x, wa, wb) in enumerate(entries):
            dev.ctrl_transfer(0x40, 0x83, wa & 0xffff, wa >> 16, b'', timeout=1000)
            dev.ctrl_transfer(0x40, 0x84, wb & 0xffff, wb >> 16, b'', timeout=1000)
            dev.ctrl_transfer(0x40, 0x85, i, x // 64, b'', timeout=1000)
        dev.ctrl_transfer(0x40, 0x87, (count & 0xff) | 0x200, 100, b'', timeout=1000)
        w = wait(0x88, 12, 48); assert w[3] == 2 and w[5] == len(entries), ('job', w); return w

    def set_pattern(pa, pb):
        dev.ctrl_transfer(0x40, 0x89, pa & 0xffff, pa >> 16, b'', timeout=1000)
        dev.ctrl_transfer(0x40, 0x8a, pb & 0xffff, pb >> 16, b'', timeout=1000)

    try:
        drain(); cart_read()
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); wait(0x6e, 12, 48)
        if not args.resume: feed(1, 0)   # firmware default placement
        for k, (pa, pb) in enumerate([] if args.resume else PATTERNS):   # series 1
            set_pattern(pa, pb); x = 36000 + k * 2000
            w = job([(x, ALL & ~(1 << GROUP), ALL)], args.count)
            plan['series1'].append({'pattern': k, 'x': x, 'fired': w[5], 'ms': w[8]}); print('pattern', k, hex(pa), hex(pb), 'at', x, 'fired', w[5], flush=True)
        set_pattern(0, 0)
        x = 60000; w = job([(x, REF_WORD, ALL)], args.count); plan['series2'].append({'step': 0, 'x': x, 'advance': 0})
        for k in range(1, args.steps + 1):                              # series 2
            f = feed(3, args.step // 64); x = 60000 + k * 1500
            w = job([(x, REF_WORD, ALL)], args.count)
            plan['series2'].append({'step': k, 'x': x, 'advance': f[6], 'position': f[9]}); print('step', k, 'advance', f[6], 'x', x, flush=True)
            drain()
        seq = struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))[17]
        dev.ctrl_transfer(0x40, 0x81, 0x100 | 0x400, 40, b'', timeout=1000)
        while struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))[17] == seq: time.sleep(0.05)
        feed(2, 0)
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); wait(0x6e, 12, 48)
    finally:
        try: drain()
        except Exception: pass
        plan['firmware_log'] = log.decode(errors='replace')
        if args.output: args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(plan, indent=2) + '\n'); print('saved', args.output)
        usb.util.dispose_resources(dev)


def decode(args):
    scan = json.loads(Path(args.scan).read_text()); plan = json.loads(args.plan.read_text())
    marks = scan['marks']; mm_per_count = 2.61e-3
    # Series 1 marks lie at x = x_ref - (x_counts - x0) * scale; locate by clustering columns 5.2 mm apart.
    cols = {}
    for m in marks: cols.setdefault(round(m['x_mm'] / 1.0), []).append(m)
    print('columns (x mm): marks (y, h)')
    for c in sorted(cols): print(f"  x~{c:4d}: " + ', '.join(f"y={m['y_mm']:.2f} h={m['h_mm']:.2f} dark={m['mean_darkness']:.2f}" for m in sorted(cols[c], key=lambda m: m['y_mm'])))
    print('series 1 entries (counts -> expected ~5.2 mm apart, leftmost = pattern 6):', [s['x'] for s in plan['series1']])
    print('series 2 entries (counts, 3.9 mm apart; dot pairs 13 mm apart; y shift per step = paper advance):', [(s['step'], s['x']) for s in plan['series2']])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run'); r.add_argument('--count', type=int, default=15); r.add_argument('--step', type=int, default=1024)
    r.add_argument('--steps', type=int, default=20); r.add_argument('--output', type=Path)
    r.add_argument('--resume', action='store_true', help='sheet already placed and series 1 done: run series 2 only, no placement feed')
    d = sub.add_parser('decode'); d.add_argument('scan'); d.add_argument('--plan', type=Path, required=True)
    args = p.parse_args()
    run(args) if args.cmd == 'run' else decode(args)


if __name__ == '__main__':
    main()
