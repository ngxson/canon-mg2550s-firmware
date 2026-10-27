#!/usr/bin/env python3
"""One sheet, six colour sweeps, one variable each (owner present): why do
pages print only ~45 colour rows per band while a single pass prints 160?

Every sweep prints a black band label + row-0 tick and, per colour, a full
160-row bar (C at 12 mm, M at 20 mm, Y at 28 mm) so the printed bar height
per variant is read from one grayscale scan.

  A  first real pass after the blank fresh pass          (expect 160 rows)
  B  after a stock LF advance of 320 Ulf                 (page condition)
  C  no advance: same paper position as B, another pass
  D  advance 320, then a blank non-fresh pass, then the real pass
  E  advance 320, non-fresh, post-pass tail skipped (finish mask 0)
  F  advance 320, non-fresh, tail without e753c (mask 0x5f)

    colour_variants.py --first-ulf 650 --extra-advance 3     # below CB2159's three bands
"""
import argparse, json, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, hybrid_call, page_pattern
from band_live import verify_band_feed_init
from page_live import place_for_x
from colour_band import GROUPS

PX_MM = 600 / 25.4
VARIANTS = [('A', 0, False, 0xff), ('B', 320, False, 0xff), ('C', 0, False, 0xff), ('D', 320, 'blank', 0xff), ('E', 320, False, 0x00), ('F', 320, False, 0x5f)]
ALIGN_VARIANTS = [('S00', 0, 0), ('S08', 320, 8), ('S16', 320, 16), ('S24', 320, 24), ('S32', 320, 32), ('S40', 320, 40), ('S48', 320, 48), ('S56', 320, 56), ('AL', 320, 0x100)]   # (name, advance, raster_mode shift / aligned)
RESTORE_VARIANTS = [('N0', 0, 0x00), ('N1', 320, 0x01), ('N2', 320, 0x02), ('N4', 320, 0x04), ('N8', 320, 0x08), ('N16', 320, 0x10), ('N32', 320, 0x20), ('NALL', 320, 0x3f), ('N0b', 320, 0x00)]   # (name, advance, snapshot restore mask before the prepare)
PASS_VARIANTS = [('P1', 0, 'single'), ('P2A', 320, 'even'), ('P2B', 32, 'odd'), ('P1M', 288, 'single')]   # single pass; two interleaved passes 16 rows apart (even / odd columns); single again
ROWS_VARIANTS = [('R160', 0, 160, 160), ('R176', 320, 176, 160), ('R224', 320, 224, 160), ('R304', 320, 304, 160), ('R304F', 640, 304, 304)]   # (name, advance, selection rows, content rows)


