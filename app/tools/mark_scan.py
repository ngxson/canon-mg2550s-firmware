#!/usr/bin/env python3
"""Scan a printed sheet back through the open app and locate the ink marks.

  mark_scan.py --out scans/marks1               # scan the glass (75 dpi grey via eSCL), detect marks
  mark_scan.py --image scans/marks1.jpg          # detect marks in an existing scan or photo
  mark_scan.py --out scans/marks2 --reference scans/marks1.json   # also report the shift versus a previous scan

Print-then-scan loop for head debugging, in the spirit of the stock head
alignment: fire or print something, eject, put the sheet face down on the
glass against the corner mark, run this. It drives escl_usb.py for the scan,
thresholds dark pixels against the local background, labels connected blobs
and reports each mark's centre, size and bounding box in millimetres from the
glass corner (75 dpi: 0.3387 mm per pixel), plus an annotated PNG and a JSON
report. With --reference it pairs the marks with those of an earlier report
and prints the offsets, which is the alignment measurement. Uncalibrated
scanner: positions are relative to the glass corner, good to a millimetre.
"""
import argparse, json, subprocess, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

TOOLS = Path(__file__).resolve().parent
MM_PER_PX = {75: 25.4 / 75, 150: 25.4 / 150, 300: 25.4 / 300}


def scan(out_prefix, resolution):
    out = out_prefix.with_suffix('.png')
    out.parent.mkdir(parents=True, exist_ok=True)
    # macOS ippusbd may hold one of the two IPP-USB interfaces after a power
    # cycle (access denied); try the other one before giving up.
    for interface in (0, 1):
        cmd = [sys.executable, str(TOOLS / 'escl_usb.py'), '--mode', 'Grayscale8', '--resolution', str(resolution),
               '--interface', str(interface), '--out', str(out)]
        print('scanning:', ' '.join(cmd), flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and out.exists():
            return out
        print('  interface %d failed: %s' % (interface, (r.stderr.strip().splitlines() or ['?'])[-1][:120]), flush=True)
    raise SystemExit('scan failed on both IPP-USB interfaces')


def detect(image_path, resolution, min_area_mm2=0.3, darkness=0.55, border_mm=3.0, paper_level=110.0, max_extent_mm=30.0):
    img = np.asarray(Image.open(image_path).convert('L'), dtype=np.float32)
    h, w = img.shape
    mm = MM_PER_PX.get(resolution, 25.4 / resolution)
    # Local background: a wide median filter approximates the paper white, so shading
    # and lamp fall-off do not count as marks.
    background = ndimage.median_filter(img, size=(1, 65))
    background = np.maximum(background, 1.0)
    # A mark is a dark pixel on bright paper; with no sheet on the glass the
    # scan is dark everywhere and nothing qualifies.
    paper = background > paper_level
    dark = (img < darkness * background) & paper
    b = int(round(border_mm / mm))
    dark[:b, :] = dark[-b:, :] = False; dark[:, :b] = dark[:, -b:] = False   # glass edge shadows
    labels, n = ndimage.label(dark)
    marks = []
    min_px = max(1, int(round(min_area_mm2 / (mm * mm))))
    for i, sl in enumerate(ndimage.find_objects(labels), 1):
        blob = labels[sl] == i
        area = int(blob.sum())
        if area < min_px: continue
        ys, xs = np.nonzero(blob)
        cy, cx = ys.mean() + sl[0].start, xs.mean() + sl[1].start
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        if (y1 - y0) * mm > max_extent_mm or (x1 - x0) * mm > max_extent_mm: continue   # sheet edge / shadow strips
        if min(y1 - y0, x1 - x0) < 2: continue                                            # one-pixel lines: sheet edges
        marks.append(dict(x_mm=round(cx * mm, 2), y_mm=round(cy * mm, 2), w_mm=round((x1 - x0) * mm, 2),
                          h_mm=round((y1 - y0) * mm, 2), area_mm2=round(area * mm * mm, 2),
                          bbox_px=[int(x0), int(y0), int(x1), int(y1)],
                          mean_darkness=round(float(1 - (img[sl][blob] / background[sl][blob]).mean()), 3)))
    marks.sort(key=lambda m: (m['y_mm'], m['x_mm']))
    return img, marks, mm, float(paper.mean())


def annotate(img, marks, mm, out_png):
    im = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).convert('RGB')
    d = ImageDraw.Draw(im)
    for k, m in enumerate(marks):
        x0, y0, x1, y1 = m['bbox_px']
        d.rectangle([x0 - 4, y0 - 4, x1 + 4, y1 + 4], outline=(255, 0, 0), width=2)
        d.text((x1 + 6, y0 - 4), f"{k}: {m['x_mm']},{m['y_mm']}mm", fill=(255, 0, 0))
    im.save(out_png)


def pair(marks, reference):
    out = []
    for m in marks:
        best = min(reference, key=lambda r: (r['x_mm'] - m['x_mm']) ** 2 + (r['y_mm'] - m['y_mm']) ** 2, default=None)
        if best is None: continue
        out.append(dict(mark=m, reference=best, dx_mm=round(m['x_mm'] - best['x_mm'], 2), dy_mm=round(m['y_mm'] - best['y_mm'], 2)))
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out', type=Path, help='output prefix; scans the glass unless --image is given')
    p.add_argument('--image', type=Path, help='analyse this image instead of scanning')
    p.add_argument('--resolution', type=int, default=75, help='scan dpi (also the dpi assumed for --image)')
    p.add_argument('--darkness', type=float, default=0.55, help='pixel counts as ink below this fraction of the local background')
    p.add_argument('--min-area', type=float, default=0.3, help='smallest mark to report, mm^2')
    p.add_argument('--reference', type=Path, help='earlier report JSON to compare against')
    p.add_argument('--paper-level', type=float, default=110.0, help='local background must exceed this grey level (0..255) to count as paper')
    args = p.parse_args()
    if not args.image and not args.out: p.error('--out or --image required')
    image = args.image or scan(args.out, args.resolution)
    img, marks, mm, paper_fraction = detect(image, args.resolution, args.min_area, args.darkness, paper_level=args.paper_level)
    prefix = args.out or image.with_suffix('')
    annotate(img, marks, mm, prefix.with_name(prefix.name + '-marks.png'))
    report = dict(image=str(image), resolution=args.resolution, mm_per_px=mm, image_mm=[round(img.shape[1] * mm, 1), round(img.shape[0] * mm, 1)],
                  paper_fraction=round(paper_fraction, 3), marks=marks)
    if args.reference:
        ref = json.loads(args.reference.read_text())['marks']
        report['versus_reference'] = pair(marks, ref)
    prefix.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f"{len(marks)} mark(s) in {report['image_mm'][0]} x {report['image_mm'][1]} mm; paper covers {paper_fraction:.0%} of the scan")
    if paper_fraction < 0.2: print('  little or no paper detected: is the sheet on the glass, face down?')
    for k, m in enumerate(marks):
        print(f"  {k}: centre x={m['x_mm']} y={m['y_mm']} mm  size {m['w_mm']} x {m['h_mm']} mm  area {m['area_mm2']} mm2  darkness {m['mean_darkness']}")
    for v in report.get('versus_reference', []):
        print(f"  shift vs reference: dx={v['dx_mm']} dy={v['dy_mm']} mm")
    print('annotated:', prefix.with_name(prefix.name + '-marks.png'), ' report:', prefix.with_suffix('.json'))


if __name__ == '__main__':
    main()
