#!/usr/bin/env python3
"""Emulate the open app on top of the REAL installed bootloader.

Builds a factory image from the installed boot_region.bin (stage 0 + stage 2,
read-only) plus the freshly built app, then runs it through the bootloader's
public ARM model. Asserts the app boots, answers management correctly (so
fwctl can always reflash it), drains the USB debug log, and reboots into
rescue on command. The model also verifies the boot region is never written.
"""
import hashlib
import json
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests'))
from model import Model, setup, TICKS_PER_MS  # noqa: E402

LOG = ('log', setup(0xc0, 0x50, 0, 0, 256))


def app_mode(e):
    return bool(e.info()) and e.info()[2] == 2


def log_text(e):
    blobs = [data for gen, name, data in e.responses if name == 'log' and gen == e.generation]
    return bytes(blobs[-1]) if blobs else b''


def main():
    boot_region = (BOOT / 'build/boot_region.bin').read_bytes()
    app = (APP / 'build/application.bin').read_bytes()
    assert len(boot_region) == 0x10000, 'unexpected boot region size'
    factory = (boot_region + app).ljust(0x200000, b'\xff')

    boot_syms = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    app_syms = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    metadata = {
        'layout': {'version': 2, 'application': [0x10000, 0x200000]},
        'symbols': {'bootloader': boot_syms['bootloader'], 'application': app_syms},
    }
    report = {'factory_sha256': hashlib.sha256(factory).hexdigest(),
              'app_sha256': hashlib.sha256(app).hexdigest(), 'cases': {}}

    def record(name, e):
        report['cases'][name] = e.summary()
        print('PASS', name, flush=True)

    # 1) Boots into the app behind the real bootloader, distinct USB identities.
    e = Model(image=factory, metadata=metadata)
    e.run(app_mode)
    assert e.identities[:2] == [0x176d, 0x12fe], e.identities
    assert 0x20020000 in e.entered
    magic, version, mode, flags, remaining, status, addr, capacity = e.info()
    assert magic == 0x5746474d and version == 2 and mode == 2
    assert flags & 4 and not (flags & (1 | 2 | 8 | 16))      # valid app; not held/busy/dangerous/blocked
    assert remaining == 0 and status == 0x80
    assert addr == 0x10000 and capacity == 0x1f0000
    record('app_boots_and_reports_management', e)

    # 2) USB debug log drains and carries the app's boot messages.
    e.requests.appendleft(LOG)
    e.run(lambda x: b'[app]' in log_text(x), resume=True)
    text = log_text(e)
    assert b'usb up' in text and b'app_flash=0x00010000' in text, text
    record('usb_debug_log_drains', e)

    # 3) Cooperative reboot into held rescue (the path fwctl flash relies on).
    before = e.resets
    e.requests.appendleft(('reboot_boot', setup(0x40, 0x41, 1)))
    e.run(lambda x: x.resets == before + 1 and bool(x.info())
          and x.info()[2] == 1 and bool(x.info()[3] & 1), resume=True)
    record('app_reboots_into_held_rescue', e)

    report['all_assertions_passed'] = True
    (APP / 'build/validation_app.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL APP ASSERTIONS PASSED', flush=True)


if __name__ == '__main__':
    main()
