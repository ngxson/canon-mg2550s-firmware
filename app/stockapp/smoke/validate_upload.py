#!/usr/bin/env python3
"""Execute protected full-slot smoke upload without running its stock entry."""
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'bootloader/tests'))
from model import Model,setup
out=ROOT/'emu/output/stockapp/smoke'
data=(out/'candidate.mgap').read_bytes()
prefix=(ROOT/'bootloader/build/vectors/boot_region.bin').read_bytes()
original=(ROOT/'app/build/application.bin').read_bytes()
metadata=json.loads((ROOT/'bootloader/build/vectors/manifest.json').read_text())
e=Model(image=(prefix+original).ljust(0x200000,b'\xff'),metadata=metadata,
        actions=[[('stay',setup(0x40,0x40)),('begin',setup(0x40,0x43,len(data)&65535,len(data)>>16))]],bulk=data)
e.run(lambda x:bool(x.info()) and x.info()[5]==0xc0,budget=500_000_000)
assert e.info()[5]==0xc0 and e.bulk_position==len(data)
assert bytes(e.uc.mem_read(0,0x10000))==prefix
assert bytes(e.uc.mem_read(0x10000,len(data)))==data
assert min(e.erases)>=0x10000 and min(e.programs)>=0x10000 and e.programs[-1]==0x10000
r={'passed':True,'package_sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),
   'boot_region_unchanged':True,'erases':e.erases,'program_count':len(e.programs),'header_committed_last':True}
(out/'upload_validation.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps(r,indent=2))
