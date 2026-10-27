#!/usr/bin/env python3
"""Render a labelled full-page geometry/nozzle test and slice it into stock
band rasters for the hybrid band path (page_live.py).

Page model: `cols` columns across the carriage sweep at 600 dpi (16 CR counts
per column) by `bands * rows` rows along the paper at 600 dpi; each band is
`rows` black nozzle rows (304 = the 38 eight-row groups the stock selection
matrix can address). The
paper advances `rows` rows (2 Ulf per row, Ulf = 1/1200 inch) between bands.
Band k holds page rows k*rows .. (k+1)*rows-1; raster row g*8+r of a band is
stored at tile*0x500 + 16*g + 2*r (little-endian halfword, bit b = column
16*tile + b), the layout measured on the board (emu/evidence/stockapp/band).

What the sheet measures (every element is placed from the page origin, so
the scan gives absolute geometry):
  - mm rulers along the top and left edges (ticks every 1 mm, numbers every
    10 mm): scale in both axes, first-line and left-margin offsets
  - one-pixel frame and vertical lines through every band: band pitch
    (gaps/overlaps at the band seams), carriage/paper skew
  - per band: its index as text, seam marks on its first and last row, and a
    staircase (row r -> column r) that shows the physical nozzle order and
    whether the band is mirrored along the paper axis
  - nozzle map in band 1: one-row lines every eighth row, 8-on/8-off stripes
  - diagonal and centre cross, small density patches
Ink is kept low: lines are one or two pixels, no large solid areas.

    page_pattern.py --label PG1234 --preview page.png     # render only
    page_pattern.py --label PG1234 --write out_dir        # band_00.bin ...
"""
import argparse, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 5x7 font, column-major, bit 0 = top row (same convention as stamp_plan.FONT, complete A-Z 0-9).
FONT = {
    'A': [0x7e, 0x09, 0x09, 0x09, 0x7e], 'B': [0x7f, 0x49, 0x49, 0x49, 0x36], 'C': [0x3e, 0x41, 0x41, 0x41, 0x22],
    'D': [0x7f, 0x41, 0x41, 0x22, 0x1c], 'E': [0x7f, 0x49, 0x49, 0x49, 0x41], 'F': [0x7f, 0x09, 0x09, 0x09, 0x01],
    'G': [0x3e, 0x41, 0x49, 0x49, 0x7a], 'H': [0x7f, 0x08, 0x08, 0x08, 0x7f], 'I': [0x00, 0x41, 0x7f, 0x41, 0x00],
    'J': [0x20, 0x40, 0x41, 0x3f, 0x01], 'K': [0x7f, 0x08, 0x14, 0x22, 0x41], 'L': [0x7f, 0x40, 0x40, 0x40, 0x40],
    'M': [0x7f, 0x02, 0x0c, 0x02, 0x7f], 'N': [0x7f, 0x04, 0x08, 0x10, 0x7f], 'O': [0x3e, 0x41, 0x41, 0x41, 0x3e],
    'P': [0x7f, 0x09, 0x09, 0x09, 0x06], 'Q': [0x3e, 0x41, 0x51, 0x21, 0x5e], 'R': [0x7f, 0x09, 0x19, 0x29, 0x46],
    'S': [0x46, 0x49, 0x49, 0x49, 0x31], 'T': [0x01, 0x01, 0x7f, 0x01, 0x01], 'U': [0x3f, 0x40, 0x40, 0x40, 0x3f],
    'V': [0x1f, 0x20, 0x40, 0x20, 0x1f], 'W': [0x3f, 0x40, 0x38, 0x40, 0x3f], 'X': [0x63, 0x14, 0x08, 0x14, 0x63],
    'Y': [0x07, 0x08, 0x70, 0x08, 0x07], 'Z': [0x61, 0x51, 0x49, 0x45, 0x43],
    '0': [0x3e, 0x51, 0x49, 0x45, 0x3e], '1': [0x00, 0x42, 0x7f, 0x40, 0x00], '2': [0x42, 0x61, 0x51, 0x49, 0x46],
    '3': [0x21, 0x41, 0x45, 0x4b, 0x31], '4': [0x18, 0x14, 0x12, 0x7f, 0x10], '5': [0x27, 0x45, 0x45, 0x45, 0x39],
    '6': [0x3c, 0x4a, 0x49, 0x49, 0x30], '7': [0x01, 0x71, 0x09, 0x05, 0x03], '8': [0x36, 0x49, 0x49, 0x49, 0x36],
    '9': [0x06, 0x49, 0x49, 0x29, 0x1e], '-': [0x08, 0x08, 0x08, 0x08, 0x08], ':': [0x00, 0x36, 0x36, 0x00, 0x00],
    '.': [0x00, 0x60, 0x60, 0x00, 0x00], '/': [0x20, 0x10, 0x08, 0x04, 0x02], ' ': [0, 0, 0, 0, 0],
}

