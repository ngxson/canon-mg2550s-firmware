#!/usr/bin/env python3
"""Set / clear the "ignore low ink" persistent override over USB (request 0xd5).

Replicates what holding Stop/Cancel on a low-ink error does (BRIEF_ink_off.md):
sets INK_OFF_BK/CL (0x410510a4/a5, EEPROM 0x22c bits 3/4) and the group-3 mirror
(0x410310a2/a3) through the field write path, then commits. Disables only
ink-LEVEL monitoring (lamp/nag/print-gate), not cartridge-presence detection.

A WRITE command. EEPROM is recoverable from eeprom_backup_initial.bin via the
I2C bridge. The cleanest result needs a reboot after setting so the monitor
re-syncs from the persisted bits.

    python3 tools/set_ink_off.py --tank cl          # ignore low ink on CL
    python3 tools/set_ink_off.py --tank both        # both tanks
    python3 tools/set_ink_off.py --tank cl --clear  # re-enable CL monitoring
"""
import argparse
from pathlib import Path

VID, PID, PRODUCT = 0x04a9, 0x176d, 'MG2500 series'
REQUEST, EEPROM_REQUEST, EEPROM_LENGTH = 0xd5, 0xd2, 1024
TANK_MASK = {'bk': 1, 'cl': 2, 'both': 3}


def connect():
    import usb.core, usb.util
    devs = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID))
    if len(devs) != 1:
        raise RuntimeError(f'Expected one directly attached MG2500, found {len(devs)}')
    dev = devs[0]
    if usb.util.get_string(dev, dev.iProduct) != PRODUCT:
        raise RuntimeError('Device is not the running stock runtime (is it in the bootloader?)')
    return dev


def read_22c(dev):
    data = bytes(dev.ctrl_transfer(0xc0, EEPROM_REQUEST, 0x22c, 0, 1, timeout=2000))
    return data[0] if data else None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tank', choices=TANK_MASK, default='both')
    p.add_argument('--clear', action='store_true', help='re-enable monitoring instead of ignoring')
    args = p.parse_args()
    import usb.util
    dev = connect()
    try:
        before = read_22c(dev)
        mask = TANK_MASK[args.tank]
        windex = 1 if args.clear else 0            # wIndex: 0 set/ignore, !=0 clear
        status = bytes(dev.ctrl_transfer(0xc0, REQUEST, mask, windex, 1, timeout=2000))
        n = status[0] if status else None
        if n == 0xEE:
            print('ink-off is ARMED but NOT CONFIGURED in this build (0xEE) -- no write.')
        else:
            action = 'clear' if args.clear else 'set'
            print(f'ink-off {action} tank={args.tank}: {n} field writes reported success')
        after = read_22c(dev)
        print(f'EEPROM 0x22c: 0x{before:02x} -> 0x{after:02x} '
              f'(INK_OFF_BK bit3={bool(after & 0x08)}, INK_OFF_CL bit4={bool(after & 0x10)})')
        print('NOTE: power-cycle for the monitor to re-sync (lamp/gate update).')
    finally:
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
