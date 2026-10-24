#!/usr/bin/env python3
"""Read the head thermistor ADC for both cartridges (USB 0x80, read-only).

Prints the 16-sample means and the samples. Raw ADC counts; stock converts
them with fuse-derived calibration to half-degrees, which is not reproduced.
"""
import argparse, json, struct, sys
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    import usb.util
    dev, info = wait_device('application')
    try:
        w = struct.unpack('<36I', bytes(dev.ctrl_transfer(0xc0, 0x80, 0, 0, 144, timeout=2000)))
    finally:
        usb.util.dispose_resources(dev)
    assert w[0] == 0x4441484d, [hex(x) for x in w[:2]]
    r = {'adc_init_ok': w[1] == 1, 'black_mean': w[2], 'colour_mean': w[3], 'black_samples': list(w[4:20]), 'colour_samples': list(w[20:36])}
    print(json.dumps(r))
    if args.output: args.output.write_text(json.dumps({'management': info, **r}, indent=2) + '\n')


if __name__ == '__main__':
    main()
