"""Compare unmodified blank-paper pixels on the same sheet before/after calibration."""
import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('before', type=Path)
ap.add_argument('after', type=Path)
ap.add_argument('--out', type=Path, default=Path(__file__).resolve().parent)
a = ap.parse_args()
a.out.mkdir(parents=True, exist_ok=True)
regions = {'left': (1090, 1140), 'centre': (1175, 1350), 'right': (1410, 1460)}
report = {'rows': [300, 650], 'column_ranges': regions, 'scans': {}}
comparison = Image.new('RGB', (900, 390), 'white')
draw = ImageDraw.Draw(comparison)
font = ImageFont.load_default(size=18)
for i, (label, path) in enumerate((('Before', a.before), ('After', a.after))):
    im = Image.open(path).convert('RGB')
    assert im.size == (2480, 700), im.size
    pixels = np.asarray(im, dtype=float)
    means = {name: pixels[300:650, x0:x1].mean(axis=(0, 1)).tolist()
             for name, (x0, x1) in regions.items()}
    excess = np.mean(means['centre']) - (np.mean(means['left']) + np.mean(means['right'])) / 2
    report['scans'][label.lower()] = {'file': str(path), 'mean_rgb': means,
                                     'centre_minus_flanks': float(excess)}
    draw.text((i * 450 + 12, 8), label, fill='black', font=font)
    # Identical crop, no contrast correction or colour manipulation.
    comparison.paste(im.crop((1050, 260, 1500, 610)), (i * 450, 40))
(a.out / 'scan_comparison.json').write_text(json.dumps(report, indent=2) + '\n')
comparison.save(a.out / 'before_after.png')
print(json.dumps(report, indent=2))
