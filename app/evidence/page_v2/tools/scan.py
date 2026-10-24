#!/usr/bin/env python3
"""Capture scanner lines from the open MG2550s app and save them as an image.

Runs the app's stationary capture (USB vendor request 0x59) and reads back the
DMA buffer (0x5a), one 3072-sample line per capture. Repeats --lines times and
stacks them into a PGM so you can see it. Motor is never moved — this is the
park-position line only until carriage motion is added.

Examples:
  python3 scan.py                       # AFE+lamp, 1 line
  python3 scan.py --lines 64 --out scan # stack 64 captures -> scan.pgm + scan.bin
  python3 scan.py --variant 0x0e        # AFE+lamp+exposure boost
"""
import argparse
import struct
import sys
import time

VID, APP_PID = 0x04a9, 0x12fe
LINE_SAMPLES = 3072           # ch0 segment (1536) + ch1 segment (1536)
LINE_BYTES = LINE_SAMPLES * 2


def find(serial=None):
    import usb.core
    for _ in range(40):
        dev = usb.core.find(idVendor=VID, idProduct=APP_PID)
        if dev and (serial is None or dev.serial_number == serial):
            return dev
        time.sleep(0.3)
    return None


def capture_line(dev, variant):
    import usb.core
    dev.ctrl_transfer(0x40, 0x59, variant, 0, b'', timeout=5000)   # run capture
    time.sleep(0.2)
    raw = bytearray()
    chunk = 0
    while len(raw) < LINE_BYTES:
        d = dev.ctrl_transfer(0xC0, 0x5a, chunk, 0, 256, timeout=2000)
        if not d:
            break
        raw += bytes(d)
        chunk += 1
    raw = bytes(raw[:LINE_BYTES])
    return struct.unpack('<%dH' % (len(raw) // 2), raw)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--variant', default='0x06',
                    help='capture variant (bits: 0x02 AFE, 0x04 lamp, 0x08 expo, 0x01 clock)')
    ap.add_argument('--lines', type=int, default=1, help='number of captures to stack')
    ap.add_argument('--out', default='scan', help='output file prefix')
    ap.add_argument('--serial')
    args = ap.parse_args()
    variant = int(args.variant, 0)
    import usb.util

    dev = find(args.serial)
    if dev is None:
        print('MG2550s app (04a9:12fe) not found', file=sys.stderr)
        return 1
    try:
        rows = [capture_line(dev, variant) for _ in range(args.lines)]
    finally:
        usb.util.dispose_resources(dev)

    flat = [v for row in rows for v in row]
    lo, hi = min(flat), max(flat)
    mean = sum(flat) // len(flat)
    print('variant=0x%02x  lines=%d  min=0x%04x max=0x%04x mean=0x%04x' %
          (variant, args.lines, lo, hi, mean))

    # raw 16-bit
    with open(args.out + '.bin', 'wb') as f:
        for row in rows:
            f.write(struct.pack('<%dH' % len(row), *row))
    # 8-bit PGM, contrast-stretched
    rng = max(1, hi - lo)
    with open(args.out + '.pgm', 'wb') as f:
        f.write(('P5\n%d %d\n255\n' % (LINE_SAMPLES, args.lines)).encode())
        for row in rows:
            f.write(bytes((v - lo) * 255 // rng for v in row))
    print('wrote %s.bin (%d-bit) and %s.pgm (%dx%d)' %
          (args.out, 16, args.out, LINE_SAMPLES, args.lines))

    # ascii preview of the first line across the sensor
    ramp = ' .:-=+*#%@'
    step = LINE_SAMPLES // 72
    prev = ''.join(ramp[min(9, (sum(rows[0][i*step:(i+1)*step]) // step - lo) * 10 // rng)]
                   for i in range(72))
    print('line[0] dark->light:\n' + prev)
    return 0


if __name__ == '__main__':
    sys.exit(main())
