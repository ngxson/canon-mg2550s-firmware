#!/usr/bin/env python3
"""Clear the printer's usage counters over USB (vendor request 0xd3).

Requires an app/patch build with `clear_counters` armed AND configured (the
firmware returns status 0xEE = "not configured" until the checksum-safe write
path from BRIEF_counters.md is wired in). This tool is safe to run regardless:
it dumps the EEPROM working cache before and after and shows the diff, so the
effect (and only the effect) is visible. A backup exists
(eeprom_backup_initial.bin) if a write ever needs reverting via the I2C bridge.

Usage:
    python3 tools/clear_counters.py            # dump, clear, dump, diff
    python3 tools/clear_counters.py --dry-run  # just dump (no clear)
"""
import argparse
from pathlib import Path

VID, PID, PRODUCT = 0x04a9, 0x176d, 'MG2500 series'
CLEAR_REQUEST = 0xd3
EEPROM_REQUEST = 0xd2
EEPROM_LENGTH = 1024


def connect():
    import usb.core, usb.util
    devices = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID))
    if len(devices) != 1:
        raise RuntimeError(f'Expected one directly attached MG2500, found {len(devices)}')
    dev = devices[0]
    if usb.util.get_string(dev, dev.iProduct) != PRODUCT:
        raise RuntimeError('Device is not the running stock runtime (is it in the bootloader?)')
    return dev


def read_eeprom(dev):
    data = bytearray()
    while len(data) < EEPROM_LENGTH:
        n = min(64, EEPROM_LENGTH - len(data))
        part = bytes(dev.ctrl_transfer(0xc0, EEPROM_REQUEST, len(data), 0, n, timeout=2000))
        if len(part) != n:
            raise RuntimeError(f'Short EEPROM read at {len(data)}')
        data.extend(part)
    return bytes(data)


def clear(dev, selector):
    status = bytes(dev.ctrl_transfer(0xc0, CLEAR_REQUEST, selector, 0, 1, timeout=2000))
    return status[0] if status else None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dry-run', action='store_true', help='dump only, do not clear')
    p.add_argument('--counter', type=int, help='clear only this counter index (default: all). '
                   'Use a single index first to verify the mechanism safely.')
    p.add_argument('--out', type=Path, help='save the post-clear dump here')
    args = p.parse_args()
    selector = 0xffff if args.counter is None else args.counter
    import usb.util
    dev = connect()
    try:
        before = read_eeprom(dev)
        if args.dry_run:
            print('dry run: EEPROM dumped, no clear issued')
            (args.out or Path('eeprom_before.bin')).write_bytes(before)
            return
        status = clear(dev, selector)
        if status == 0xEE:
            print('clear-counters is ARMED but NOT CONFIGURED in this build '
                  '(0xEE) — no NVM write performed.')
        else:
            tgt = 'all' if selector == 0xffff else f'index {selector}'
            print(f'clear-counters ({tgt}): {status} counters written')
        after = read_eeprom(dev)
        diffs = [i for i in range(EEPROM_LENGTH) if before[i] != after[i]]
        print(f'EEPROM changed bytes: {len(diffs)}')
        for i in diffs:
            print(f'  0x{i:03x}: {before[i]:02x} -> {after[i]:02x}')
        (args.out or Path('eeprom_after.bin')).write_bytes(after)
    finally:
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
