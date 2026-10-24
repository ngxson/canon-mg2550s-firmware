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
args = parser.parse_args()
total = args.rows + args.probe
import usb.util
dev, _ = wait_device('application')
data = bytearray()
try:
    for chunk in range((total * 0x1440 + 255) // 256):
        data += bytes(dev.ctrl_transfer(0xc0, 0x5d, chunk & 0xffff, chunk >> 16, 256, timeout=2000))
finally:
    usb.util.dispose_resources(dev)
args.out.write_bytes(bytes(data[:total * 0x1440]))
print(len(data), 'bytes')
