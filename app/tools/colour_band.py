#!/usr/bin/env python3
"""One colour band on paper through the hybrid shim (owner present): stock print
mode 7 (black + C/M/Y), one sweep, no paper advance. Each plane carries a
distinct pattern at known raster rows so an RGB scan measures, per colour,
the vertical extent of the array, its offset from the black array and the
column/row order. Black: label + one-row lines every 8th row (cols 0..299) +
seam marks. Cyan: vertical bars. Magenta: diagonal. Yellow: 8-row ladder.
Every plane also prints its name in text at a different column.

    colour_band.py --label CB2140 [--cols 1024] [--no-feed] [--no-eject]
"""
import argparse, json, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, hybrid_call, page_pattern
from band_live import verify_band_feed_init
from page_live import place_for_x

GROUPS = {'K': 6, 'C': 0, 'M': 2, 'Y': 4}


COLOUR_ROWS = 176      # the builder gives the C/M/Y lanes 22 eight-row groups (emulator, mode 7)
PX_MM = 600 / 25.4


def planes(label, cols, rows):
    """All planes at `cols` columns (600 dpi; CR2225 showed colour columns share the black
    column pitch and index; mode 7's double-width colour buffers are capacity only).

    Readable without a colour scanner: black prints a vertical mm ruler (ticks
    every 1 mm, long every 5 mm, numbers every 5 mm, from raster row 0) at the
    left, and a horizontal mm ruler along raster row 0. Each colour prints one
    horizontal line at raster row 0 and one at row 168 (both 12 mm long), each
    at its own x, plus a vertical line at a known column. Reading the colour
    lines against the black rulers gives each array's row and column offset."""
    # CR2225: colour columns print at the same 600 dpi pitch and index as black columns;
    # the double-width colour buffers are capacity only, so colour planes use `cols` too.
    k, c, m, y = (page_pattern.Page(cols, rows, 1) for _ in range(4))
    def mm(v): return int(round(v * PX_MM))
    k.text(mm(14), 8, label + ' K', 4)
    for i in range(0, 13):                                                     # vertical ruler at x = 0..: ticks per mm
        r = mm(i)
        if r < rows: k.hline(r, 0, mm(3) if i % 5 == 0 else mm(1.5))
        if i % 5 == 0 and r + 20 < rows: k.text(mm(3.5), r + 2, str(i), 2)
    for i in range(0, int(cols / PX_MM) + 1):                                  # horizontal ruler along row 0..
        x = mm(i); k.vline(x, 0, mm(3) if i % 5 == 0 else mm(1.5))
        if i % 10 == 0 and i: k.text(x + 3, mm(3.5), str(i), 2)
    k.hline(0, 0, cols - 1); k.hline(rows - 1, 0, cols - 1)                  # black band edges
    # colour markers: horizontal lines at raster rows 0 and 168, vertical line at a known column
    for plane, x_mm, name in ((c, 12, 'C'), (m, 20, 'M'), (y, 28, 'Y')):
        x0 = mm(x_mm)
        plane.hline(0, x0, x0 + mm(6), 2); plane.hline(COLOUR_ROWS - 8, x0, x0 + mm(6), 2)
        plane.vline(x0, 0, COLOUR_ROWS - 1, 2)
        plane.text(x0 + mm(1), 24, name, 4)
    return {'K': k.img, 'C': c.img, 'M': m.img, 'Y': y.img}


