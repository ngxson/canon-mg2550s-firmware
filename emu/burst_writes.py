#!/usr/bin/env python3
"""Record the ordered MMIO writes of one grey burst (vendor 0x5b n=4) on a given app build."""
import json, sys
from pathlib import Path
build = Path(sys.argv[1]); out = Path(sys.argv[2])
APP = Path('/project/app'); BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests'))
from model import setup
import validate_burst as vb
import validate_escl as v
from unicorn import UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_PC
factory, app, metadata = v.factory_and_metadata(build)
e = vb.BurstModel(image=factory, metadata=metadata)
e.run(v.app_mode)
writes = []
def hook(cpu, access, address, size, value, user):
    if 0xc0100000 <= address < 0xc0e00000 and len(writes) < 20000:
        writes.append((hex(address), hex(value)))
e.uc.hook_add(UC_HOOK_MEM_WRITE, hook, begin=0xc0000000, end=0xc0ffffff)
e.requests.append(('burst', setup(0x40, 0x5b, 4 | 6 << 8, 12)))
text = ''
for _ in range(400):
    e.requests.append(('log', setup(0xc0, 0x50, 0, 0, 256)))
    e.run(lambda x: x.requests == type(x.requests)() and x.current is None, resume=True)
    text += ''.join(d.decode('latin1') for g, n, d in e.responses[-1:] if n == 'log')
    if '[burst] guard=' in text: break
out.write_text(json.dumps({'writes': writes, 'log': text}, indent=0))
print(len(writes), 'writes')
