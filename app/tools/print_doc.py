#!/usr/bin/env python3
"""Print a document (PNG/JPEG/PDF page) through the hybrid firmware over USB.

Rasterizes at 600 dpi into black (K) and C/M/Y planes (simple CMY separation,
ordered-dither halftone), then drives the band path: stock load, first-line
advance, per band of 160 rows: prepare (mode 7, selection 304), upload K/C/M/Y
tiles (colour in the 2N-wide layout, shifted by the colour array offset), stock
CR sweep, stock LF 320 Ulf; finally stock eject. Mono documents (`--mono`)
use mode 1 with 304-row bands.

    print_doc.py photo.jpg                 # colour, fit to the printable width
    print_doc.py page.pdf --mono --dpi-in 300
"""
import argparse, json, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, hybrid_call, page_pattern
from PIL import Image, ImageOps
from band_live import verify_band_feed_init
from page_live import place_for_x
from colour_band import GROUPS

PX_MM = 600 / 25.4
BAYER8 = np.array([[0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26], [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
                   [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25], [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]]) / 64.0


def load_image(path, page=0):
    if str(path).lower().endswith('.pdf'):
        import fitz   # PyMuPDF
        doc = fitz.open(str(path)); pix = doc[page].get_pixmap(dpi=600)
        im = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
    else:
        im = Image.open(path).convert('RGB')
    return ImageOps.exif_transpose(im)


def halftone(gray01):
    """gray01: ink coverage 0..1 -> bool dots, 8x8 Bayer ordered dither."""
    h, w = gray01.shape
    t = np.tile(BAYER8, (h // 8 + 1, w // 8 + 1))[:h, :w]
    return gray01 > t


def separate(im, cols, rows_total, mono, x_scale):
    """Resize to `cols` x rows_total (keeping aspect by height), return K/C/M/Y bool planes."""
    # Horizontal compensation before halftoning (a nearest-neighbour stretch of dithered
    # planes duplicates every ~42nd dot column and shows as faint vertical lines).
    hh = int(round(im.height * (cols / x_scale) / im.width))
    im = im.resize((cols, hh), Image.LANCZOS)
    canvas = Image.new('RGB', (cols, max(hh, 1)), 'white'); canvas.paste(im, (0, 0))
    rgb = np.asarray(canvas).astype(float) / 255.0
    c, m, y = 1 - rgb[..., 0], 1 - rgb[..., 1], 1 - rgb[..., 2]
    k = np.minimum(np.minimum(c, m), y)                       # grey component replacement
    if mono:
        planes = {'K': halftone(1 - rgb.mean(axis=2))}
    else:
        planes = {'K': halftone(k), 'C': halftone(np.clip(c - k, 0, 1)), 'M': halftone(np.clip(m - k, 0, 1)), 'Y': halftone(np.clip(y - k, 0, 1))}
    return planes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file', type=Path); ap.add_argument('--page', type=int, default=0)
    ap.add_argument('--mono', action='store_true')
    ap.add_argument('--cols', type=int, default=4224, help='printable width in 600 dpi columns (4224 = 178.8 mm)')
    ap.add_argument('--seg-cols', type=int, default=2112, help='columns per carriage sweep (colour: 2112 fits the arena)')
    ap.add_argument('--max-rows', type=int, default=6400)
    ap.add_argument('--x-scale', type=float, default=1.024); ap.add_argument('--colour-offset', type=int, default=128)
    ap.add_argument('--blank-pass', action='store_true', help='print a blank fresh pass first (no longer needed: the first-pass mask shift is suppressed in the shim)')
    ap.add_argument('--black-mode1', action='store_true', help='print the black plane in its own mode-1 sweep per band (black in mode 7 misses a row group every 32 rows); doubles the sweeps')
    ap.add_argument('--band-rows', type=int, default=0, help='rows per band: default 152 colour (multiple of 8; the arrays print 156-160 of 160 rows, so 152 overlaps slightly instead of leaving a seam gap), 304 mono')
    ap.add_argument('--x-start', type=int, default=35008); ap.add_argument('--first-ulf', type=int, default=650)
    ap.add_argument('--no-feed', action='store_true'); ap.add_argument('--no-eject', action='store_true'); ap.add_argument('--no-head', action='store_true')
    ap.add_argument('--preview', type=Path, help='write the halftoned planes as a PNG and exit')
    ap.add_argument('--log', type=Path); ap.add_argument('--serial'); ap.add_argument('--ambient', type=int, default=50)
    a = ap.parse_args()
    band_rows = a.band_rows or (304 if a.mono else 152)
    assert band_rows % 8 == 0 and 8 <= band_rows <= 304, 'band rows must be a multiple of 8 (tile groups)'
    seg = a.seg_cols if not a.mono else min(a.seg_cols * 2, a.cols)
    assert a.cols % seg == 0 and seg % 32 == 0, 'segment must divide the width'
    im = load_image(a.file, a.page)
    planes = separate(im, a.cols, a.max_rows, a.mono, a.x_scale)
    H = min(planes['K'].shape[0], a.max_rows)
    bands = (H + band_rows - 1) // band_rows
    label = 'DOC' + time.strftime('%H%M')
    print(f'{a.file.name}: {a.cols} x {H} dots ({a.cols / PX_MM:.0f} x {H / PX_MM:.0f} mm), {bands} bands of {band_rows} rows, {a.cols // seg} sweeps per band, planes', {k: int(v.sum()) for k, v in planes.items()})
    if a.preview:
        rgb = np.full((H, a.cols, 3), 255, dtype=np.uint8)
        for key, col in (('C', (0, 174, 239)), ('M', (236, 0, 140)), ('Y', (255, 241, 0)), ('K', (0, 0, 0))):
            if key in planes: rgb[planes[key][:H]] = np.minimum(rgb[planes[key][:H]], col)
        Image.fromarray(rgb).save(a.preview); print('wrote', a.preview); return
    dev = hybrid_call.device(a.serial); record = {'label': label, 'file': str(a.file), 'steps': []}

    def wait(req, n_words, length, limit=30):
        t0 = time.monotonic()
        while True:
            w = struct.unpack('<%dI' % n_words, bytes(dev.ctrl_transfer(0xc0, req, 0, 0, length, timeout=1000)))
            if w[2] == 0 and w[3] != 1: return w
            if time.monotonic() - t0 > limit: raise TimeoutError(hex(req))
            time.sleep(0.03)

    def home():
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); w = wait(0x6e, 12, 48); assert w[3] == 2, w

    def cart_read():
        before = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
        dev.ctrl_transfer(0x40, 0x62, 3 | 0x200, 0, b'', timeout=1000)
        t0 = time.monotonic()
        while True:
            s = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
            if s[2] == 2 and s[5] != before[5]: break
            if time.monotonic() - t0 > 30: raise TimeoutError('cartridge')
            time.sleep(0.05)

    def run(routine, flags=0, index=None, limit=120):
        status = lambda: struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x98, 0xffff, 0, 64, timeout=5000)))
        before = status()[2]
        dev.ctrl_transfer(0x40, 0x97, routine | flags, a.ambient if index is None else index, b'', timeout=2000)
        t0 = time.monotonic()
        while status()[2] == before:
            if time.monotonic() - t0 > limit: raise TimeoutError('routine')
            time.sleep(0.1)
        st = status()
        record['steps'].append({'routine': routine, 'flags': flags, 'index': index, 'result': st[1], 'errors': st[3], 'exceptions': st[11]})
        if a.log: a.log.write_text(json.dumps(record, indent=1) + '\n')
        assert st[1] == (0 if routine in (8, 9, 10) else 1), (routine, st)

    t_start = time.monotonic()
    home()
    if not a.no_head: cart_read()
    if not a.no_feed:
        run(8, 0x100 | 0x800 | 0x1000)
        if hybrid_call.read_words(dev, 0x180c6f54)[0] & 0xff != 1: sys.exit('no sheet loaded')
        run(10, 0x800, index=a.first_ulf)
    groups = {'K': 6} if a.mono else GROUPS
    mode = 1 if a.mono else 7
    first = True
    for b in range(bands):
        t_b = time.monotonic()
        for c0 in range(0, a.cols, seg):
            place = place_for_x(a.x_start + 16 * c0)
            hybrid_call.band_params(dev, seg, 304, place, mode=mode, colour=not a.mono)
            if first and a.blank_pass:   # optional blank fresh pass (pre brief-20 workaround)
                run(5, 0x400); verify_band_feed_init(dev)
                for k, g in groups.items():
                    m = hybrid_call.manager(dev, g); hybrid_call.upload_raster(dev, bytes(m['width'] // 16 * m['stride']), verify=False, group=g)
                run(7, 0x100 | 0x1000)
            fresh_flag = 0x400 if first and not a.blank_pass else 0; first = False
            def slice_plane(k):
                r0 = band_rows * b + (0 if k == 'K' else a.colour_offset)
                sl = planes[k][r0:r0 + band_rows, c0:c0 + seg]
                if sl.shape[0] < band_rows: sl = np.pad(sl, ((0, band_rows - sl.shape[0]), (0, 0)), constant_values=False)
                return sl
            if a.black_mode1 and not a.mono:
                # black alone in a mode-1 sweep (clean rows), then the colour planes in a mode-7 sweep with black empty
                hybrid_call.band_params(dev, seg, 304, place, mode=1, colour=False)
                run(5, fresh_flag); fresh_flag = 0; verify_band_feed_init(dev)
                m = hybrid_call.manager(dev, 6); hybrid_call.upload_raster(dev, page_pattern.encode_band(slice_plane('K'), m['slots']), verify=False, group=6)
                run(7, 0x100 | 0x1000)
                hybrid_call.band_params(dev, seg, 304, place, mode=7, colour=True)
            run(5, fresh_flag); verify_band_feed_init(dev)
            for k, g in groups.items():
                m = hybrid_call.manager(dev, g)
                sl = slice_plane(k)
                if k == 'K' and a.black_mode1 and not a.mono: sl = np.zeros_like(sl)
                if k != 'K': sl = np.concatenate([sl, np.zeros_like(sl)], axis=1)
                hybrid_call.upload_raster(dev, page_pattern.encode_band(sl, m['slots']), verify=False, group=g)
            run(7, 0x100 | 0x1000)
        if b < bands - 1: run(10, 0x800, index=2 * band_rows)
        print(f'  band {b + 1}/{bands} {time.monotonic() - t_b:.1f}s, total {time.monotonic() - t_start:.0f}s')
    if not a.no_feed and not a.no_eject: run(9, 0x100 | 0x800 | 0x400)
    home(); print(f'done {label} in {time.monotonic() - t_start:.0f}s')


if __name__ == '__main__':
    main()
