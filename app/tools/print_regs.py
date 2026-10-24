#!/usr/bin/env python3
"""Dump or watch the CR/LF print motor registers (read-only, USB 0x64).

With --watch, polls and prints only the registers whose value changed, so the
carriage or feed roller can be moved by hand while the encoder counters are
observed. Nothing is written to the printer.
"""
import argparse, json, struct, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device  # noqa: E402

NAMES = ['CR_enable', 'CR_period', 'CR_command', 'CR_p000', 'CR_p004', 'CR_p008',
         'CR_position', 'CR_head_stage_pos', 'CR_evt_mask', 'CR_evt_status',
         'LF_enable', 'LF_period', 'LF_command', 'LF_p000', 'LF_p004', 'LF_p008',
         'LF_position', 'LF_p108', 'LF_p120', 'LF_p220',
         'LF_cap0', 'LF_cap1', 'LF_cap2', 'LF_cap3',
         'LF_evt_mask', 'LF_evt_status', 'LF_c_evt_mask', 'LF_c_evt_status',
         'SCAN_enable', 'SCAN_position',
         'CH4_enable', 'CH4_period', 'CH4_command', 'CH4_p000', 'CH4_p004', 'CH4_p008', 'CH4_position',
         'CH4_evt_mask', 'CH4_evt_status', 'CH4_c_evt_mask', 'CH4_c_evt_status']


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--watch', action='store_true', help='poll until Ctrl-C, print changes')
    p.add_argument('--interval', type=float, default=0.2)
    p.add_argument('--duration', type=float, default=0, help='stop watching after this many seconds (0 = until Ctrl-C)')
    p.add_argument('--prep', type=int, default=0, help='before dumping, send motor-off counter prep variant bits (1 CR init, 2 routing, 4 LF init, 8 scanner shared setup, 16 fourth-channel init)')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    import usb.util
    dev, info = wait_device('application')
    samples = []
    try:
        def dump():
            raw = bytes(dev.ctrl_transfer(0xc0, 0x64, 0, 0, 256, timeout=1000))
            words = struct.unpack('<%dI' % (len(raw) // 4), raw)
            assert words[:3] == (0x5250474d, 1, len(NAMES)), words
            return dict(zip(NAMES, words[3:]))
        if args.prep:
            dev.ctrl_transfer(0x40, 0x65, args.prep, 0, b'', timeout=1000)
            time.sleep(0.2)
        last = dump(); t0 = time.monotonic()
        samples.append({'t': 0.0, 'regs': last})
        for k, v in last.items():
            print(f'{k:18s} {v:#010x} ({v})')
        if args.watch:
            print('# watching; move the carriage by hand, Ctrl-C to stop', flush=True)
            while not args.duration or time.monotonic() - t0 < args.duration:
                time.sleep(args.interval)
                cur = dump()
                changed = {k: (last[k], cur[k]) for k in NAMES if cur[k] != last[k]}
                if changed:
                    t = round(time.monotonic() - t0, 2)
                    samples.append({'t': t, 'regs': cur})
                    for k, (a, b) in changed.items():
                        d = (b - a) & 0xffffffff
                        d = d - (1 << 32) if d >= 1 << 31 else d
                        print(f't={t:7.2f}s {k:18s} {a:#010x} -> {b:#010x} (delta {d:+d})', flush=True)
                    last = cur
    except KeyboardInterrupt:
        pass
    finally:
        if args.output:
            args.output.write_text(json.dumps({'management': info, 'samples': samples}, indent=2) + '\n')
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
