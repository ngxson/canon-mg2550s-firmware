#!/usr/bin/env python3
"""Short labelled print-quality page through normal IPP (no direct raster writes).

Solid K/C/M/Y bars exercise both carriage segments and a band boundary.
Run without --print to create the input image and URF for review.
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
        intf = next(i for i in dev.get_active_configuration()
                    if (i.bInterfaceClass, i.bInterfaceSubClass, i.bInterfaceProtocol) == (7, 1, 4))
        conn = escl_usb.Connection(dev, intf)
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
        assert status['sweeps'] == 12 and status['failures'] == 0, status
        record['completed'] = True
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