def planes(label, cols, rows, name, content=160):
    k, c, m, y = (page_pattern.Page(cols, rows, 1) for _ in range(4))
    def mm(v): return int(round(v * PX_MM))
    k.text(mm(2), 8, label + ' ' + name, 4); k.hline(0, 0, mm(10)); k.hline(content - 1, 0, mm(4))
    k.img[0:content, mm(38):mm(40)] = True
    for plane, x in ((c, 12), (m, 20), (y, 28)):
        plane.img[0:content, mm(x):mm(x) + 48] = True
        plane.img[0:content, mm(x) + 64:mm(x) + 80] = ((np.arange(rows)[:, None] // 16) % 2 == 0)[0:content]   # 16-row stripes
    return {'K': k.img, 'C': np.concatenate([c.img, np.zeros_like(c.img)], axis=1), 'M': np.concatenate([m.img, np.zeros_like(m.img)], axis=1), 'Y': np.concatenate([y.img, np.zeros_like(y.img)], axis=1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--label', default='CV' + time.strftime('%H%M'))
    ap.add_argument('--cols', type=int, default=1024); ap.add_argument('--rows', type=int, default=160)
    ap.add_argument('--x-start', type=int, default=40000); ap.add_argument('--first-ulf', type=int, default=650); ap.add_argument('--extra-advance', type=int, default=0)
    ap.add_argument('--no-feed', action='store_true'); ap.add_argument('--no-eject', action='store_true'); ap.add_argument('--no-head', action='store_true')
    ap.add_argument('--log', type=Path); ap.add_argument('--serial'); ap.add_argument('--ambient', type=int, default=50)
    ap.add_argument('--restore-variants', action='store_true', help='nine sweeps: first pass, then passes with chosen snapshot RAM ranges re-copied before the prepare (which stock state the first pass leaves behind makes the 32-row lines)')
    ap.add_argument('--pass-variants', action='store_true', help='single-pass bars vs two interleaved passes 16 rows apart (even/odd columns): does multi-pass hide the 32-row lines?')
    ap.add_argument('--align-variants', action='store_true', help='nine sweeps varying the raster storage start (shift 0..56 bytes, then 64-byte aligned); black and colour 160-row bars show which starts drop a row group')
    ap.add_argument('--rows-variants', action='store_true', help='four sweeps varying the selection height (160/176/224/304) instead of the six tail/LF variants')
    a = ap.parse_args()
    dev = hybrid_call.device(a.serial)
    log = bytearray(); record = {'label': a.label, 'variants': VARIANTS, 'steps': []}

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
    print('1. home'); home()
    if not a.no_head: print('2. head power and prepare'); cart_read()
    if not a.no_feed:
        print('3. stock paper load'); run(8, 0x100 | 0x800 | 0x1000)
        if hybrid_call.read_words(dev, 0x180c6f54)[0] & 0xff != 1: sys.exit('stock loader did not load a sheet')
        run(10, 0x800, index=a.first_ulf)
        for _ in range(a.extra_advance): run(10, 0x800, index=1200)
    hybrid_call.band_params(dev, a.cols, a.rows, place, mode=7, colour=True)
    first = True   # the first real prepare restores the snapshot; no blank pass (brief 20 fix in the shim)
    if a.restore_variants: variants = [(n, adv, False, 0xff, 304, ('restore', rm), 0x100) for n, adv, rm in RESTORE_VARIANTS]
    elif a.pass_variants: variants = [(n, adv, False, 0xff, 304, kind, 0x100) for n, adv, kind in PASS_VARIANTS]
    elif a.align_variants: variants = [(n, adv, False, 0xff, 304, 160, mode) for n, adv, mode in ALIGN_VARIANTS]
    elif a.rows_variants: variants = [(n, adv, False, 0xff, rows, cont, 0x100) for n, adv, rows, cont in ROWS_VARIANTS]
    else: variants = [(n, adv, f, m, a.rows, 160, 0x100) for n, adv, f, m in VARIANTS]
    for name, advance, fresh, mask, sel_rows, content, rmode in variants:
        print(f'variant {name}: advance {advance}, fresh {fresh}, finish mask {mask:#x}, selection rows {sel_rows}, raster mode {rmode:#x}')
        if advance: run(10, 0x800, index=advance)
        hybrid_call.finish_mask(dev, mask); hybrid_call.raster_mode(dev, rmode)
        hybrid_call.restore_mask(dev, content[1] if isinstance(content, tuple) else 0)
        hybrid_call.band_params(dev, a.cols, sel_rows, place, mode=7, colour=True)
        if fresh == 'blank':
            run(5, 0x400 if first else 0); first = False; verify_band_feed_init(dev)
            for k, g in GROUPS.items():
                m = hybrid_call.manager(dev, g); hybrid_call.upload_raster(dev, bytes(m['width'] // 16 * m['stride']), verify=False, group=g)
            run(7, 0x100 | 0x1000)
        run(5, 0x400 if first else 0); first = False; verify_band_feed_init(dev)
        if isinstance(content, tuple): content = 160
        if isinstance(content, str):   # pass variants: 160-row bars, column parity and 16-row shift per pass
            imgs = planes(a.label, a.cols, sel_rows, name, 160)
            if content != 'single':
                for k in imgs:
                    img = imgs[k]; cols_ = np.arange(img.shape[1]); img &= (cols_ % 2 == (0 if content == 'even' else 1))[None, :]
                    if content == 'odd': img[:] = np.vstack([img[16:], np.zeros((16, img.shape[1]), dtype=bool)])   # second pass sits 16 rows further down the sheet
        else:
            imgs = planes(a.label, a.cols, sel_rows, name, content)
        for k, g in GROUPS.items():
            m = hybrid_call.manager(dev, g)
            r = hybrid_call.upload_raster(dev, page_pattern.encode_band(imgs[k], m['slots']), verify=False, group=g)
        run(7, 0x100 | 0x1000)
        hybrid_call.finish_mask(dev, 0xff); hybrid_call.raster_mode(dev, 0x100)
    if not a.no_feed and not a.no_eject: print('5. stock eject'); run(9, 0x100 | 0x800 | 0x400)
    print('6. home'); home(); print('done', a.label)


if __name__ == '__main__':
    main()
