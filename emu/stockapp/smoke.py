#!/usr/bin/env python3
"""Execute the bounded stock start and its real exception/recovery callbacks.

Peripheral model changes at app entry and at the return to reset, as in the
registered candidate's reset probes. No firmware function/return is replaced.
SVC 7/8 tests execute real SVC instructions as explicit CPU stimuli; fault-vector
tests model exception entry. These do not prove physical recovery or timing.
"""
import hashlib
import json
from pathlib import Path
import struct
import sys

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(HERE))
sys.path.insert(0,str(ROOT/'bootloader/tests'))
from probe import Startup
from model import Model
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_LR

OUT=ROOT/'emu/output/stockapp/smoke'
BOOT=ROOT/'bootloader/build/vectors'
RECORD=0xe00008d0


class Smoke(Startup):
    def __init__(self,stop_at_handoff=False):
        self.stop_at_handoff=stop_at_handoff
        self.panel_reached=False
        super().__init__(candidate=OUT/'candidate.bin',from_reset=True,boot_build=BOOT)

    def code(self,cpu,address,size,user):
        if self.stop_at_handoff and address==0x11960:
            self.stop_reason='ready for explicit exception stimulus'
            cpu.emu_stop()
            return
        if address==0x135768:
            self.panel_reached=True
            self.instructions+=1
            self.pcs[address]+=1
            self.last_pcs.append(address)
            return  # Execute the actual patched SVC; bypass only parent's stop.
        if address==0:
            self.stop_reason='real reset branch reached'
            cpu.emu_stop()
            return
        super().code(cpu,address,size,user)


def check_return(e,reason):
    assert e.error is None and e.stop_reason=='real reset branch reached',e.result()
    rec=struct.unpack('<8I',e.uc.mem_read(RECORD,32))
    assert rec[:6]==(0x31504d53,0xceafb2ac,2,0xfffffffd,reason,reason^0xffffffff),rec
    assert struct.unpack('<4I',e.uc.mem_read(0xe0000800,16))==(0x4d474252,1,0xb2b8bdad,0xfffffffe)
    assert e.uc.reg_read(UC_ARM_REG_CPSR)&0xff==0xd3
    assert not e.spi_writes and not e.cpu_flash_stores
    assert bytes(e.uc.mem_read(0,0x200000))==e.execution_image
    return {'reason':hex(reason),'record':[hex(v) for v in rec],
            'svcs':e.svcs,'panel_reached':e.panel_reached,'flash_unchanged':True,
            'spi_writes':0,'returned_to_reset':True}


def main():
    report={'status':'Offline only; independent physical recovery still required','cases':{}}
    report['package_sha256']=hashlib.sha256((OUT/'candidate.mgap').read_bytes()).hexdigest()
    e=Smoke()
    e.run(budget=8_000_000)
    report['cases']['startup_to_rescue']=check_return(e,0xfe)
    assert e.panel_reached and e.svcs==879
    md=json.loads((BOOT/'manifest.json').read_text())
    md['symbols'].pop('application',None)
    loader=Model(image=e.execution_image,metadata=md)
    loader.uc.mem_write(0x20000000,bytes(e.uc.mem_read(0x20000000,0x800000)))
    loader.uc.mem_write(0xe0000000,bytes(e.uc.mem_read(0xe0000000,0x10000)))
    loader.uc.context_restore(e.uc.context_save())
    loader.regs.update(e.registers)
    loader.run(lambda x:bool(x.info()) and x.info()[2]==1 and x.info()[3]&1,
               budget=800_000_000)
    assert not loader.erases and not loader.programs
    assert bytes(loader.uc.mem_read(RECORD,32))==bytes(e.uc.mem_read(RECORD,32))
    report['cases']['real_reset_into_held_usb_rescue']={'passed':True,'info':list(loader.info()),
        'record_survived':True,'flash_unchanged':True}
    print('PASS startup, 879 real SVCs, recovery reset and held USB rescue',flush=True)
    for thumb in (False,True):
        for reason in (7,8):
            e=Smoke(stop_at_handoff=True)
            e.run();assert e.stop_reason=='ready for explicit exception stimulus'
            e.stop_at_handoff=False;e.stop_reason=None
            site=0x207f0000
            e.uc.mem_write(site,struct.pack('<H',0xdf00|reason) if thumb else struct.pack('<I',0xef000000|reason))
            e.uc.reg_write(UC_ARM_REG_PC,site)
            # Unicorn's PC write selects ARM state for an even address. Set
            # the modeled caller state afterward, before run() supplies bit 0.
            e.uc.reg_write(UC_ARM_REG_CPSR,0x30 if thumb else 0x10)
            assert bool(e.uc.reg_read(UC_ARM_REG_CPSR)&0x20)==thumb
            e.run()
            name=('thumb' if thumb else 'arm')+'_svc_'+str(reason)
            report['cases'][name]=check_return(e,reason)
            print('PASS',name,flush=True)
    for name,vector,mode,reason in [('undef',4,0xdb,0x11),('pabort',12,0xd7,0x12),
                                  ('dabort',16,0xd7,0x13),('reserved',20,0xd3,0x14),
                                  ('fiq',28,0xd1,0x15)]:
        e=Smoke(stop_at_handoff=True);e.run()
        assert e.stop_reason=='ready for explicit exception stimulus'
        e.stop_at_handoff=False;e.stop_reason=None
        e.uc.reg_write(UC_ARM_REG_CPSR,mode)
        e.uc.reg_write(UC_ARM_REG_SPSR,0x10)
        e.uc.reg_write(UC_ARM_REG_LR,0x11964)
        e.uc.reg_write(UC_ARM_REG_PC,vector)
        e.run()
        report['cases'][name]=check_return(e,reason)
        print('PASS',name,flush=True)
    report['all_assertions_passed']=True
    (OUT/'validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
