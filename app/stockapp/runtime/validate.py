#!/usr/bin/env python3
"""Runtime recovery guards and actual stock parsers, with modeled input FIFO."""
import argparse,hashlib,json,struct,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'emu/output/stockapp/runtime'
BOOT=ROOT/'bootloader/build/vectors'
sys.path[:0]=[str(ROOT/'emu/stockapp'),str(ROOT/'emu/evidence'),str(ROOT/'bootloader/tests')]
from probe import Inspect,Startup
from stock_download_paths_probe import MessageProbe
from model import Model,setup
from unicorn.arm_const import UC_ARM_REG_PC,UC_ARM_REG_CPSR

class Guard(Startup):
    def __init__(self):
        self.ready=False
        super().__init__(candidate=OUT/'candidate.bin',from_reset=True,boot_build=BOOT)
    def code(self,cpu,address,size,user):
        if address==0 or (address==0x11960 and not self.ready):
            self.stop_reason='reset' if address==0 else 'handoff';cpu.emu_stop();return
        super().code(cpu,address,size,user)

class Managed(Inspect,MessageProbe):
    def code(self,cpu,address,size,user):
        if address==0:
            self.stop_reason='reset';cpu.emu_stop();return
        super().code(cpu,address,size,user)

def check(e,reason):
    assert e.error is None and e.stop_reason=='reset',e.result()
    record=list(struct.unpack('<8I',e.uc.mem_read(0xe00008d0,32)))
    assert record[:6]==[0x31504d53,0xceafb2ac,2,0xfffffffd,reason,reason^0xffffffff],record
    assert not e.spi_writes and not e.cpu_flash_stores
    audit=Inspect.audit(e)
    assert audit['full_image_unchanged'] and audit['registration_intact']
    return {'reason':hex(reason),'record':[hex(v) for v in record],'flash_unchanged':True,'spi_writes':0}

def main():
    global OUT
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,default=OUT)
    OUT=p.parse_args().directory.resolve()
    report={'package_sha256':hashlib.sha256((OUT/'candidate.mgap').read_bytes()).hexdigest(),'cases':{}}
    for address,thumb,reason in [(0x154146,True,7),(0x154162,True,7),(0x15417e,True,7),(0x11004,False,8)]:
        e=Guard();e.run();assert e.stop_reason=='handoff';e.ready=True;e.stop_reason=None
        e.uc.reg_write(UC_ARM_REG_PC,address);e.uc.reg_write(UC_ARM_REG_CPSR,0x30 if thumb else 0x10)
        e.run();report['cases'][hex(address)]=check(e,reason);print('PASS guard',hex(address),flush=True)
    for kind,event in [('message',0x30001),('bjl',0x30007),('xml',0x30007)]:
        e=Managed(candidate=OUT/'candidate.bin',from_reset=True,boot_build=BOOT,
                  eeprom=ROOT/'emu/inputs/eeprom.bin',event=event,kind=kind)
        e.boot();e.run_boot(500,30_000_000)
        if event!=0x30007:
            assert e.fixture_returned and e.error is None and Inspect.audit(e)['full_image_unchanged']
            assert not e.spi_writes and not e.cpu_flash_stores
            report['cases']['unrelated_event']={'returned_to_stock':True,'flash_unchanged':True}
        else:
            report['cases'][kind]=check(e,0xfd)
            report['cases'][kind]['input_fixture']='Actual parser with modeled FIFO; not USB electrical delivery'
            md=json.loads((BOOT/'manifest.json').read_text());md['symbols'].pop('application',None)
            loader=Model(image=e.execution_image,metadata=md)
            loader.uc.mem_write(0x20000000,bytes(e.uc.mem_read(0x20000000,0x800000)))
            loader.uc.mem_write(0xe0000000,bytes(e.uc.mem_read(0xe0000000,0x10000)))
            loader.uc.context_restore(e.uc.context_save());loader.regs.update(e.registers)
            loader.run(lambda x:bool(x.info()) and x.info()[2]==1 and x.info()[3]&1,budget=800_000_000)
            assert not loader.erases and not loader.programs
            report['cases'][kind]['held_rescue_after_real_reset']=True
        print('PASS',kind,hex(event),flush=True)
    data=(OUT/'candidate.mgap').read_bytes();prefix=(BOOT/'boot_region.bin').read_bytes()
    md=json.loads((BOOT/'manifest.json').read_text())
    original=(ROOT/'app/build/application.bin').read_bytes()
    e=Model(image=(prefix+original).ljust(0x200000,b'\xff'),metadata=md,
            actions=[[('stay',setup(0x40,0x40)),('begin',setup(0x40,0x43,len(data)&65535,len(data)>>16))]],bulk=data)
    e.run(lambda x:bool(x.info()) and x.info()[5]==0xc0,budget=500_000_000)
    assert e.info()[5]==0xc0 and bytes(e.uc.mem_read(0x10000,len(data)))==data
    assert bytes(e.uc.mem_read(0,0x10000))==prefix
    assert min(e.erases)>=0x10000 and min(e.programs)>=0x10000 and e.programs[-1]==0x10000
    report['cases']['protected_upload']={'bytes':len(data),'boot_unchanged':True,'header_last':True}
    report['passed']=True
    (OUT/'guard_validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
