#!/usr/bin/env python3
"""Count white lines inside solid ink bars on a colour scan (RGB, 300 dpi).

    bar_lines.py scan.png [--dpi 300]

For each ink (K: all channels dark; C: red low; M: green low; Y: blue low) the
image is thresholded, solid regions wider than 3 mm and taller than 6 mm are
located, and inside each region the rows whose ink coverage drops below half
the region's median are counted as lines, with their spacing in 600 dpi rows.
Prints one line per bar: ink, x/y (mm), size (mm), rows at 600 dpi, lines,
spacing. Lines every 32 rows (1.35 mm) are the defect of sheets CT0142/DOC0019."""
import argparse, numpy as np
from PIL import Image
from scipy import ndimage


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('image'); ap.add_argument('--dpi', type=int, default=300); ap.add_argument('--thr', type=int, default=150)
    a = ap.parse_args()
    im = np.asarray(Image.open(a.image).convert('RGB')).astype(int)
    r, g, b = im[..., 0], im[..., 1], im[..., 2]
    pxmm = a.dpi / 25.4
    inks = {'K': (r < a.thr) & (g < a.thr) & (b < a.thr),
            'C': (r < a.thr) & (g > a.thr) & (b > a.thr - 30),
            'M': (g < a.thr) & (r > a.thr) & (b > a.thr - 30),
            'Y': (b < a.thr) & (r > a.thr) & (g > a.thr)}
    for ink, mask in inks.items():
        filled = ndimage.binary_closing(mask, structure=np.ones((9, 3)))      # bridge the thin white lines for region finding
        labels, n = ndimage.label(filled)
        for i, sl in enumerate(ndimage.find_objects(labels), 1):
            h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
            if w < 1 * pxmm or h < 6 * pxmm: continue
            region = mask[sl] & (labels[sl] == i)
            x0, x1 = sl[1].start + w // 5, sl[1].stop - w // 5                 # interior columns
            cov = mask[sl[0], x0:x1].mean(axis=1)
            med = np.median(cov)
            lines = [y for y in range(len(cov)) if cov[y] < 0.5 * med]
            groups = []
            for y in lines:
                if groups and y - groups[-1][-1] <= 1: groups[-1].append(y)
                else: groups.append([y])
            centres = [np.mean(gg) for gg in groups]
            spacing = np.diff(centres) * 600 / a.dpi if len(centres) > 1 else []
            print(f'{ink} bar at x={sl[1].start / pxmm:5.1f} y={sl[0].start / pxmm:5.1f} mm, {w / pxmm:4.1f} x {h / pxmm:4.1f} mm ({round(h * 600 / a.dpi)} rows), '
                  f'coverage {med:.2f}, white lines {len(groups)}' + (f', spacing rows {[round(s) for s in spacing][:12]}' if len(spacing) else ''))


if __name__ == '__main__':
    main()
