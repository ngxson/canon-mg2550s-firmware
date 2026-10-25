#!/usr/bin/env python3
"""Execute a hash-pinned diagnostic app through real vectors and rescue reset."""
import argparse,hashlib,json,struct,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
BOOT=ROOT/'bootloader/build/vectors'
sys.path[:0]=[str(ROOT/'emu/stockapp'),str(ROOT/'bootloader/tests')]
from probe import Boot
from model import Model
from unicorn.arm_const import UC_ARM_REG_PC,UC_ARM_REG_CPSR,UC_ARM_REG_LR

class Checkpoint(Boot):
    def code(self,cpu,address,size,user):
        if address==0:
            self.stop_reason='checkpoint reset';cpu.emu_stop();return
        super().code(cpu,address,size,user)

def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args()
    out=a.directory.resolve();m=json.loads((out/'manifest.json').read_text())
    data=(out/'candidate.mgap').read_bytes()
    assert m['runtime'] and (m['checkpoint'] or m.get('irq_snapshot')) and hashlib.sha256(data).hexdigest()==m['package_sha256']
    e=Checkpoint(candidate=out/'candidate.bin',from_reset=True,boot_build=BOOT,eeprom=ROOT/'emu/inputs/eeprom.bin')
    e.boot();e.run_boot(max(1000,m.get('irq_snapshot',0)+1),30_000_000)
    if e.error is not None or e.stop_reason!='checkpoint reset':
        failure={'error':e.error,'stop':e.stop_reason,'next_wall':getattr(e,'next_wall',None),
                 'pc':hex(e.uc.reg_read(UC_ARM_REG_PC)),'lr':hex(e.uc.reg_read(UC_ARM_REG_LR)),
                 'cpsr':hex(e.uc.reg_read(UC_ARM_REG_CPSR)),
                 'irqs':e.irqs,'svcs':e.svcs,'record':bytes(e.uc.mem_read(0xe00008d0,36)).hex(),
                 'recent':list(e.mmio_tail)[-8:]}
        (out/'checkpoint_failure.json').write_text(json.dumps(failure,indent=2)+'\n')
        raise AssertionError(failure)
    rec=list(struct.unpack('<8I',e.uc.mem_read(0xe00008d0,32)))
    reason=0xfc if m.get('irq_snapshot') else 0xfe
    assert rec[:6]==[0x31504d53,0xceafb2ac,2,0xfffffffd,reason,reason^0xffffffff],rec
    if m['checkpoint']:assert rec[6]==int(m['checkpoint']['address'],16)+2,rec
    else:assert e.irqs==m['irq_snapshot']
    assert not e.spi_writes and not e.cpu_flash_stores
    audit=e.audit();assert audit['full_image_unchanged'] and audit['registration_intact']
    if m.get('native_startup'):
        assert sum(x['bytes'] for x in audit['native_ram_clears'])==0x800000
    md=json.loads((BOOT/'manifest.json').read_text());md['symbols'].pop('application',None)
    loader=Model(image=e.execution_image,metadata=md)
    loader.uc.mem_write(0x20000000,bytes(e.uc.mem_read(0x20000000,0x800000)))
    loader.uc.mem_write(0xe0000000,bytes(e.uc.mem_read(0xe0000000,0x10000)))
    loader.uc.context_restore(e.uc.context_save());loader.regs.update(e.registers)
    loader.run(lambda x:bool(x.info()) and x.info()[2]==1 and x.info()[3]&1,budget=800_000_000)
    assert not loader.erases and not loader.programs
    assert bytes(loader.uc.mem_read(0xe00008d0,32))==struct.pack('<8I',*rec)
    report={'package_sha256':m['package_sha256'],'checkpoint':m['checkpoint'],
            'irq_snapshot':m.get('irq_snapshot',0),
            'record':[hex(x) for x in rec],'native_cpu':m.get('native_cpu',False),
            'native_startup':m.get('native_startup',False),'native_ram_clears':audit['native_ram_clears'],
            'CP15_instructions':audit['cp15'],'irqs':e.irqs,'svcs':e.svcs,
            'held_rescue':list(loader.info()),'flash_unchanged':True,'passed':True,
            'limits':'Synthetic peripheral readiness/IRQ timing; cache not modeled; native-startup DRAM aliases and memory configuration inputs are explicit assumptions'}
    (out/'checkpoint_validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2),flush=True)

if __name__=='__main__':main()
