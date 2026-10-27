#!/usr/bin/env python3
"""Paper-free probe: run consecutive band passes with a 76-dot raster and read
the head activity counters after each, to see whether a second pass feeds data
to the head (PG2009: band 0 printed, bands 1..20 counted nothing). Fires a few
droplets onto the platen per pass; the carriage sweeps (owner present).

    twopass_probe.py --passes 2 --out app/hybrid/evidence/page/twopass_A
"""
import argparse, json, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, hybrid_call, page_pattern
from band_live import collect_head_counts, verify_band_feed_init

REGS = (0xc0c51400, 0xc0c51404, 0xc0c51500, 0xc0c51d04, 0xc0c51d0c, 0xc0c51118, 0xc0c51100, 0xc0c28000, 0xc0c28200, 0xc0c27814, 0xc0c27810, 0xc0c290a0, 0xc0c29030, 0xc0c29608, 0xc0c87000, 0xc0c812c0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--passes', type=int, default=2); ap.add_argument('--cols', type=int, default=320); ap.add_argument('--rows', type=int, default=304)
    ap.add_argument('--place', type=int, default=5000); ap.add_argument('--no-fresh', action='store_true', help='second and later prepares keep stock RAM')
    ap.add_argument('--no-head', action='store_true'); ap.add_argument('--out', type=Path, required=True); ap.add_argument('--serial')
    ap.add_argument('--colour', action='store_true', help='stock mode 7: black + C/M/Y planes, each a 160-row x 8-column block (1280 dots); counters per lane show how many colour rows each pass fires')
    a = ap.parse_args()
    dev = hybrid_call.device(a.serial)
    record = {'args': vars(a) | {'out': str(a.out)}, 'passes': []}
    log = bytearray()

    def drain():
        for _ in range(64):
            d = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
            if not d: break
            log.extend(d)

    def wait(req, n_words, length, limit=30):
        t0 = time.monotonic()
        while True:
            w = struct.unpack('<%dI' % n_words, bytes(dev.ctrl_transfer(0xc0, req, 0, 0, length, timeout=1000)))
            if w[2] == 0 and w[3] != 1: return w
            if time.monotonic() - t0 > limit: raise TimeoutError(hex(req))
            drain(); time.sleep(0.03)

    def home():
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); w = wait(0x6e, 12, 48); print(f'  home: result {w[3]} raw {w[11]:#x}'); assert w[3] == 2, w

    def cart_read():
        before = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
        dev.ctrl_transfer(0x40, 0x62, 3 | 0x200, 0, b'', timeout=1000)
        t0 = time.monotonic()
        while True:
            s = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
            if s[2] == 2 and s[5] != before[5]: break
            if time.monotonic() - t0 > 30: raise TimeoutError('cartridge')
            drain(); time.sleep(0.05)
        print(f'  head: connection {s[6] & 0xff}/{s[6] >> 8}, prepared {bool(s[7] & 0x10000)}')

    def run(routine, flags=0, index=50):
        status = lambda: struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x98, 0xffff, 0, 64, timeout=5000)))
        before = status()[2]
        dev.ctrl_transfer(0x40, 0x97, routine | flags, index, b'', timeout=2000)
        t0 = time.monotonic()
        while status()[2] == before:
            if time.monotonic() - t0 > 120: raise TimeoutError('routine')
            drain(); time.sleep(0.1)
        st = status(); drain()
        rows = [struct.unpack('<4I', bytes(dev.ctrl_transfer(0xc0, 0x98, i, 0, 16, timeout=2000))) for i in range(min(st[4], 160))]
        print(f'  routine {routine}|{flags:#x}: result {st[1]:#x} errors {st[3]} exceptions {st[11]} ({time.monotonic() - t0:.1f}s) CR {[(r[1], hex(r[2])) for r in rows if r[0] == 1]}')
        return st, rows

    def regs():
        hybrid_call.unlock(dev)
        try: return {hex(r): hex(hybrid_call.read_words(dev, r)[0]) for r in REGS}
        finally: hybrid_call.lock(dev)

    band = np.zeros((a.rows, a.cols), dtype=bool)
    for r in range(0, a.rows, 8): band[r, 100] = True          # 38 even-row dots
    for r in range(4, a.rows, 8): band[r, 200] = True          # 38 odd-row dots
    raster = page_pattern.encode_band(band)
    colour_blocks = {}
    if a.colour:
        from colour_band import GROUPS
        for k, g in GROUPS.items():
            img = np.zeros((a.rows, 2 * a.cols if k != 'K' else a.cols), dtype=bool)
            x = {'K': 100, 'C': 300, 'M': 400, 'Y': 500}[k]; img[0:160, x:x + 8] = True            # 1280 dots per plane
            colour_blocks[g] = img
    print('1. home'); home()
    if not a.no_head: print('2. head power and prepare'); cart_read()
    print('registers before:', regs()); record['before'] = regs()
    print('counters before (cleared):', collect_head_counts(dev))
    for k in range(a.passes):
        print(f'pass {k}:')
        hybrid_call.band_params(dev, a.cols, a.rows, a.place, mode=7 if a.colour else 1, colour=a.colour)
        fresh = 0x400 if (k == 0 or not a.no_fresh) else 0
        st, rows = run(5, fresh)
        verify_band_feed_init(dev)
        if a.colour:
            for g, img in colour_blocks.items():
                m = hybrid_call.manager(dev, g); up = hybrid_call.upload_raster(dev, page_pattern.encode_band(img, m['slots']), verify='sample', group=g); assert up['mismatches'] == 0, up
        else:
            up = hybrid_call.upload_raster(dev, raster, verify=True); assert up['mismatches'] == 0, up
        before = regs()
        st, rows = run(7, 0x100 | 0x1000)
        after = regs(); counts = collect_head_counts(dev)
        lines = [l for l in log.decode(errors='replace').splitlines() if '[shim]' in l or '[cr]' in l]; log.clear()
        print('  counters:', counts); print('  regs after:', after)
        record['passes'].append({'pass': k, 'fresh': bool(fresh), 'result': st[1], 'errors': st[3], 'regs_before_fire': before, 'regs_after': after, 'counters': counts, 'cr': [(r[1], hex(r[2])) for r in rows if r[0] == 1], 'log': lines})
        a.out.parent.mkdir(parents=True, exist_ok=True); a.out.with_suffix('.json').write_text(json.dumps(record, indent=1) + '\n')
    print('3. home'); home()
    print('wrote', a.out.with_suffix('.json'))


if __name__ == '__main__':
    main()
