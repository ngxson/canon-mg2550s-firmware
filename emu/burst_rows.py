#!/usr/bin/env python3
"""Emulator: one colour burst (n=8, C=3) on the eSCL scan model, then report which ring rows/samples hold the sentinel."""
import struct, sys
from pathlib import Path
APP = Path('/project/app'); BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests'))
from model import setup
import validate_escl as v
from validate_escl_scan import ScanHost
factory, app, metadata = v.factory_and_metadata(APP / 'build'); metadata['build'] = str(APP / 'build')
e = ScanHost(image=factory, metadata=metadata)
e.run(v.app_mode)
def req(name, pkt):
    before = len(e.responses); stalls = len(e.stalls)
    e.requests.appendleft((name, pkt)); e.run(lambda q: len(q.responses) > before or len(q.stalls) > stalls, resume=True)
req('opts', setup(0x40, 0x5e, 3 | 1 << 4, 0))
req('burst', setup(0x40, 0x5b, 8 | 6 << 8, 12))
text = ''
for _ in range(400):
    before = len(e.responses); e.requests.appendleft(('log', setup(0xc0, 0x50, 0, 0, 256))); e.run(lambda q: len(q.responses) > before, resume=True)
    text += bytes(e.responses[-1][2]).decode('latin1')
    if 'guard=' in text: break
print([l for l in text.splitlines() if 'done=' in l or 'guard=' in l or 'priming' in l])
rows = 25
for r in range(rows):
    data = bytes(e.uc.mem_read(0x20420000 + r * 0x1440, 0x1440))
    s = struct.unpack('<2592H', data)
    sent = [i for i, x in enumerate(s) if x == 0xa5a5]
    print('row', r, 'sentinel', len(sent), 'first', sent[:3], 'last', sent[-3:], 'ch0 mean', sum(s[:1296]) // 1296, 'ch1 mean', sum(s[1296:]) // 1296)
