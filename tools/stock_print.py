#!/usr/bin/env python3
"""Generate or print an MG2500 stock raster job; --send explicitly prints it."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
import numpy as np
from stockusb.raster import Page,prepare_image,job_stream
from stockusb.printing import print_page
from stockusb.transport import Trace,USB


def main():
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--image',type=Path,help='PNG/JPEG/etc.; fit and center inside printable area')
    source.add_argument('--test-page',action='store_true',help='Small colour bars and label on an otherwise white page')
    p.add_argument('--dpi',type=int,choices=(300,600),default=300)
    p.add_argument('--paper',choices=('A4','Letter'),default='A4')
    p.add_argument('--output',type=Path,help='Save generated .prn to a new file')
    p.add_argument('--send',action='store_true',help='Print one physical page over USB')
    p.add_argument('--trace',type=Path,help='New JSONL metadata file; excludes raster/document bytes')
    p.add_argument('--finish-timeout',type=float,default=180,help='Seconds to wait for the EndJob acknowledgement')
    p.add_argument('--stall-timeout',type=float,default=900,
                   help='Seconds the printer may refuse data while printing or waiting for paper (ESP32: unbounded)')
    args=p.parse_args()
    if not args.send and not args.output: p.error('Choose --output for offline generation, or --send to print')
    if not 0<args.finish_timeout<=3600: p.error('finish-timeout must be in (0, 3600] seconds')
    if not 0<args.stall_timeout<=3600: p.error('stall-timeout must be in (0, 3600] seconds')
    page=Page(args.dpi,args.paper)
    trace=Trace(args.trace)
    try:
        image=prepare_image(page,args.image)
        pixels=np.asarray(image)
        if args.output:
            digest=hashlib.sha256();n=0
            # An explicitly requested .prn contains the document raster.
            with args.output.open('xb') as f:
                try:
                    for part in job_stream(page,pixels): f.write(part);digest.update(part);n+=len(part)
                except BaseException:
                    f.close();args.output.unlink();raise
            print(json.dumps({'output':str(args.output),'bytes':n,'sha256':digest.hexdigest(),'sent':False}))
        if args.send:
            def stall(seconds,status):
                note='' if status is None else f', port status 0x{status:02x}'+(' (PAPER EMPTY?)' if status&0x20 else '')
                print(f'printer not accepting data for {seconds:.0f}s{note}',file=sys.stderr,flush=True)
            def status(fields):
                print('printer status: '+' '.join(f'{k}={v}' for k,v in fields.items()),file=sys.stderr,flush=True)
            with USB('print',trace) as io:
                result=print_page(io,page,pixels,trace,finish_timeout=args.finish_timeout,
                                  stall_timeout=args.stall_timeout,on_stall=stall,on_status=status)
            print(json.dumps(result))
            if not result['end_job_acknowledged']:
                raise SystemExit('Data sent, but EndJob was not acknowledged; physical completion is unverified')
    finally:
        trace.close()


if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: raise SystemExit('Print canceled; printer-class cleanup attempted')
