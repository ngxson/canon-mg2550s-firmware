#!/usr/bin/env python3
"""Save the raw line buffer of the last continuous scan (vendor 0x5d).

Buffer rows are 0x1440 bytes; with the probe experiment on (vendor 0x7d), the
first --probe rows are the stationary probe lines and scan line k is row k + P.
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from fwctl import wait_device

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--rows', type=int, required=True)
parser.add_argument('--out', type=Path, required=True)
parser.add_argument('--probe', type=int, default=0, help='stationary probe rows before scan line 0')
parser.add_argument('--row-list', default='', help='comma-separated buffer rows to read instead of 0..rows-1')
args = parser.parse_args()
total = args.rows + args.probe
import usb.util
dev, _ = wait_device('application')
data = bytearray()
rows = [int(r) for r in args.row_list.split(',') if r] or list(range(total))
try:
    for r in rows:
        for chunk in range(r * 0x1440 // 256, (r + 1) * 0x1440 // 256):
            data += bytes(dev.ctrl_transfer(0xc0, 0x5d, chunk & 0xffff, chunk >> 16, 256, timeout=2000))
finally:
    usb.util.dispose_resources(dev)
args.out.write_bytes(bytes(data))
import struct
for i, r in enumerate(rows):
    s = struct.unpack_from('<2592H', data, i * 0x1440)
    sent = sum(1 for x in s if x == 0xa5a5); zero = sum(1 for x in s if x == 0)
    print('row', r, 'sentinel', sent, 'zero', zero, 'mean', sum(s) // len(s), 'bands', [sum(s[b*324:(b+1)*324]) // 324 for b in range(8)])
print(len(data), 'bytes')