DPI = 600
PX_PER_MM = DPI / 25.4
TILE = 0x500


class Page:
    def __init__(self, cols, rows, bands):
        self.cols, self.rows, self.bands = cols, rows, bands
        self.height = rows * bands
        self.img = np.zeros((self.height, cols), dtype=bool)

    def dot(self, c, r):
        if 0 <= c < self.cols and 0 <= r < self.height: self.img[r, c] = True

    def hline(self, r, c0, c1, thick=1):
        c0, c1 = max(0, min(c0, c1)), min(self.cols, max(c0, c1) + 1)
        for t in range(thick):
            if 0 <= r + t < self.height: self.img[r + t, c0:c1] = True

    def vline(self, c, r0, r1, thick=1):
        r0, r1 = max(0, min(r0, r1)), min(self.height, max(r0, r1) + 1)
        for t in range(thick):
            if 0 <= c + t < self.cols: self.img[r0:r1, c + t] = True

    def text(self, c0, r0, s, scale):
        """5x7 font (stamp_plan.FONT), column-major, bit 0 = top row."""
        c = c0
        for ch in s.upper():
            for col in FONT.get(ch, FONT[' ']):
                for r in range(7):
                    if col >> r & 1: self.img[r0 + r * scale:r0 + (r + 1) * scale, c:c + scale] = True
                c += scale
            c += scale

    def rect(self, c0, r0, c1, r1):
        self.hline(r0, c0, c1); self.hline(r1, c0, c1); self.vline(c0, r0, r1); self.vline(c1, r0, r1)


def mm(v): return int(round(v * PX_PER_MM))


