#!/usr/bin/env python3
"""Rehearse a whole-flash install exactly as it will run on the board.

Starts from the saved baseline boot region (bootloader/build), lets that bootloader
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
from unicorn import UC_HOOK_MEM_INVALID
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_LR

STAY = ('stay', setup(0x40, 0x40))


def status(e, code):
    return bool(e.info()) and e.info()[5] == code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('manifest', type=Path, help='manifest.json of the bootloader inside IMAGE')
    parser.add_argument('--upload', type=Path, help='app package to upload through the new bootloader')
    parser.add_argument('--app-manifest',type=Path,help='symbols of the app in IMAGE (defaults to new demo)')
    parser.add_argument('--output',type=Path,help='checked JSON report; defaults beside the new manifest')
    args = parser.parse_args()
    image = args.image.read_bytes()
    assert len(image) == 0x200000
    installed = (ROOT / 'build/boot_region.bin').read_bytes()
    old = json.loads((ROOT / 'build/manifest.json').read_text())['symbols']['bootloader']
    new_meta=json.loads(args.manifest.read_text())
    new = new_meta['symbols']['bootloader']
    app_symbols=(json.loads(args.app_manifest.read_text()) if args.app_manifest else new_meta)['symbols']['application']
    # The installed flash: current boot region plus the image's own app slot.
    start = (installed + image[0x10000:]).ljust(0x200000, b'\xff')
    meta = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
            'symbols': {'installed': old, 'new': new, 'application':app_symbols}}
    report={'baseline_boot_region_sha256':hashlib.sha256(installed).hexdigest(),
            'target_sha256':hashlib.sha256(image).hexdigest(),'cases':{}}
    wire = dangerous_packet(image, hashlib.sha256(image).hexdigest())
    begin = ('begin', setup(0x40, 0x44, len(wire) & 65535, len(wire) >> 16))

    e = Model(image=start, metadata=meta, actions=[[STAY, begin]], bulk=wire, dangerous=True)
    def invalid(cpu,access,address,size,value,user):
        print('INVALID MEMORY',json.dumps({'pc':hex(cpu.reg_read(UC_ARM_REG_PC)),
            'sp':hex(cpu.reg_read(UC_ARM_REG_SP)),'lr':hex(cpu.reg_read(UC_ARM_REG_LR)),
            'address':hex(address),'size':size,'value':hex(value),'access':access}),flush=True)
        return False
    e.uc.hook_add(UC_HOOK_MEM_INVALID,invalid)
    e.run(lambda x: status(x, 0xc0), budget=900_000_000)
    assert bytes(e.uc.mem_read(0, 0x200000)) == image, 'written flash differs from the image'
    assert e.info()[3] & 8 and not e.info()[3] & 16 and not e.info()[3] & 2
    report['cases']['baseline_writer_installs_candidate']=e.summary()
    print('PASS baseline bootloader wrote and verified the full image', flush=True)

    e.requests.appendleft(('reboot_normal', setup(0x40, 0x41, 0)))
    e.run(lambda x: x.resets >= 1 and bool(x.info()) and x.info()[2] == 2, resume=True, budget=900_000_000)
    assert e.entered[-2:] == [0x20010000, 0x20020000], e.entered
    assert e.resets==2
    report['cases']['software_restart_into_current_application']=e.summary()
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
        report['cases']['normal_update_and_app_restart']=u.summary()
        print(f'PASS new bootloader accepted a {len(app)}-byte app and started it', flush=True)
    report['all_assertions_passed']=True
    output=args.output or args.manifest.parent/'migration_validation.json'
    for source in (args.image,args.manifest,args.upload,args.app_manifest):
        if source and output.resolve()==source.resolve():raise ValueError('report aliases an input')
    output.write_text(json.dumps(report,indent=2)+'\n')
    print('REHEARSAL PASSED', flush=True)


if __name__ == '__main__':
    main()