def planes_probe(label, cols, rows):
    """Buffer-layout probe: colour planes uploaded 2*cols wide. Per colour: a solid
    bar rows 0..175 in the first half (cols x0..x0+63) and one in the second half
    (cols + x0 ..), 16-row stripes next to each. Black: mm rulers and a 304-row bar."""
    k = page_pattern.Page(cols, rows, 1)
    c, m, y = (page_pattern.Page(2 * cols, rows, 1) for _ in range(3))
    def mm(v): return int(round(v * PX_MM))
    k.text(mm(14), 8, label + ' P', 4)
    for i in range(0, 14):
        r = mm(i)
        if r < rows: k.hline(r, 0, mm(3) if i % 5 == 0 else mm(1.5))
    for i in range(0, int(cols / PX_MM) + 1):
        x = mm(i); k.vline(x, 0, mm(3) if i % 5 == 0 else mm(1.5))
        if i % 10 == 0 and i: k.text(x + 3, mm(3.5), str(i), 2)
    k.img[0:rows, mm(38):mm(40)] = True                                        # black 304-row reference bar at 38 mm
    for plane, x0 in ((m, 128), (c, 384), (y, 640)):
        for half in (0, cols):
            plane.img[0:COLOUR_ROWS, half + x0:half + x0 + 64] = True             # solid bar rows 0..175
            for r in range(0, COLOUR_ROWS, 32): plane.img[r:r + 16, half + x0 + 96:half + x0 + 160] = True   # 16 on / 16 off
    return {'K': k.img, 'C': c.img, 'M': m.img, 'Y': y.img}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--label', default='CB' + time.strftime('%H%M'))
    ap.add_argument('--cols', type=int, default=1024); ap.add_argument('--rows', type=int, default=304)
    ap.add_argument('--x-start', type=int, default=40000); ap.add_argument('--first-ulf', type=int, default=650)
    ap.add_argument('--no-feed', action='store_true'); ap.add_argument('--no-eject', action='store_true'); ap.add_argument('--no-head', action='store_true')
    ap.add_argument('--no-blank', action='store_true', help='skip the blank fresh pass')
    ap.add_argument('--layout-probe', action='store_true', help='buffer-layout probe pattern (colour planes 2*cols wide)')
    ap.add_argument('--extra-advance', type=int, default=0, help='additional stock LF steps of 1200 Ulf after the first-line advance')
    ap.add_argument('--log', type=Path); ap.add_argument('--serial'); ap.add_argument('--ambient', type=int, default=50)
    a = ap.parse_args()
    dev = hybrid_call.device(a.serial)
    log = bytearray(); record = {'label': a.label, 'cols': a.cols, 'rows': a.rows, 'steps': []}

    def drain():
        for _ in range(32):
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

    def run(routine, flags=0, index=None, limit=120):
        status = lambda: struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x98, 0xffff, 0, 64, timeout=5000)))
        before = status()[2]
        dev.ctrl_transfer(0x40, 0x97, routine | flags, a.ambient if index is None else index, b'', timeout=2000)
        t0 = time.monotonic()
        while status()[2] == before:
            if time.monotonic() - t0 > limit: raise TimeoutError('routine')
            time.sleep(0.1)
        st = status(); drain()
        rows = [struct.unpack('<4I', bytes(dev.ctrl_transfer(0xc0, 0x98, i, 0, 16, timeout=2000))) for i in range(min(st[4], 160))]
        moves = [(r[1], hex(r[2])) for r in rows if r[0] == 1]
        print(f'  routine {routine}|{flags:#x}: result {st[1]:#x} errors {st[3]} exceptions {st[11]} ({time.monotonic() - t0:.1f}s) CR {moves}')
        record['steps'].append({'routine': routine, 'flags': flags, 'index': index, 'result': st[1], 'errors': st[3], 'exceptions': st[11], 'cr': moves,
                                'log': [l for l in log.decode(errors='replace').splitlines() if '[shim]' in l]}); log.clear()
        if a.log: a.log.write_text(json.dumps(record, indent=1) + '\n')
        assert st[1] == (0 if routine in (8, 9, 10) else 1), st
        return st

    place = place_for_x(a.x_start)
    imgs = planes_probe(a.label, a.cols, a.rows) if a.layout_probe else planes(a.label, a.cols, a.rows)
    print(f'label {a.label}: {a.cols} columns x {a.rows} rows, placement {place}, planes', {k: int(v.sum()) for k, v in imgs.items()})
    print('1. home'); home()
    if not a.no_head: print('2. head power and prepare'); cart_read()
    if not a.no_feed:
        print('3. stock paper load'); run(8, 0x100 | 0x800 | 0x1000)
        asf = hybrid_call.read_words(dev, 0x180c6f54)[0] & 0xff
        if asf != 1: sys.exit('stock loader did not load a sheet')
        print(f'3b. first-line advance {a.first_ulf} Ulf'); run(10, 0x800, index=a.first_ulf)
        for _ in range(a.extra_advance): run(10, 0x800, index=1200)
    hybrid_call.band_params(dev, a.cols, a.rows, place, mode=7, colour=True)
    if not a.no_blank:
        print('4. blank fresh pass'); run(5, 0x400); verify_band_feed_init(dev)
        for k, g in GROUPS.items():
            m = hybrid_call.manager(dev, g); hybrid_call.upload_raster(dev, bytes(a.cols // 16 * m['stride']), verify=False, group=g)
        run(7, 0x100 | 0x1000)
    print('5. colour band'); run(5, 0x400 if a.no_blank else 0); verify_band_feed_init(dev)
    for k, g in GROUPS.items():
        m = hybrid_call.manager(dev, g); print(f'  manager {g} ({k}): {m}')
        data = page_pattern.encode_band(imgs[k], m['slots'])
        r = hybrid_call.upload_raster(dev, data, verify='sample', group=g); print(f'  upload {k}:', r); assert r['mismatches'] == 0, r
    run(7, 0x100 | 0x1000)
    if not a.no_feed and not a.no_eject: print('6. stock eject'); run(9, 0x100 | 0x800 | 0x400)
    print('7. home'); home()
    print('done', a.label)


if __name__ == '__main__':
    main()
