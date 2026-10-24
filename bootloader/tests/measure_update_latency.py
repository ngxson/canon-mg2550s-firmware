#!/usr/bin/env python3
"""Measure how long a USB app upload keeps the bootloader away from usb_poll.

The host (fwctl) polls INFO with a 1 s control-transfer timeout while the
bootloader programs the image. Any single main-loop step that runs longer
than that makes the host abandon a control transfer. Flash timing in this
model is instantaneous, so the metric here is executed ARM instructions
between consecutive usb_poll() entries. With --calibrate, the same count is
taken for a slice of the eSCL scan whose duration was measured on the board,
giving an instructions-per-second figure to convert counts into time.

Usage: measure_update_latency.py [IMAGE.bin ...] [--synthetic BYTES ...]
       [--calibrate] [--max-gap INSTRUCTIONS] [--json OUT]
"""
import argparse
import json
import random
import struct
import sys
import zlib
from pathlib import Path

from unicorn import UC_HOOK_BLOCK, UC_HOOK_CODE

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parents[1]
sys.path.insert(0, str(TESTS))
from model import Model, setup  # noqa: E402

STAY = ('stay', setup(0x40, 0x40))


def synthetic_app(size):
    """A valid MGAP package with a random payload; it is uploaded, never run."""
    payload = random.Random(size).randbytes(size - 32)
    head = struct.pack('<7I', 0x5041474d, 2, len(payload), 0x20020000, 0, zlib.crc32(payload), 0)
    return head + struct.pack('<I', zlib.crc32(head)) + payload


def begin(data):
    return ('begin', setup(0x40, 0x43, len(data) & 65535, len(data) >> 16))


class Counting:
    """Mixin: count instructions and the gaps between hits of one address."""

    def count(self, address):
        self.instructions = 0
        self.last_hit = None
        self.gaps = []
        self.uc.hook_add(UC_HOOK_BLOCK, self._block)
        self.uc.hook_add(UC_HOOK_CODE, self._hit, begin=address, end=address)

    def _block(self, uc, address, size, user):
        self.instructions += size // 4             # ARM state only in these builds

    def _hit(self, uc, address, size, user):
        if self.last_hit is not None:
            self.gaps.append(self.instructions - self.last_hit)
        self.last_hit = self.instructions


class CountingModel(Counting, Model):
    pass


def upload_gaps(boot, image_name, data):
    meta = json.loads((ROOT / 'bootloader/build/manifest.json').read_text())
    factory = boot.ljust(0x200000, b'\xff')
    e = CountingModel(image=factory, metadata=meta, actions=[[STAY, begin(data)]], bulk=data)
    e.count(int(meta['symbols']['bootloader']['usb_poll'], 16))
    e.run(lambda x: bool(x.info()) and x.info()[5] == 0xc0, budget=4_000_000_000)
    worst = max(e.gaps)
    return {'image': image_name, 'bytes': len(data), 'worst_gap_instructions': worst,
            'usb_polls': len(e.gaps), 'status': hex(e.info()[5])}


def calibrate():
    """Instructions per second from the eSCL scan timed on hardware."""
    sys.path.insert(0, str(ROOT / 'app/tests'))
    import validate_escl as v

    class Host(Counting, v.IppUsbHost):
        pass
    factory, _, metadata = v.factory_and_metadata()
    e = Host(image=factory, metadata=metadata)
    e.count(0)                                     # gaps unused; total count only
    e.run(v.app_mode)
    # Hardware: full Letter RGB24 at 75 dpi (876 rows), 53.945 s for NextDocument.
    rows, hardware_rows, hardware_seconds = 109, 876, 53.945
    assert v.create_job(e, 0, v.settings(mode='RGB24', region=(0, 0, 2550, rows * 4)))[0] == 201
    start = e.instructions
    status, _, body, _ = v.http(e, 0, 'GET', '/eSCL/ScanJobs/1/NextDocument')
    assert status == 200
    per_row = (e.instructions - start) / rows
    return {'scan_instructions_per_row': round(per_row),
            'instructions_per_second': round(per_row * hardware_rows / hardware_seconds),
            'basis': 'eSCL RGB24 75 dpi page, 876 rows, 53.945 s on the board'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images', nargs='*', type=Path)
    parser.add_argument('--synthetic', type=int, action='append', default=[],
                        help='also upload a generated app package of this many bytes')
    parser.add_argument('--max-gap', type=int, help='fail if any upload exceeds this many instructions')
    parser.add_argument('--boot', type=Path, default=ROOT / 'bootloader/build/boot_region.bin')
    parser.add_argument('--calibrate', action='store_true')
    parser.add_argument('--json', type=Path)
    args = parser.parse_args()
    boot = args.boot.read_bytes()
    report = {'boot_region': str(args.boot), 'uploads': []}
    if args.calibrate:
        report['calibration'] = calibrate()
        print(json.dumps(report['calibration']), flush=True)
    uploads = [(path.name, path.read_bytes()) for path in args.images]
    uploads += [(f'synthetic_{size}', synthetic_app(size)) for size in args.synthetic]
    for name, data in uploads:
        result = upload_gaps(boot, name, data)
        if args.calibrate:
            ips = report['calibration']['instructions_per_second']
            result['worst_gap_seconds_estimate'] = round(result['worst_gap_instructions'] / ips, 2)
        report['uploads'].append(result)
        print(json.dumps(result), flush=True)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + '\n')
    if args.max_gap is not None:
        worst = max(r['worst_gap_instructions'] for r in report['uploads'])
        assert worst <= args.max_gap, f'worst gap {worst} exceeds {args.max_gap}'
        print(f'PASS every upload kept usb_poll gaps <= {args.max_gap} instructions')


if __name__ == '__main__':
    main()
