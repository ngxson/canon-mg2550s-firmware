#!/usr/bin/env python3
"""Map the MG2550s front-panel buttons.

Enables the firmware key probe (USB vendor request 0x70), then streams the debug
log and highlights the `[btn] 028=0x...` lines. Press one button at a time and
note which bit(s) of the PANEL+0x28 key word change; that builds the physical
bit<->button map (power / cancel / copy B&W / copy colour / access-door switch).

Usage:  python3 buttons.py            # enable + watch
        python3 buttons.py --no-enable  # just watch (already enabled)
"""
import argparse
import sys
import time

VID = 0x04a9
APP_PID = 0x12fe
REQUEST_LOG = 0x50
REQUEST_BTN = 0x70


def find(serial=None):
    import usb.core
    for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=APP_PID):
        if serial is None or dev.serial_number == serial:
            return dev
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', help='Printer USB serial number')
    parser.add_argument('--no-enable', action='store_true', help='Do not send 0x70 (already enabled)')
    args = parser.parse_args()
    import usb.core
    import usb.util

    print('# waiting for MG2550s app (04a9:12fe)...', file=sys.stderr, flush=True)
    dev = None
    enabled = args.no_enable
    buf = b''
    last = None
    while True:
        try:
            if dev is None:
                dev = find(args.serial)
                if dev is None:
                    time.sleep(0.3)
                    continue
                print('# connected', file=sys.stderr, flush=True)
                enabled = args.no_enable
            if not enabled:
                dev.ctrl_transfer(0x40, REQUEST_BTN, 0, 0, None, timeout=500)
                enabled = True
                print('# key probe enabled; press each button once', file=sys.stderr, flush=True)
            data = dev.ctrl_transfer(0xC0, REQUEST_LOG, 0, 0, 256, timeout=500)
            if data:
                buf += bytes(data)
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    text = line.decode('latin1')
                    if '[btn] 028=' in text:
                        val = text.split('028=')[1].strip()
                        changed = '' if last is None else f'  (changed bits: 0x{int(val, 16) ^ int(last, 16):08x})'
                        print(f'KEY {val}{changed}', flush=True)
                        last = val
                    else:
                        print(text, flush=True)
            else:
                time.sleep(0.03)
        except KeyboardInterrupt:
            break
        except usb.core.USBError:
            usb.util.dispose_resources(dev) if dev is not None else None
            dev = None
            print('\n# device went away, waiting...', file=sys.stderr, flush=True)
            time.sleep(0.3)


if __name__ == '__main__':
    main()
