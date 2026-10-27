#!/usr/bin/env python3
"""Measure colour bar heights per sweep on a colour_variants.py scan (150 dpi grayscale):
finds the black 160/304-row reference bar (band x = 38 mm), then lists the C/M/Y bar runs
(y mm, height mm, rows).   colour_bars_check.py scan.png [y0_px] [y1_px]"""
import sys, numpy as np
from PIL import Image
a = np.asarray(Image.open(sys.argv[1]).convert('L')).astype(float); pxmm = 150 / 25.4
y0, y1 = int(sys.argv[2]) if len(sys.argv) > 2 else 100, int(sys.argv[3]) if len(sys.argv) > 3 else a.shape[0] - 60
# black reference bars: columns with long dark runs
cols = (a[y0:y1, :] < 200).sum(axis=0); xs = [x for x in range(60, a.shape[1] - 60) if cols[x] > 25]
g = []
for x in xs:
    if g and x - g[-1][-1] <= 2: g[-1].append(x)
    else: g.append([x])
print('black bar columns:', [(gg[0], gg[-1]) for gg in g])
def runs_in_col(x0, x1, thr):
    prof = (a[y0:y1, x0:x1] < thr).sum(axis=1)
    r = [y for y in range(len(prof)) if prof[y] >= 0.5 * (x1 - x0)]; out = []
    for y in r:
        if out and y - out[-1][-1] <= 2: out[-1].append(y)
        else: out.append([y])
    return [(round((rr[0] + y0) / pxmm, 1), round(len(rr) / pxmm, 2), round(len(rr) / pxmm * 600 / 25.4)) for rr in out if len(rr) >= 3]
cand = [gg for gg in g if 8 <= gg[-1] - gg[0] + 1 <= 16]
if cand:
    best = max(cand, key=lambda gg: cols[gg[0]:gg[-1] + 1].sum())
    xb = best[0]                       # black reference bar at band x = 38 mm -> band left = xb - 38 mm
    xl = xb - int(38 * pxmm)
    print('black bar at x', best, 'runs (y mm, height mm, rows):', runs_in_col(xb, best[-1] + 1, 200))
    for name, mmx, thr in (('C', 12, 238), ('M', 20, 215), ('Y', 28, 240)):
        x0 = xl + int(mmx * pxmm) + 1; x1 = x0 + int(48 / 600 * 25.4 * pxmm) - 2
        print(f'{name} bar runs (y mm, height mm, rows):', runs_in_col(x0, x1, thr))

# 32-row line check per black bar run: count light rows inside each run (the mode-7 defect)
if cand:
    pxmm_ = 150 / 25.4
    prof = a[y0:y1, xb:best[-1] + 1].mean(axis=1)
    mask = (a[y0:y1, xb:best[-1] + 1] < 200).sum(axis=1) >= 0.5 * (best[-1] + 1 - xb)
    idx = np.where(mask)[0]; r = []
    for i in idx:
        if r and i - r[-1][0] - r[-1][1] <= 1: r[-1][1] += 1
        else: r.append([i, 1])
    r = [(s0, n) for s0, n in r if n >= 20]
    for start, n in r:
        seg = prof[start:start + n]; base = np.median(seg)
        light = [i for i in range(1, n - 1) if seg[i] > base + 25 and seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]]
        print(f'black bar at {round((start + y0) / pxmm_, 1)} mm, {round(n / pxmm_, 1)} mm tall: {len(light)} light lines, spacing mm {[round(d / pxmm_, 2) for d in np.diff(light)][:12]}')
