#!/usr/bin/env python3
"""Home, capture a page with measured forward steps, save it, and return home.

Writes original little-endian 16-bit samples (.bin), unscaled 16-bit PGM,
contrast-stretched PNG previews, per-row positions and status (.json), and the
device log. Horizontal optical geometry/shading are not calibrated. The aspect
preview assumes a 216mm sensor span; raw images retain every captured sample.
"""
import argparse
import json
import struct
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import get_info, wait_device

FIELDS = ('magic', 'version', 'state', 'error', 'row', 'rows', 'width', 'row_bytes',
          'pitch', 'origin', 'position', 'capture_start', 'capture_end',
          'chunks_read', 'elapsed_ms', 'variant')
STATES = ('idle', 'homing', 'settling', 'capturing', 'row_ready', 'moving',
          'returning_home', 'done', 'failed', 'cancelled')


def save_images(prefix, count, width, pitch):
    if not count:
        return {}
    import numpy as np
    from PIL import Image
    pixels = np.fromfile(str(prefix) + '.bin', dtype='<u2').reshape(count, width)
    with open(str(prefix) + '.pgm', 'wb') as f:
        f.write(f'P5\n{width} {count}\n65535\n'.encode())
        f.write(pixels.astype('>u2').tobytes())
    low, high = np.percentile(pixels, [.5, 99.5])
    preview = np.clip((pixels.astype(np.float32) - low) * (255 / max(1, high - low)), 0, 255).astype('uint8')
    image = Image.fromarray(preview)
    image.save(str(prefix) + '-raw.png')
    aspect_width = round(216 / 25.4 * 38400 / pitch)
    image.resize((aspect_width, count), Image.Resampling.LANCZOS).save(str(prefix) + '.png')
    return dict(min=int(pixels.min()), max=int(pixels.max()), mean=float(pixels.mean()),
                preview_low=float(low), preview_high=float(high),
                raw_width=width, rows=count, preview_width=aspect_width,
                preview_assumed_width_mm=216, nominal_y_dpi=38400 / pitch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='output filename prefix')
    parser.add_argument('--rows', type=int, default=880)
    parser.add_argument('--pitch', type=int, default=512, help='nominal 38400 counts/inch; default 75 rows/inch')
    parser.add_argument('--serial')
    args = parser.parse_args()
    if not (1 <= args.rows <= 880 and 256 <= args.pitch <= 1024 and
            (args.rows - 1) * args.pitch <= 450048):
        parser.error('invalid page geometry (maximum travel 450048 counts)')
    # Resolve dependencies before commanding hardware.
    import numpy
    from PIL import Image
    import usb.util
    args.out.parent.mkdir(parents=True, exist_ok=True)
    raw_path = Path(str(args.out) + '.bin')
    if raw_path.exists():
        parser.error(f'{raw_path} already exists; choose a new output prefix')
    dev, info = wait_device('application', serial_number=args.serial)
    began = time.monotonic()
    report = dict(requested_rows=args.rows, pitch=args.pitch, before=info,
                  rows=[], states=[], completed=False,
                  optical_calibration=False, source='physical USB capture')
    started = False
    width = 3072

    def status():
        data = bytes(dev.ctrl_transfer(0xc0, 0x78, 0, 0, 64, timeout=2000))
        s = dict(zip(FIELDS, struct.unpack('<16I', data)))
        if (s['magic'], s['version']) != (0x4750534d, 1):
            raise RuntimeError(f'Unexpected page protocol: {s}')
        return s

    def checkpoint():
        Path(str(args.out) + '.json').write_text(json.dumps(report, indent=2) + '\n')

    try:
        with raw_path.open('xb') as raw, open(str(args.out) + '.log', 'wb') as log:
            dev.ctrl_transfer(0x40, 0x77, args.rows, args.pitch, b'', timeout=2000)
            started = True
            last_state = None
            while True:
                s = status()
                report['final_status'] = s
                if s['state'] != last_state:
                    if s['state'] in (1, 6, 7, 8, 9):
                        print(STATES[s['state']], 'error', s['error'], flush=True)
                    report['states'].append(dict(state=s['state'], row=s['row'], host_s=time.monotonic() - began))
                    last_state = s['state']
                for _ in range(4):
                    data = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=2000))
                    if not data: break
                    log.write(data)
                if s['state'] == 4:
                    number = len(report['rows'])
                    if s['row'] != number or s['row_bytes'] != 6144 or s['width'] != width:
                        raise RuntimeError(f'Unexpected row/geometry: {s}')
                    line = b''.join(bytes(dev.ctrl_transfer(0xc0, 0x79, chunk, number, 256, timeout=2000))
                                    for chunk in range(s['row_bytes'] // 256))
                    after = status()
                    if len(line) != s['row_bytes'] or after['state'] != 4 or after['row'] != number:
                        raise RuntimeError('Incomplete or changing row; leaving partial output marked incomplete')
                    raw.write(line); raw.flush()
                    report['rows'].append(dict(index=number, start=s['capture_start'], end=s['capture_end'],
                                               elapsed_ms=s['elapsed_ms']))
                    dev.ctrl_transfer(0x40, 0x7a, number, 0, b'', timeout=2000)
                    if number % 32 == 0:
                        print(f'{number + 1}/{args.rows} rows, position {s["capture_start"] - s["origin"]}, '
                              f'{time.monotonic() - began:.1f}s', flush=True)
                        checkpoint()
                elif s['state'] in (7, 8, 9):
                    report['completed'] = s['state'] == 7 and len(report['rows']) == args.rows
                    if not report['completed']:
                        raise RuntimeError(f'Firmware stopped: {STATES[s["state"]]}, error {s["error"]}')
                    break
                if time.monotonic() - began > 960:
                    raise TimeoutError('Host page deadline')
                time.sleep(.01)
            report['management_after'] = get_info(dev)
    except BaseException as exc:
        report['exception'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if started:
            try:
                dev.ctrl_transfer(0x40, 0x76, 0, 0, b'', timeout=2000)
            except Exception as exc:
                report['stop_error'] = str(exc)
        usb.util.dispose_resources(dev)
        report['host_elapsed_s'] = time.monotonic() - began
        report['image'] = save_images(args.out, len(report['rows']), width, args.pitch)
        checkpoint()
        print(json.dumps(dict(completed=report['completed'], rows=len(report['rows']),
                              output=str(args.out), image=report['image'])), flush=True)


if __name__ == '__main__':
    main()
