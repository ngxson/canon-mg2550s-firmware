#!/usr/bin/env python3
"""Read the application's raw/extended clock while idle; never command a motor."""
import argparse
import json
import struct
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import get_info, wait_device


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    import usb.util
    dev, before = wait_device('application')
    samples = []
    started = time.monotonic()
    try:
        while time.monotonic() - started < 12:
            raw = bytes(dev.ctrl_transfer(0xc0, 0x75, 0, 0, 48, timeout=1000))
            words = struct.unpack('<12I', raw)
            assert words[:2] == (0x4d48474d, 1), words
            assert words[3] == 0, 'Clock measurement requires an idle motor'
            samples.append(dict(host_s=time.monotonic() - started,
                                raw=words[8], extended=words[9],
                                limit=words[10], control=words[11]))
            time.sleep(.05)
        after = get_info(dev)
    finally:
        usb.util.dispose_resources(dev)
    wraps = []
    errors = []
    ticks = 0
    for a, b in zip(samples, samples[1:]):
        delta = (b['extended'] - a['extended']) & 0xffffffff
        ticks += delta
        if b['raw'] < a['raw']:
            wraps.append(dict(before=a, after=b))
        errors.append(abs(delta - ((b['raw'] - a['raw']) & 0x1fffffff)))
    elapsed = samples[-1]['host_s'] - samples[0]['host_s']
    rate = ticks / elapsed
    passed = bool(wraps) and 95e6 < rate < 97e6 and max(errors) < 96000
    report = dict(before=before, after=after, samples=samples, raw_wraps=wraps,
                  extended_ticks_per_second=rate, max_delta_disagreement=max(errors),
                  assertions_passed=passed, motor_command_sent=False)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k not in ('samples', 'before', 'after')}, indent=2))
    assert passed, 'Clock rate, rollover, or extension disagrees with the expected clock'


if __name__ == '__main__':
    main()
