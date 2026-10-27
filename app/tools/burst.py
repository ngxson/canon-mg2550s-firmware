#!/usr/bin/env python3
"""Diagnostic N-line burst capture with one scanner setup (motor off).

Optionally parks the carriage first (home, then a forward offset), runs one
burst with the given exposure (eighths of the P0 window widths) and AFE gain,
then saves the firmware log, the raw rows (N plus the reserved row) and
statistics, including row-to-row differences that show whether rows are fresh.
"""
import argparse
import json
import struct
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device

ROW, SEG = 0x1440, 0xa20


def drain(dev):
    out = b''
    for _ in range(800):
        chunk = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=2000))
        if not chunk:
            break
        out += chunk
    return out.decode('latin1')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--n', type=int, default=4)
    parser.add_argument('--exposure', type=int, default=8, help='eighths of the P0 windows, 1..8')
    parser.add_argument('--gain', type=int, default=13, help='AFE R2/R5 gain code, 1..13')
    parser.add_argument('--park', type=int, help='first move: offset in units of 256 counts')
    parser.add_argument('--skip', type=int, default=0, help='line periods before the first request (multiple of 8)')
    parser.add_argument('--offset-step', type=int, default=0, help='AFE R1/R4 = 0xff - 0x20*step (8 -> 0)')
    parser.add_argument('--components', type=int, default=1, choices=(1, 3), help='3 = stock colour configuration (three illumination phases per line)')
    parser.add_argument('--repeat', type=int, default=1, help='LDMAC repeat count (+0x28 high16), 1..4')
    parser.add_argument('--period', type=lambda v: int(v, 0), default=0, help='sub-period T (S+0x138 = T-1); 0 = P0 default 0x1a748')
    parser.add_argument('--immediate', action='store_true', help='first request right after arming instead of on the second IRQ19 event')
    parser.add_argument('--no-ack158', action='store_true', help='do not acknowledge S+0x158 bit 0 per event')
    parser.add_argument('--full-arm', action='store_true', help='arm with the complete stock table, period written after (as the page capture)')
    parser.add_argument('--window', type=int, default=0, help='LDMAC window rows (0 = stock N+1 / 3N+1); smaller than the transfer count tests the ring wrap')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    import usb.util
    dev, _ = wait_device('application')
    report = {'n': args.n, 'exposure': args.exposure, 'gain': args.gain, 'park': args.park, 'skip': args.skip,
              'offset_step': args.offset_step, 'components': args.components, 'repeat': args.repeat, 'period': args.period}
    rows_total = args.window or (3 * args.n + 1 if args.components == 3 else args.n + 1)
    try:
        drain(dev)
        if args.park:
            dev.ctrl_transfer(0x40, 0x7b, args.park, 0, b'', timeout=2000)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x78, 0, 0, 64, timeout=2000)))
                if status[2] in (7, 8, 9):
                    break
                time.sleep(0.2)
            report['park_state'], report['park_error'] = status[2], status[3]
            assert status[2] == 7, f'park ended in state {status[2]} error {status[3]}'
        dev.ctrl_transfer(0x40, 0x5e, args.components | args.repeat << 4 | args.window << 8 | (0x8000 if args.immediate else 0) | (0x4000 if args.no_ack158 else 0) | (0x2000 if args.full_arm else 0), args.period >> 4, b'', timeout=2000)
        dev.ctrl_transfer(0x40, 0x5b, args.n | args.exposure << 8 | args.offset_step << 12,
                          args.gain | (args.skip // 8) << 8, b'', timeout=2000)
        time.sleep(2.5 + args.skip * 0.0025 + 0.2 * args.repeat)
        log = drain(dev)
        rows = bytearray()
        for chunk in range((rows_total * ROW + 255) // 256):
            rows += bytes(dev.ctrl_transfer(0xc0, 0x5c, chunk, 0, 256, timeout=2000))
    finally:
        usb.util.dispose_resources(dev)
    rows = bytes(rows[:rows_total * ROW])
    args.out.with_suffix('.log').write_text(log)
    args.out.with_suffix('.bin').write_bytes(rows)
    report['log'] = [line for line in log.splitlines() if line.startswith('[burst]')]
    samples = [struct.unpack_from('<2592H', rows, k * ROW) for k in range(rows_total)]
    report['row_mean'] = [round(sum(s) / len(s)) for s in samples]
    report['row_clipped'] = [sum(1 for x in s if x == 0xffff) for s in samples]
    report['row_sentinel'] = [sum(1 for x in s if x == 0xa5a5) for s in samples]
    report['adjacent_mean_abs_diff'] = [
        round(sum(abs(a - b) for a, b in zip(samples[k], samples[k + 1])) / 2592, 1) for k in range(rows_total - 1)]
    # per-row sample means in 8 bands across the line: a coloured target shows the component order
    report['row_band_means'] = [[round(sum(s[b * 324:(b + 1) * 324]) / 324) for b in range(8)] for s in samples]
    args.out.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=1))


if __name__ == '__main__':
    main()