def build(label, cols=4224, rows=304, bands=21):
    p = Page(cols, rows, bands)
    H, W = p.height, cols
    # frame and long verticals through all bands (seam continuity)
    p.rect(0, 0, W - 1, H - 1)
    for c in (mm(20), mm(60), W // 2, W - mm(60), W - mm(20)): p.vline(c, 0, H - 1)
    # rulers: top edge (columns) and left edge (rows), origin = page corner
    for i in range(0, int(W / PX_PER_MM) + 1):
        c = mm(i); n = mm(4) if i % 10 == 0 else mm(2.5) if i % 5 == 0 else mm(1.2)
        p.vline(c, 0, n)
        if i % 10 == 0 and 0 < i: p.text(c + 4, mm(1), str(i), 3)
    for i in range(0, int(H / PX_PER_MM) + 1):
        r = mm(i); n = mm(4) if i % 10 == 0 else mm(2.5) if i % 5 == 0 else mm(1.2)
        p.hline(r, 0, n)
        if i % 10 == 0 and 0 < i: p.text(mm(1.2), r + 4, str(i), 3)
    # label: big, top-left inside the ruler band; plus a readable orientation word
    p.text(mm(12), mm(7), label, 8)
    p.text(mm(12), mm(7) + 60, 'TOP LEFT  HYBRID BAND PAGE  ' + time.strftime('%Y-%m-%d %H:%M'), 4)
    # diagonals and centre cross
    for k in range(H):
        p.dot(int(k * (W - 1) / (H - 1)), k); p.dot(int(k * (W - 1) / (H - 1)) + 1, k)
    p.hline(H // 2, W // 2 - mm(15), W // 2 + mm(15)); p.vline(W // 2, H // 2 - mm(15), H // 2 + mm(15))
    # per band: index text, seam marks, nozzle-order staircase
    for b in range(bands):
        r0 = b * rows
        p.text(mm(8), r0 + mm(4), f'BAND {b:02d}', 4)   # below the top ruler's numbers in band 0
        p.hline(r0, mm(25), mm(40)); p.hline(r0 + rows - 1, mm(25), mm(40))          # seam marks: first and last row
        p.hline(r0, mm(42), mm(45), 2); p.hline(r0 + rows - 3, mm(42), mm(45), 2)   # 2-row marks 2 rows in
        for r in range(rows):                                                        # staircase: row r -> column r (two dots)
            p.dot(mm(48) + r, r0 + r); p.dot(mm(48) + r + 1, r0 + r)
        for r in range(0, rows, 8): p.hline(r0 + r, mm(48) + rows + 16, mm(48) + rows + 16 + 24)   # eight-row group marks
    # nozzle map in band 1: one-row lines every eighth row, then 8-on/8-off stripes, then 1-px grid
    r0 = rows
    for r in range(0, rows, 8): p.hline(r0 + r, mm(70), mm(90))
    for r in range(rows):
        if (r // 8) % 2 == 0: p.hline(r0 + r, mm(92), mm(100))
    for r in range(r0, r0 + rows, 16): p.hline(r, mm(102), mm(120))
    for c in range(mm(102), mm(120), 16): p.vline(c, r0, r0 + rows - 1)
    # density patches (3 x 3 mm) in band 2: 100 %, 50 % checker, 25 %
    r0 = 2 * rows + 40
    p.img[r0:r0 + mm(3), mm(70):mm(73)] = True
    yy, xx = np.mgrid[r0:r0 + mm(3), mm(75):mm(78)]; p.img[r0:r0 + mm(3), mm(75):mm(78)] = (xx + yy) % 2 == 0
    yy, xx = np.mgrid[r0:r0 + mm(3), mm(80):mm(83)]; p.img[r0:r0 + mm(3), mm(80):mm(83)] = (xx % 2 == 0) & (yy % 2 == 0)
    # widths test in band 3: vertical stripes 1, 2, 4, 8, 16 px wide
    r0 = 3 * rows + 40; c = mm(70)
    for w in (1, 2, 4, 8, 16):
        for k in range(6):
            p.img[r0:r0 + mm(6), c + 2 * k * w:c + (2 * k + 1) * w] = True
        c += 14 * w
    # bottom-right text for the trailing end
    p.text(W - mm(60), H - mm(8), 'BOTTOM RIGHT ' + label, 4)
    return p


def encode_band(band, slots=80):
    """(rows, cols) bool -> tile-layout bytes: cols/16 tiles of 16*slots bytes (the manager's
    stride = 2 * slots * 8), group g of 8 rows at +16g, row r at +2r, bit b = column 16*tile + b."""
    rows, cols = band.shape
    assert rows % 8 == 0 and rows <= 8 * slots and cols % 16 == 0, (band.shape, slots)
    tiles = cols // 16
    packed = np.packbits(band.reshape(rows, tiles, 16), axis=2, bitorder='little')   # (rows, tiles, 2)
    out = np.zeros((tiles, slots, 8, 2), dtype=np.uint8)
    out[:, :rows // 8] = packed.reshape(rows // 8, 8, tiles, 2).transpose(2, 0, 1, 3)
    return out.tobytes()


def decode_band(data, rows, cols, slots=80):
    tiles = cols // 16
    arr = np.frombuffer(data, dtype=np.uint8).reshape(tiles, slots, 8, 2)[:, :rows // 8].transpose(1, 2, 0, 3).reshape(rows, tiles, 2)
    return np.unpackbits(arr, axis=2, bitorder='little').reshape(rows, cols).astype(bool)


def stretch_columns(img, x_scale):
    """Horizontal scale compensation: the hybrid prints columns about 2 % narrower than
    1/600 inch (PG2052: 174.6 mm for 178.8 designed), so a design feature meant for
    column x must be sent at column x * x_scale. Nearest-neighbour resample; the
    rightmost (1 - 1/x_scale) of the design falls off the sweep."""
    if abs(x_scale - 1.0) < 1e-6: return img
    cols = img.shape[1]
    src = np.minimum((np.arange(cols) / x_scale).astype(int), cols - 1)
    return img[:, src]


def bands_of(page):
    return [encode_band(page.img[b * page.rows:(b + 1) * page.rows]) for b in range(page.bands)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--label', default='PG' + time.strftime('%H%M'))
    ap.add_argument('--cols', type=int, default=4224); ap.add_argument('--rows', type=int, default=304); ap.add_argument('--bands', type=int, default=21)
    ap.add_argument('--preview', type=Path, help='write a PNG of the page (1 px = 1 dot)')
    ap.add_argument('--write', type=Path, help='directory for band_NN.bin')
    a = ap.parse_args()
    page = build(a.label, a.cols, a.rows, a.bands)
    data = bands_of(page)
    for b, d in enumerate(data): assert np.array_equal(decode_band(d, a.rows, a.cols), page.img[b * a.rows:(b + 1) * a.rows])
    on = int(page.img.sum())
    print(f'label {a.label}: {a.cols} x {page.height} ({a.cols / PX_PER_MM:.1f} x {page.height / PX_PER_MM:.1f} mm), {a.bands} bands of {len(data[0])} bytes, {on} dots ({100 * on / page.img.size:.2f} %)')
    if a.preview:
        from PIL import Image
        Image.fromarray(np.where(page.img, 0, 255).astype(np.uint8)).save(a.preview); print('wrote', a.preview)
    if a.write:
        a.write.mkdir(parents=True, exist_ok=True)
        for b, d in enumerate(data): (a.write / f'band_{b:02d}.bin').write_bytes(d)
        print('wrote', a.bands, 'bands to', a.write)


if __name__ == '__main__':
    main()
