#!/usr/bin/env python3
"""Build a labelled test raster for one stock band (hybrid routine 7) and
optionally upload it.

Band geometry (manager 6, hybrid band path): 320 columns along the carriage
sweep by 640 nozzle rows, stored as 20 tiles of 16 columns: byte offset =
tile*0x500 + group*16 + row_in_group*2, little-endian halfword, bit b = column
16*tile + b (bit 0 = leftmost in the RB2PB measurement), row = 8*group +
row_in_group. Which nozzle row is the paper's leading side and whether bit 0
prints first are not yet confirmed on paper: the pattern carries orientation
marks so one sheet settles both.

    band_pattern.py --label HYB1455 --write band.bin [--preview]
    band_pattern.py --label HYB1455 --upload          # after `hybrid_call.py run band-prepare`

Every sheet gets its own label (the default is HYB + current HHMM).
"""
import argparse, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stamp_plan import FONT

COLS, ROWS, TILE = 320, 640, 0x500


def text_columns(text, scale):
    cols = []
    for ch in text.upper():
        for c in FONT.get(ch, FONT[' ']):
            cols += [[bool(c >> r & 1) for r in range(7) for _ in range(scale)]] * scale
        cols += [[False] * (7 * scale)] * scale
    return cols


def build(label):
    img = [[False] * COLS for _ in range(ROWS)]
    def put(col, row, on=True):
        if 0 <= col < COLS and 0 <= row < ROWS: img[row][col] = on
    def text(col0, row0, s, scale):
        for i, column in enumerate(text_columns(s, scale)):
            for r, on in enumerate(column):
                if on: put(col0 + i, row0 + r)
    text(4, 2, label, 4)                                   # rows 2..29: the label, 24 columns per character
    for r in range(0, 30):                                 # right-edge marker bar (columns 312..315)
        for c in range(312, 316): put(c, r)
    for r in range(40, 104):                               # nozzle-group ladder: 8-row stripes
        if (r // 8) % 2 == 0:
            for c in range(0, 300): put(c, r)
    for r in range(112, 176):                              # vertical stripes 8 on / 8 off
        for c in range(COLS):
            if c % 16 < 8: put(c, r)
    for r in range(184, 248):                              # single-dot checkerboard
        for c in range(COLS):
            if (c + r) % 2 == 0: put(c, r)
    for r in range(256, 320):                              # solid block, then isolated dots every 8 columns
        for c in range(0, 160): put(c, r)
        for c in range(168, COLS, 8):
            if r % 8 == 0: put(c, r)
    for r in range(328, 392):                              # two diagonals: direction and skew
        put(r - 328, r); put(r - 327, r); put(COLS - 1 - (r - 328), r); put(COLS - 2 - (r - 328), r)
    for r in range(400, 464, 8):                           # one-row horizontal lines, every eighth nozzle
        for c in range(0, 300): put(c, r)
    text(4, 472, 'ROW 472', 2)                             # orientation check: which end of the band is this
    widths = (1, 2, 4, 8, 16, 32); seg = COLS // len(widths)
    for k, width in enumerate(widths):                     # rows 544..639: stripes of width 1, 2, 4, 8, 16, 32 columns
        for c in range(k * seg, (k + 1) * seg - 4):
            if ((c - k * seg) // width) % 2 == 0:
                for r in range(544, 640): put(c, r)
    return img


def encode(img):
    out = bytearray(TILE * (COLS // 16))
    for row in range(ROWS):
        g, r = divmod(row, 8)
        for tile in range(COLS // 16):
            hw = 0
            for b in range(16):
                if img[row][16 * tile + b]: hw |= 1 << b
            off = tile * TILE + g * 16 + r * 2
            out[off] = hw & 0xff; out[off + 1] = hw >> 8
    return bytes(out)


def decode(data):
    img = [[False] * COLS for _ in range(ROWS)]
    for row in range(ROWS):
        g, r = divmod(row, 8)
        for tile in range(COLS // 16):
            off = tile * TILE + g * 16 + r * 2
            hw = data[off] | data[off + 1] << 8
            for b in range(16): img[row][16 * tile + b] = bool(hw >> b & 1)
    return img


def preview(img, step=4):
    for row in range(0, ROWS, step * 2):
        print(''.join('#' if any(img[row + dr][col + dc] for dr in range(min(step * 2, ROWS - row)) for dc in range(step)) else '.' for col in range(0, COLS, step)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--label', default='HYB' + time.strftime('%H%M'))
    ap.add_argument('--write', type=Path)
    ap.add_argument('--upload', action='store_true')
    ap.add_argument('--preview', action='store_true')
    ap.add_argument('--serial')
    a = ap.parse_args()
    img = build(a.label); data = encode(img)
    assert decode(data) == img
    on = sum(sum(r) for r in img)
    print(f'label {a.label}: {len(data)} bytes, {on} dots of {COLS * ROWS}')
    if a.preview: preview(img)
    if a.write: a.write.write_bytes(data); print('wrote', a.write)
    if a.upload:
        import hybrid_call
        dev = hybrid_call.device(a.serial)
        print(hybrid_call.upload_raster(dev, data))


if __name__ == '__main__':
    main()
