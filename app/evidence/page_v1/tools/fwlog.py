#!/usr/bin/env python3
"""Stream the open app's USB debug log.

Polls the app's vendor control-IN request (bmRequestType 0xC0, bRequest 0x50)
and prints whatever the firmware has buffered. Survives reboots: when the
device goes away (e.g. a reboot into rescue) it waits for it to come back.
"""
import argparse
import sys
import time

VID = 0x04a9
APP_PID = 0x12fe
REQUEST_LOG = 0x50


def find(serial=None):
    import usb.core
    for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=APP_PID):
        if serial is None or dev.serial_number == serial:
            return dev
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', help='Printer USB serial number')
    parser.add_argument('--chunk', type=int, default=256, help='Max bytes per poll (<=256)')
    args = parser.parse_args()
    import usb.core
    import usb.util

    print('# waiting for MG2550s app (04a9:12fe)...', file=sys.stderr, flush=True)
    dev = None
    while True:
        try:
            if dev is None:
                dev = find(args.serial)
                if dev is None:
                    time.sleep(0.3)
                    continue
                print('# connected', file=sys.stderr, flush=True)
            data = dev.ctrl_transfer(0xC0, REQUEST_LOG, 0, 0, min(args.chunk, 256), timeout=500)
            if data:
                sys.stdout.buffer.write(bytes(data))
                sys.stdout.buffer.flush()
            else:
                time.sleep(0.05)
        except KeyboardInterrupt:
            break
        except usb.core.USBError:
            usb.util.dispose_resources(dev) if dev is not None else None
            dev = None
            print('\n# device went away, waiting...', file=sys.stderr, flush=True)
            time.sleep(0.3)


if __name__ == '__main__':
    main()
