#!/usr/bin/env python3
"""Execute the compiled hook and native dispatch/lookup; USB wire model excluded."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_MEM_WRITE, UC_HOOK_CODE
from unicorn.arm_const import *

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'tools'))
from stocktrace import Reader, REGIONS

OBJ = 0x180409f8
BUF = 0x100129e0
STACK = 0x18070000
STOP = 0x1fff00
REGS = [UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3,
        UC_ARM_REG_R4,UC_ARM_REG_R5,UC_ARM_REG_R6,UC_ARM_REG_R7,
        UC_ARM_REG_R8,UC_ARM_REG_R9,UC_ARM_REG_R10,UC_ARM_REG_R11]


class Fixture:
    def __init__(self, image):
        self.uc = u = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        u.mem_map(0, 0x200000); u.mem_write(0, image)
        u.mem_map(0x10012000, 0x2000); u.mem_map(0x18000000, 0x800000)
        self.writes = []
        self.stop = None
        u.hook_add(UC_HOOK_CODE, lambda uc,addr,size,user:
                   uc.emu_stop() if addr==self.stop else None)
        u.hook_add(UC_HOOK_MEM_WRITE, lambda uc,access,addr,size,value,user:
                   self.writes.append((addr,size,value)))
        # Native lookup reads configured interface presence and numbers.
        u.mem_write(0x180409e4, b'\x01\x01\0\0\0\x00\x01\0\0')
        for region,(address,length) in enumerate(REGIONS):
            u.mem_write(address, bytes((i*13+region)&255 for i in range(length)))

    def run(self, start, stop):
        # Explicit instruction hook: repeated runs use different stop addresses
        # in the same translated block, so don't rely on cached TB boundaries.
        self.stop = stop
        self.uc.emu_start(start,0,count=10000)
        assert self.uc.reg_read(UC_ARM_REG_PC)==stop

    def lookup(self, typ, req, value, index, length, misalign=0, buffer=BUF):
        u = self.uc
        u.mem_write(OBJ, struct.pack('<IBBHHHII',0,typ,req,value,index,length,0,buffer))
        u.mem_write(BUF-8, b'\xa5'*80)
        u.reg_write(UC_ARM_REG_CPSR,0x30)  # Actual user-mode Thumb caller.
        sp = STACK-misalign
        u.reg_write(UC_ARM_REG_SP,sp)
        for i,reg in enumerate(REGS): u.reg_write(reg,0x12340000+i)
        u.reg_write(UC_ARM_REG_R4,OBJ); u.reg_write(UC_ARM_REG_R5,typ)
        u.reg_write(UC_ARM_REG_R6,3); u.reg_write(UC_ARM_REG_R7,length)
        u.mem_write(sp,struct.pack('<I',req))
        self.writes.clear()
        self.run(0x51bf1,0x51bfa)
        assert u.reg_read(UC_ARM_REG_PC)==0x51bfa
        assert u.reg_read(UC_ARM_REG_SP)==sp
        assert u.reg_read(UC_ARM_REG_R4)==OBJ and u.reg_read(UC_ARM_REG_R5)==typ
        assert u.reg_read(UC_ARM_REG_R7)==length
        assert [u.reg_read(r) for r in REGS[8:]]==[0x12340000+i for i in range(8,12)]
        for addr,size,_ in self.writes:
            assert sp-512<=addr and addr+size<=sp or \
                   BUF<=addr and addr+size<=BUF+64 or addr==OBJ+12 and size==4,(hex(addr),size)
        assert u.mem_read(BUF-8,8)==b'\xa5'*8 and u.mem_read(BUF+64,8)==b'\xa5'*8
        return u.reg_read(UC_ARM_REG_R0),u.reg_read(UC_ARM_REG_R6)

    def transfer(self, req, value, index, length):
        lookup,result = self.lookup(0xc0,req,value,index,length)
        assert lookup==15 and result==2
        # Execute the native unknown vendor-IN continuation, then completion.
        self.run(0x51bfb,0x51c18)
        assert self.uc.reg_read(UC_ARM_REG_PC)==0x51c18
        assert self.uc.reg_read(UC_ARM_REG_R6)==2
        n = struct.unpack('<I',self.uc.mem_read(OBJ+12,4))[0]
        data = bytes(self.uc.mem_read(BUF,n))
        self.uc.reg_write(UC_ARM_REG_R0,OBJ)
        self.uc.reg_write(UC_ARM_REG_LR,STOP|1)
        self.run(0x51c59,STOP)
        assert self.uc.reg_read(UC_ARM_REG_PC)==STOP
        return data


def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path)
    out=p.parse_args().directory.resolve()
    image=(out/'candidate.bin').read_bytes()
    m=json.loads((out/'manifest.json').read_text())
    assert m['telemetry'] and hashlib.sha256(image).hexdigest()==m['candidate_sha256']
    original=bytearray(image); original[0x51bf6:0x51bfa]=bytes.fromhex('fff7d1fe')
    patched,base=Fixture(image),Fixture(bytes(original))
    count=0
    for typ in (0x40,0x41,0xc0,0xc1):
        for req in range(256):
            for index in (0,1):
                for alignment in (0,4):
                    actual=patched.lookup(typ,req,0,index,4,alignment)
                    expected=base.lookup(typ,req,0,index,4,alignment)
                    if typ!=0xc0 or req not in (0xd0,0xd1):
                        assert actual==expected,(typ,req,index,actual,expected)
                        assert bytes(patched.uc.mem_read(OBJ,20))==bytes(base.uc.mem_read(OBJ,20))
                        assert patched.uc.mem_read(BUF,64)==base.uc.mem_read(BUF,64)
                        count+=1
    accepted=0
    full_info=patched.transfer(0xd0,0,0,64)
    for length in range(1,65):
        assert patched.transfer(0xd0,0,0,length)==full_info[:length]
    for region,(address,size) in enumerate(REGIONS):
        for offset,n in ((0,min(64,size)),(size-1,1),(max(0,size-64),min(64,size))):
            expected=bytes(patched.uc.mem_read(address+offset,n))
            assert patched.transfer(0xd1,offset,region,n)==expected
            accepted+=1
    # ABI 3: the 112-byte map is read with wValue as byte offset.
    words=4+2*len(REGIONS); size=4*words
    tail=patched.transfer(0xd0,64,0,64)
    assert len(tail)==size-64 and full_info+tail==struct.pack(f'<{words}I',0x314c5453,3,64,len(REGIONS),*(v for r in REGIONS for v in r))
    assert patched.transfer(0xd0,1,0,8)==full_info[1:9] and patched.transfer(0xd0,size-1,0,1)==tail[-1:]
    rejected=0
    for req,value,index,length,buffer in (
        (0xd0,size,0,48,BUF),(0xd0,0xffff,0,1,BUF),(0xd0,0,1,48,BUF),(0xd0,0,0,0,BUF),
        (0xd0,0,0,65,BUF),(0xd1,0,len(REGIONS),4,BUF),(0xd1,0xffff,2,1,BUF),
        (0xd1,1023,2,2,BUF),(0xd1,0,2,65535,BUF),(0xd1,0,0,5,BUF),(0xd1,2,4,1,BUF),
        (0xd1,511,5,2,BUF),(0xd1,0,6,513,BUF),(0xd1,0x128,7,1,BUF),(0xd1,0,8,29,BUF),
        (0xd1,1,9,1,BUF),(0xd1,0,11,6,BUF),
        (0xd1,0,0,4,0xc0780014),(0xd1,0,0,0,BUF)):
        assert patched.lookup(0xc0,req,value,index,length,buffer=buffer)==(15,3)
        assert patched.uc.mem_read(BUF,64)==b'\xa5'*64
        assert struct.unpack('<I',patched.uc.mem_read(OBJ+12,4))[0]==0
        rejected+=1
    reader=Reader(patched.transfer)
    snapshot=reader.snapshot(history=True,messages=True)
    assert snapshot['usb_state']=={'state':0,'mode':13,'initialized':26,'attached':39}
    assert len(bytes.fromhex(snapshot['setup_history']['first_words_hex']))==1024
    for key,region in (('all_ids_hex',5),('filtered_ids_hex',6)):
        assert bytes.fromhex(snapshot['messages'][key])==bytes(patched.uc.mem_read(REGIONS[region][0],512))
    assert snapshot['messages']['filtered_index_before']==4 and snapshot['messages']['filtered_processed_before']==17
    engine=reader.snapshot(engine=True)['engine']
    assert bytes.fromhex(engine['status_raw_hex'])==bytes(patched.uc.mem_read(REGIONS[7][0],0x128))
    assert engine['asf_state']==patched.uc.mem_read(REGIONS[9][0],1)[0]
    assert engine['lf_position_ulf']==int.from_bytes(patched.uc.mem_read(REGIONS[10][0],4),'little',signed=True)
    assert engine['edge_state']==patched.uc.mem_read(REGIONS[11][0]+4,1)[0]
    assert engine['geometry_600dpi']['active_x']==struct.unpack('<h',patched.uc.mem_read(REGIONS[8][0],2))[0]
    assert engine['djs'] in ('NO','S1','CC','WP','LD','PR','EJ','ID') or engine['djs'].startswith('?')
    for fixture in (patched,base):
        assert bytes(fixture.uc.mem_read(0,0x10000))==image[:0x10000]
    report={'passed':True,'package_sha256':m['package_sha256'],
            'passthrough_cases':count,'accepted_boundary_reads':accepted,'rejected_requests':rejected,
            'compiled_user_mode_hook':True,'native_lookup_and_in_continuation':True,
            'native_completion_no_callback':True,'stack_alignment_0_and_4':True,
            'host_reader_roundtrip':True,'no_mmio_mapping':True,'write_allowlist_enforced':True,
            'info_short_reply_lengths_tested':64,'abi':3,'regions':len(REGIONS),'message_rings_roundtrip':True,'engine_regions_roundtrip':True,
            'boot_prefix_unchanged':True,'hardware_tested':False,
            'limits':['Fixture starts after native SETUP acquisition; USB electrical delivery is not modeled.',
                      'RAM snapshots are not atomic; actual cache, DMA and concurrency need hardware validation.'],
            'test_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'host_source_sha256':hashlib.sha256((ROOT/'tools/stocktrace.py').read_bytes()).hexdigest()}
    (out/'telemetry_validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
