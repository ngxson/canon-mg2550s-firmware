#!/usr/bin/env python3
"""Colour test page through the hybrid shim (stock print mode 7, owner present).

Bands of 176 rows (the colour arrays' 22 groups); black prints the same 176
rows per band. The colour arrays sit `--colour-offset` rows (default 112,
CR2225: ~4.7 mm) below black's row 0 on the paper, so band k carries black
page rows [176k, 176k+176) and colour page rows [176k+off, 176k+off+176).
Sweeps of `--cols` columns (2112 = 89 mm fits one sweep of the 1.5 MiB arena).

    colour_page.py                      # 24 bands, sheet in the tray
"""
import argparse, json, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, hybrid_call, page_pattern
from band_live import verify_band_feed_init
from page_live import place_for_x
from colour_band import GROUPS

ROWS = 160          # rows per band: the colour arrays print ~160 of their 176 raster rows (CL2302)
PX_MM = 600 / 25.4


def build(label, cols, bands, off):
    H = ROWS * bands + off + ROWS
    K, C, M, Y = (page_pattern.Page(cols, H, 1) for _ in range(4))
    def mm(v): return int(round(v * PX_MM))
    W = cols
    K.rect(0, 0, W - 1, ROWS * bands - 1)
    for i in range(0, int(W / PX_MM) + 1):
        x = mm(i); K.vline(x, 0, mm(3) if i % 10 == 0 else mm(1.5) if i % 5 == 0 else mm(0.8))
        if i % 10 == 0 and i: K.text(x + 4, mm(3.5), str(i), 2)
    for i in range(0, int(ROWS * bands / PX_MM) + 1):
        r = mm(i); K.hline(r, 0, mm(3) if i % 10 == 0 else mm(1.5) if i % 5 == 0 else mm(0.8))
        if i % 10 == 0 and i: K.text(mm(3.5), r + 4, str(i), 2)
    K.text(mm(10), mm(8), label, 8); K.text(mm(10), mm(8) + 60, 'HYBRID COLOUR PAGE ' + time.strftime('%Y-%m-%d %H:%M'), 4)
    # colour bars: 10 mm tall, side by side, then overlaps (C+M, M+Y, C+Y, C+M+Y)
    y0 = mm(22)
    for plane, x in ((C, 10), (M, 22), (Y, 34)):
        plane.img[y0:y0 + mm(10), mm(x):mm(x + 10)] = True
    for planes_, x in (((C, M), 48), ((M, Y), 60), ((C, Y), 72), ((C, M, Y), 84)):
        for plane in planes_: plane.img[y0:y0 + mm(10), mm(x):mm(x + 10)] = True
    # colour text and halftone ramps (checker densities 100/50/25 %)
    C.text(mm(10), mm(36), 'CYAN', 6); M.text(mm(40), mm(36), 'MAGENTA', 6); Y.text(mm(76), mm(36), 'YELLOW', 6)
    yy, xx = np.mgrid[0:H, 0:W]
    for plane, x in ((C, 10), (M, 36), (Y, 62)):
        r0 = mm(48)
        plane.img[r0:r0 + mm(8), mm(x):mm(x + 8)] = True
        plane.img[r0:r0 + mm(8), mm(x + 9):mm(x + 17)] = ((xx + yy) % 2 == 0)[r0:r0 + mm(8), mm(x + 9):mm(x + 17)]
        plane.img[r0:r0 + mm(8), mm(x + 18):mm(x + 26)] = ((xx % 2 == 0) & (yy % 2 == 0))[r0:r0 + mm(8), mm(x + 18):mm(x + 26)]
    # concentric circles: C, M, Y rings around the page centre; black cross
    cy, cx = mm(110), W // 2
    rr = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    for plane, rad in ((C, 30), (M, 22), (Y, 14)):
        plane.img |= (np.abs(rr - mm(rad)) < 6)
    K.hline(cy, cx - mm(35), cx + mm(35)); K.vline(cx, cy - mm(35), cy + mm(35))
    # per-band seam marks for colour registration: black tick at band tops, colour ticks at the same page rows
    for b in range(bands):
        r = ROWS * b
        K.hline(r, W - mm(12), W - mm(9))
        C.hline(r, W - mm(8), W - mm(6)); M.hline(r, W - mm(5.5), W - mm(3.5)); Y.hline(r, W - mm(3), W - mm(1))
    # diagonal in all four
    for k in range(ROWS * bands):
        x = int(k * (W - 1) / (ROWS * bands - 1))
        K.dot(x, k); C.dot(x + 3, k); M.dot(x + 6, k); Y.dot(x + 9, k)
    K.text(W - mm(45), ROWS * bands - mm(8), 'BOTTOM RIGHT ' + label, 4)
    return {'K': K.img, 'C': C.img, 'M': M.img, 'Y': Y.img}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--label', default='CP' + time.strftime('%H%M'))
    ap.add_argument('--cols', type=int, default=2112); ap.add_argument('--bands', type=int, default=26)
    ap.add_argument('--colour-offset', type=int, default=128, help='rows the colour arrays sit below black row 0 (CL2302: ~5.4 mm)')
    ap.add_argument('--x-start', type=int, default=35008); ap.add_argument('--first-ulf', type=int, default=650)
    ap.add_argument('--no-feed', action='store_true'); ap.add_argument('--no-eject', action='store_true'); ap.add_argument('--no-head', action='store_true')
    ap.add_argument('--log', type=Path); ap.add_argument('--serial'); ap.add_argument('--ambient', type=int, default=50)
    ap.add_argument('--x-scale', type=float, default=1.0, help='horizontal compensation (PG2052/CP2238 printed 0.975-0.977 of the designed width; 1.024 corrects it)')
    a = ap.parse_args()
    dev = hybrid_call.device(a.serial)
    log = bytearray(); record = {'label': a.label, 'cols': a.cols, 'bands': a.bands, 'offset': a.colour_offset, 'steps': []}

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
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); w = wait(0x6e, 12, 48); print(f'  home: result {w[3]}'); assert w[3] == 2, w

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
        st = status(); drain(); log.clear()
        record['steps'].append({'routine': routine, 'flags': flags, 'index': index, 'result': st[1], 'errors': st[3], 'exceptions': st[11], 'seconds': round(time.monotonic() - t0, 1)})
        if a.log: a.log.write_text(json.dumps(record, indent=1) + '\n')
        assert st[1] == (0 if routine in (8, 9, 10) else 1), (routine, st)
        return st

    place = place_for_x(a.x_start)
    imgs = build(a.label, a.cols, a.bands, a.colour_offset)
    if a.x_scale != 1.0: imgs = {k: page_pattern.stretch_columns(v, a.x_scale) for k, v in imgs.items()}
    print(f'label {a.label}: {a.cols} columns, {a.bands} bands of {ROWS} rows, colour offset {a.colour_offset}, placement {place}, dots', {k: int(v.sum()) for k, v in imgs.items()})
    t_start = time.monotonic()
    print('1. home'); home()
    if not a.no_head: print('2. head power and prepare'); cart_read()
    if not a.no_feed:
        print('3. stock paper load'); run(8, 0x100 | 0x800 | 0x1000)
        if hybrid_call.read_words(dev, 0x180c6f54)[0] & 0xff != 1: sys.exit('stock loader did not load a sheet')
        print(f'3b. first-line advance {a.first_ulf} Ulf'); run(10, 0x800, index=a.first_ulf)
    # Selection height 304 (all 38 entries): the colour lanes print only the rows their
    # selection reaches (CR0006: 160 -> 28 rows, 224 -> 92, 304 -> 160); content stays 160 rows.
    hybrid_call.band_params(dev, a.cols, 304, place, mode=7, colour=True)
    print('4. blank fresh pass'); run(5, 0x400); verify_band_feed_init(dev)
    for k, g in GROUPS.items():
        m = hybrid_call.manager(dev, g); hybrid_call.upload_raster(dev, bytes(m['width'] // 16 * m['stride']), verify=False, group=g)
    run(7, 0x100 | 0x1000)
    for b in range(a.bands):
        t_b = time.monotonic()
        run(5, 0); verify_band_feed_init(dev)
        for k, g in GROUPS.items():
            m = hybrid_call.manager(dev, g)
            r0 = ROWS * b + (0 if k == 'K' else a.colour_offset)
            band = imgs[k][r0:r0 + ROWS]
            if k != 'K': band = np.concatenate([band, np.zeros_like(band)], axis=1)   # colour planes 2N wide: the engine consumes the left half only (CL2302)
            data = page_pattern.encode_band(band, m['slots'])
            r = hybrid_call.upload_raster(dev, data, verify=False, group=g)
        run(7, 0x100 | 0x1000)
        if b < a.bands - 1: run(10, 0x800, index=2 * ROWS)
        print(f'  band {b:02d}/{a.bands} done in {time.monotonic() - t_b:.1f}s, total {time.monotonic() - t_start:.0f}s')
    if not a.no_feed and not a.no_eject: print('5. stock eject'); run(9, 0x100 | 0x800 | 0x400)
    print('6. home'); home()
    print(f'done: {a.label} in {time.monotonic() - t_start:.0f}s')


if __name__ == '__main__':
    main()
