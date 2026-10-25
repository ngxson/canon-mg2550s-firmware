#!/usr/bin/env python3
"""Verify the actual frozen loader accepts/copies the offline candidate.

Stops BEFORE the app entry instruction. This says nothing about stock execution
or physical cache/vector support. All flash operations are in the emulator.
"""
import hashlib
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'bootloader/tests'))
from model import Model, setup


def check(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    out = ROOT/'emu/output/stockapp'
    image = (out/'candidate.bin').read_bytes()
    package = image[0x10000:]
    prefix = image[:0x10000]
    meta = json.loads((ROOT/'bootloader/build/manifest.json').read_text())
    meta['symbols'].pop('application', None)
    cases = {}
    e = Model(image=image, metadata=meta)
    e.run(lambda m: 0x20020000 in m.entered, budget=800_000_000)
    check(bytes(e.uc.mem_read(0x20020000, len(package)-32)) == package[32:],
          'full payload was not copied')
    check(not e.erases and not e.programs, 'launch wrote flash')
    cases['reset_to_app_entry_full_copy'] = e.summary()
    stay = ('stay', setup(0x40, 0x40))
    begin = ('begin', setup(0x40, 0x43, len(package)&65535, len(package)>>16))
    e = Model(actions=[[stay, begin]], bulk=package, metadata=meta)
    e.run(lambda m: bool(m.info()) and m.info()[5] == 0xc0, budget=1_500_000_000)
    check(bytes(e.uc.mem_read(0, 0x200000)) == image, 'normal update differs from candidate')
    check(min(e.erases) == 0x10000 and min(e.programs) == 0x10000,
          'update touched protected boot region')
    check(e.programs[-1] == 0x10000, 'header not committed last')
    cases['normal_usb_update_retains_both_stages'] = e.summary()
    # Faulted stock does not remove the next reset's bootloader window.
    restarted = Model(image=bytes(e.uc.mem_read(0, 0x200000)), actions=[[stay]], metadata=meta)
    restarted.run(lambda m: bool(m.info()) and bool(m.info()[3]&1), budget=400_000_000)
    check(0x20020000 not in restarted.entered, 'STAY did not hold rescue')
    cases['fresh_reset_can_hold_rescue'] = restarted.summary()
    report = dict(assertions_passed=True, candidate_sha256=hashlib.sha256(image).hexdigest(),
        protected_prefix_sha256=hashlib.sha256(prefix).hexdigest(),
        payload_bytes_copied=len(package)-32, cases=cases,
        limits=['Emulation only. Stops before app entry.',
                'Fresh reset, not an automatic reboot from running stock.',
                'No new physical recovery verification.'])
    (out/'loader.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({k: report[k] for k in ('assertions_passed', 'payload_bytes_copied')}))


if __name__ == '__main__':
    main()
