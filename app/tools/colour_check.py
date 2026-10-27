#!/usr/bin/env python3
"""Measure a colour page scan (150 dpi grayscale) from colour_page.py: ring
continuity (dashes = per-band gaps), registration ticks at the right edge
(black vs colour tick rows), and the colour bar block positions.

    colour_check.py app/hybrid/evidence/page/scan_CP2314.png
"""
import sys, json
import numpy as np
from PIL import Image

PXMM = 150 / 25.4


def runs(mask, lo=1):
    idx = np.where(mask)[0]; g = []
    for i in idx:
        if g and i - g[-1][-1] <= 1: g[-1].append(i)
        else: g.append([i])
    return [(x[0], len(x)) for x in g if len(x) >= lo]


def main():
    a = np.asarray(Image.open(sys.argv[1]).convert('L')).astype(float)
    dark = a < 200
    # page frame: longest horizontal dark run near the top and longest vertical run at the left
    rowlen = dark[:, :].sum(axis=1); ytop = int(np.argmax(rowlen[60:400])) + 60
    collen = dark[ytop:ytop + 900, :].sum(axis=0); xleft = int(np.argmax(collen[40:400])) + 40
    xright = int(np.argmax(collen[xleft + 300:xleft + 700])) + xleft + 300
    print(f'frame: top y {ytop}, left x {xleft}, right x {xright} -> width {(xright - xleft) / PXMM:.1f} mm')
    # registration ticks: black at W-12..W-9 mm, colour at W-8..W-6 (C), W-5.5..W-3.5 (M), W-3..W-1 (Y)
    def ticks(mm0, mm1, thr, name):
        x0, x1 = xright - int(mm0 * PXMM), xright - int(mm1 * PXMM)
        prof = (a[ytop:ytop + 1100, x0:x1] < thr).sum(axis=1)
        r = runs(prof >= 0.5 * (x1 - x0))
        rows = [y for y, n in r]
        print(f'{name} ticks rows (px below frame top): {rows[:30]}; pitch {np.mean(np.diff(rows)) / PXMM:.2f} mm' if len(rows) > 1 else f'{name}: {rows}')
        return rows
    kb = ticks(12, 9, 200, 'black')
    cb = ticks(8, 6, 240, 'cyan')
    mb = ticks(5.5, 3.5, 215, 'magenta')
    yb = ticks(3, 1, 245, 'yellow')
    if kb and mb:
        d = [min(abs(m - k) for k in kb) for m in mb]
        print(f'magenta tick offset from nearest black tick: median {np.median(d) / PXMM:.2f} mm')
    # ring dashes: column through the ring sides at radius 22 mm (magenta) left of the page centre
    cx = (xleft + xright) // 2; cy = ytop + int(110 * PXMM)
    for name, rad, thr in (('magenta ring r=22', 22, 215), ('cyan ring r=30', 30, 240), ('yellow ring r=14', 14, 245)):
        x = cx - int(rad * PXMM)
        prof = (a[cy - int(15 * PXMM):cy + int(15 * PXMM), x - 4:x + 5] < thr).sum(axis=1)
        r = runs(prof >= 2, lo=2)
        print(f'{name}: segments (len mm) {[round(n / PXMM, 1) for y, n in r]}; gaps mm {[round((r[i + 1][0] - r[i][0] - r[i][1]) / PXMM, 1) for i in range(len(r) - 1)]}')


if __name__ == '__main__':
    main()
