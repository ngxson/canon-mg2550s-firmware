#!/usr/bin/env python3
"""Select a panel button map and collect the bench log.

  bench.py map 1          # feed bench: B/W = place a sheet, Colour = advance 1024, Power = carriage park/60000, Cancel = eject/stop
  bench.py map 0          # default map
  bench.py log            # drain and print the firmware log (bench lines carry the feed counter, sensor and carriage position)
  bench.py log --follow   # keep printing until Ctrl-C
"""
import argparse, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('map'); m.add_argument('map', type=int)
    l = sub.add_parser('log'); l.add_argument('--follow', action='store_true')
    args = p.parse_args()
    import usb.util
    dev, info = wait_device('application')
    try:
        if args.cmd == 'map':
            dev.ctrl_transfer(0x40, 0x8b, args.map, 0, b'', timeout=1000); print('button map', args.map)
        while True:
            data = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
            if data: sys.stdout.write(data.decode(errors='replace')); sys.stdout.flush()
            elif args.cmd == 'log' and args.follow: time.sleep(0.2)
            else: break
    except KeyboardInterrupt:
        pass
    finally:
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
