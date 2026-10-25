#!/usr/bin/env python3
"""Scan stock MG2500 directly through USB, without macOS scanner drivers."""
import argparse
import json
import math
import os
from pathlib import Path
import tempfile
from stockusb.scan import Params,Scanner
from stockusb.transport import Trace,USB


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True,help='New .png or .ppm file (never overwritten)')
    p.add_argument('--dpi',type=int,choices=(75,150,300,600),default=150)
    for name,default in (('x',0),('y',0),('width',210),('height',297)):
        p.add_argument('--'+name,type=float,default=default,help='Millimetres from platen origin' if name in ('x','y') else 'Millimetres')
    p.add_argument('--no-calibration',action='store_true')
    p.add_argument('--timeout',type=float,default=300,help='Total scan deadline, seconds')
    p.add_argument('--trace',type=Path,help='New JSONL timing/command metadata file; excludes image bytes')
    args=p.parse_args()
    if any(not math.isfinite(v) for v in (args.x,args.y,args.width,args.height,args.timeout)) or args.timeout<=0:
        p.error('Finite coordinates and a positive timeout are required')
    params=Params(args.dpi,*(round(v*args.dpi/25.4) for v in (args.x,args.y,args.width,args.height)),not args.no_calibration)
    try: params.validate()
    except ValueError as e: p.error(str(e))
    out=args.output.resolve()
    if out.suffix.lower() not in ('.png','.ppm'): p.error('Output must be .png or .ppm')
    if out.exists(): p.error('Output already exists')
    if args.trace and args.trace.resolve()==out: p.error('Trace and image output must be different files')
    if out.suffix.lower()=='.png':
        from PIL import Image  # Fail before opening a scan session if absent.
    trace=Trace(args.trace)
    temporary=[]
    try:
        with tempfile.NamedTemporaryFile(dir=out.parent,prefix='.stock-scan-',suffix='.ppm',delete=False) as f:
            temporary.append(Path(f.name))
            f.write(f'P6\n{params.width} {params.height}\n255\n'.encode())
            with USB('scan',trace) as io:
                def row(y,rgb):
                    f.write(rgb)
                    if y%100==0: trace('scan_row',row=y)
                result=Scanner(io,trace=trace,timeout=args.timeout).scan(params,row)
        ready=temporary[0]
        if out.suffix.lower()=='.png':
            with tempfile.NamedTemporaryFile(dir=out.parent,prefix='.stock-scan-',suffix='.png',delete=False) as f:
                temporary.append(Path(f.name))
            with Image.open(ready) as image: image.save(temporary[-1],format='PNG',dpi=(args.dpi,args.dpi))
            ready=temporary[-1]
        # Atomically publish a complete image without replacing another file.
        os.link(ready,out)
        print(json.dumps({**result,'output':str(out)}))
    finally:
        for path in temporary: path.unlink(missing_ok=True)
        trace.close()


if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: raise SystemExit('Scan canceled; session cleanup attempted')
