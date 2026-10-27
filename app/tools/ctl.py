#!/usr/bin/env python3
"""Small vendor-request console for the open app.

  ctl.py req 0x8c 1 0          # OUT request (bRequest, wValue, wIndex); ack or stall is reported
  ctl.py in 0x78 0 0 64        # IN request: hex dump of the reply
  ctl.py log [--wait SECONDS]   # drain the firmware log (optionally after a delay)
  ctl.py req 0x8c 1 0 --log 1  # request, wait one second, then drain the log
"""
import argparse, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device  # noqa: E402


def drain(dev):
    out = b''
    for _ in range(800):
        chunk = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=2000))
        if not chunk:
            break
        out += chunk
    return out.decode('latin1')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('req'); r.add_argument('request', type=lambda v: int(v, 0)); r.add_argument('value', type=lambda v: int(v, 0), nargs='?', default=0)
    r.add_argument('index', type=lambda v: int(v, 0), nargs='?', default=0); r.add_argument('--log', type=float, default=None, help='seconds to wait, then drain the log')
    i = sub.add_parser('in'); i.add_argument('request', type=lambda v: int(v, 0)); i.add_argument('value', type=lambda v: int(v, 0), nargs='?', default=0)
    i.add_argument('index', type=lambda v: int(v, 0), nargs='?', default=0); i.add_argument('length', type=int, nargs='?', default=64)
    l = sub.add_parser('log'); l.add_argument('--wait', type=float, default=0)
    args = p.parse_args()
    import usb.core, usb.util
    dev, _ = wait_device('application')
    try:
        if args.cmd == 'req':
            try:
                dev.ctrl_transfer(0x40, args.request, args.value, args.index, b'', timeout=5000); print('ack')
            except usb.core.USBError as e:
                print('stall/error:', e)
            if args.log is not None:
                time.sleep(args.log); sys.stdout.write(drain(dev))
        elif args.cmd == 'in':
            data = bytes(dev.ctrl_transfer(0xc0, args.request, args.value, args.index, args.length, timeout=5000))
            print(data.hex(' '))
        else:
            time.sleep(args.wait); sys.stdout.write(drain(dev))
    finally:
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
