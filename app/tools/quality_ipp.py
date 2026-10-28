#!/usr/bin/env python3
"""Short labelled print-quality page through normal IPP (no direct raster writes).

Solid K/C/M/Y bars exercise both carriage segments and a band boundary. With
the sweep planner the left and right groups print as two cropped sweeps per
pass; the centre cyan bar (rows 150..195, input x 1150..1330) straddles the
planner's window boundary at output column 2176 so a sweep seam falls inside
solid ink and across a one-pixel line. Run without --print to create the input
image and URF for review.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import ipp_print as ip


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--label', default='QI' + time.strftime('%d%H%M', time.gmtime()))
    ap.add_argument('--print', dest='do_print', action='store_true')
    ap.add_argument('--sweeps', type=int, default=12, help='expected sweep count (3 bands x 2 segments x 2 passes)')
    ap.add_argument('--top', type=int, default=0, help='blank input rows above the pattern (300 dpi): reuse a sheet lower down; empty passes are skipped')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    width, height = 2480, 200
    page = Image.new('RGB', (width, height), 'white')
    d = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=18)
    for left in (220, 1450):
        d.text((left, 43), a.label + '  K C M Y', font=font, fill='black')
        for i, colour in enumerate(('black', 'cyan', 'magenta', 'yellow')):
            x = left + i * 120
            d.rectangle((x, 78, x + 39, 195), fill=colour)
            d.line((x + 50, 78, x + 50, 195), fill=colour, width=1)
            # A sloping sparse line makes a repeated/missing row apparent.
            d.line((x + 60, 78, x + 100, 195), fill=colour, width=1)
    d.rectangle((1150, 150, 1329, 195), fill='cyan')          # across the sweep seam (output column 2176 = input x 1245)
    d.line((1245, 100, 1245, 148), fill='cyan', width=1)      # one-pixel line on the seam column
    if a.top:                                                  # pattern lower on the sheet; the rows above are blank
        shifted = Image.new('RGB', (width, height + a.top), 'white'); shifted.paste(page, (0, a.top)); page = shifted; height += a.top
    page.save(a.out / f'{a.label}_input.png')
    raw = page.tobytes()
    rows = [raw[y * width * 3:(y + 1) * width * 3] for y in range(height)]
    document = ip.urf_document([(width, height, 3, rows)])
    (a.out / f'{a.label}.urf').write_bytes(document)
    print(a.label, 'URF bytes', len(document), flush=True)
    if not a.do_print:
        return

    import escl_usb
    import usb.util
    from fwctl import wait_device
    dev, _ = wait_device('application')
    conn = None
    record = {'label': a.label, 'width': width, 'height': height,
              'document_bytes': len(document), 'status': []}
    logs = []
    started = time.monotonic()
    try:
        status = ip.engine_status(dev)
        assert status['state'] not in ('receiving', 'printing'), status
        assert not status['dry'], status
        conn = ip.claim_ipp(dev, escl_usb)
        logs.append(ip.drain_log(dev))
        http, _, body = conn.request('POST', '/ipp/print',
                                    ip.print_job_request(document, a.label),
                                    'application/ipp', timeout=600000)
        ipp, attrs = ip.ipp_parse(body)
        record['http'], record['ipp'] = http, ipp
        print('HTTP', http, 'IPP', ipp, 'job', attrs.get('job-id'), flush=True)
        assert http == 200 and ipp == 0
        while time.monotonic() - started < 600:
            status = ip.engine_status(dev)
            record['status'].append(status)
            logs.append(ip.drain_log(dev))
            print(status, flush=True)
            if status['state'] in ('completed', 'failed', 'canceled'):
                break
            time.sleep(2)
        assert status['state'] == 'completed' and status['pages'] == 1, status
        assert status['failures'] == 0 and (a.top or status['sweeps'] == a.sweeps), status   # --top shifts band alignment; the count then varies
        record['completed'] = True
        record['profile'] = ip.engine_profile(dev)
        print('profile:', ip.format_profile(record['profile']), flush=True)
    finally:
        record['seconds'] = round(time.monotonic() - started, 2)
        (a.out / f'{a.label}_ipp.json').write_text(json.dumps(record, indent=2) + '\n')
        (a.out / f'{a.label}_firmware.log').write_text(''.join(logs))
        if conn is not None:
            usb.util.release_interface(dev, conn.number)
        usb.util.dispose_resources(dev)
    print('Printed and ejected', a.label, 'in', record['seconds'], 'seconds', flush=True)


if __name__ == '__main__':
    main()
