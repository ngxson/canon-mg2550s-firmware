#!/usr/bin/env python3
"""Dump the printer's EEPROM (24C08) working cache over USB — read only.

Requires an app/patch build with the diag vendor hook (telemetry=True): the
running stock runtime answers vendor request 0xd2 by copying the 1 KiB EEPROM
working cache at 0x1878e200 (base verified in emulator; BRIEF_maintenance.md
§5) into the EP0 buffer, 64 bytes per transfer indexed by wValue = byte offset.

This is the firmware's live view of the 24C08 — where the usage counters
(power-on counts, print/ASF counts, setup/last-print timestamps, ink) live. It
never writes NVM. Clearing counters is a separate (write) command.

Usage:
    python3 tools/dump_eeprom.py [--out eeprom_dump.bin]
"""
import argparse
import datetime
from pathlib import Path

VID, PID, PRODUCT = 0x04a9, 0x176d, 'MG2500 series'
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
            raise RuntimeError(f'Short EEPROM read at offset {len(data)}: {len(part)} != {n}')
        data.extend(part)
    return bytes(data)


def hexdump(data):
    for off in range(0, len(data), 16):
        row = data[off:off+16]
        hexs = ' '.join(f'{b:02x}' for b in row)
        text = ''.join(chr(b) if 32 <= b < 127 else '.' for b in row)
        print(f'  {off:04x}  {hexs:<47}  {text}')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, help='save the raw 1 KiB dump to this file')
    p.add_argument('--no-hexdump', action='store_true')
    args = p.parse_args()
    import usb.util
    dev = connect()
    try:
        data = read_eeprom(dev)
    finally:
        usb.util.dispose_resources(dev)
    out = args.out or Path(f'eeprom_dump_{datetime.datetime.now():%Y%m%d_%H%M%S}.bin')
    out.write_bytes(data)
    print(f'EEPROM working cache: {len(data)} bytes -> {out}')
    if not args.no_hexdump:
        hexdump(data)


if __name__ == '__main__':
    main()
