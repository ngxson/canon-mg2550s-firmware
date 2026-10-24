#!/usr/bin/env python3
"""Rehearse a whole-flash install exactly as it will run on the board.

Starts from the INSTALLED boot region (bootloader/build), lets that bootloader
receive the new 2 MiB image through the USB dangerous-write path, checks the
written flash byte for byte, reboots into the new image, waits for its
application, and finally performs a normal USB app upload through the new
bootloader. No dump or stock code is involved.

Usage: rehearse_install.py FULL_FLASH.bin NEW_MANIFEST.json [--upload APP.bin]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT.parent / 'tools'))
from model import Model, setup  # noqa: E402
from fwctl import dangerous_packet  # noqa: E402

STAY = ('stay', setup(0x40, 0x40))


def status(e, code):
    return bool(e.info()) and e.info()[5] == code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('manifest', type=Path, help='manifest.json of the bootloader inside IMAGE')
    parser.add_argument('--upload', type=Path, help='app package to upload through the new bootloader')
    args = parser.parse_args()
    image = args.image.read_bytes()
    assert len(image) == 0x200000
    installed = (ROOT / 'build/boot_region.bin').read_bytes()
    old = json.loads((ROOT / 'build/manifest.json').read_text())['symbols']['bootloader']
    new = json.loads(args.manifest.read_text())['symbols']['bootloader']
    # The installed flash: current boot region plus the image's own app slot.
    start = (installed + image[0x10000:]).ljust(0x200000, b'\xff')
    meta = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
            'symbols': {'installed': old, 'new': new}}
    wire = dangerous_packet(image, hashlib.sha256(image).hexdigest())
    begin = ('begin', setup(0x40, 0x44, len(wire) & 65535, len(wire) >> 16))

    e = Model(image=start, metadata=meta, actions=[[STAY, begin]], bulk=wire, dangerous=True)
    e.run(lambda x: status(x, 0xc0), budget=900_000_000)
    assert bytes(e.uc.mem_read(0, 0x200000)) == image, 'written flash differs from the image'
    assert e.info()[3] & 8 and not e.info()[3] & 16 and not e.info()[3] & 2
    print('PASS installed bootloader wrote and verified the full image', flush=True)

    e.requests.appendleft(('reboot_normal', setup(0x40, 0x41, 0)))
    e.run(lambda x: x.resets >= 1 and bool(x.info()) and x.info()[2] == 2, resume=True, budget=900_000_000)
    assert e.entered[-2:] == [0x20010000, 0x20020000], e.entered
    print('PASS rebooted through the new bootloader into the application', flush=True)

    if args.upload:
        app = args.upload.read_bytes()
        u = Model(image=image, metadata=meta, bulk=app,
                  actions=[[STAY, ('begin', setup(0x40, 0x43, len(app) & 65535, len(app) >> 16))]])
        u.run(lambda x: status(x, 0xc0), budget=900_000_000)
        assert bytes(u.uc.mem_read(0x10000, len(app))) == app
        assert u.info()[3] & 4, 'new app not reported valid'
        u.requests.appendleft(('reboot_app', setup(0x40, 0x41, 2)))
        u.run(lambda x: bool(x.info()) and x.info()[2] == 2, resume=True, budget=900_000_000)
        print(f'PASS new bootloader accepted a {len(app)}-byte app and started it', flush=True)
    print('REHEARSAL PASSED', flush=True)


if __name__ == '__main__':
    main()
