#!/usr/bin/env python3
"""Native startup and one-shot Power test, with explicit USB engine inputs.

This proves init/attach and recovery invariants, not USB enumeration. The
microcode engine is given its startup response and TX-ready bit; its instruction
set and later DMA completions remain unmodeled. VBUS is modeled connected.
"""
import argparse,collections,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'emu/stockapp'),str(ROOT/'emu')]
from probe import Inspect
from stock.usbhost import UsbHost
from stock import startup
from unicorn.arm_const import UC_ARM_REG_R1

class StartupHost(Inspect,UsbHost):
    def __init__(self,**kw):
        self.engine_boot=collections.deque();self.usb_errors=[];self.last_bus_tick=-10
        super().__init__(**kw)
    def read(self,cpu,off,size,user):
        address=startup.PERI+off
        if address==0xc075008c:return self.engine_boot.popleft() if self.engine_boot else 0
        if address==0xc0750080:return 0x80000000
        if address==0xc0c83e18:return 1
        return super().read(cpu,off,size,user)
    def write(self,cpu,off,size,value,user):
        address=startup.PERI+off;previous=self.registers.get(address,0)
        super().write(cpu,off,size,value,user)
        # Stock uploads microcode, releases reset, then expects this FIFO token
        # at 0x4b750. This is a synthetic engine response, not microcode execution.
        if address==0xc0750000 and previous==1 and value==0 and 0xc077c000 in self.registers:
            self.engine_boot.append(0x80000001)
    def host_step(self):
        if self.bus_stage<2:
            bit=(0x1000,0x2000)[self.bus_stage]
            if not self.reg(0x18)&bit or self.ticks<self.last_bus_tick+5:return
            before=self.bus_stage;super().host_step()
            if self.bus_stage!=before:self.last_bus_tick=self.ticks
        else:super().host_step()
    def code(self,cpu,address,size,user):
        if address==0x500cc:self.usb_errors.append({'code':cpu.reg_read(UC_ARM_REG_R1),'tick':self.ticks})
        super().code(cpu,address,size,user)

def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args()
    out=a.directory.resolve();m=json.loads((out/'manifest.json').read_text())
    assert m['native_startup'] and m['auto_power'] and not m['irq_snapshot'] and not m['checkpoint']
    e=StartupHost(candidate=out/'candidate.bin',from_reset=True,boot_build=ROOT/'bootloader/build/vectors',
                  eeprom=ROOT/'emu/inputs/eeprom.bin',usb_source=67)
    e.boot();r=e.run_usb(450,15_000_000);audit=e.audit()
    assert e.error is None and r['result']['stop_reason']=='tick limit at idle',r['result']
    assert r['ticks']==450 and all(r['input_files_unchanged'].values())
    assert not e.spi_writes and not e.cpu_flash_stores and not r['faults']
    assert audit['registration_intact'] and audit['protected_prefix_unchanged'] and audit['full_image_unchanged']
    assert sum(x['bytes'] for x in e.native_ram_clears)==0x800000
    assert int.from_bytes(e.uc.mem_read(0xe00008f4,4),'little')==84
    assert e.pcs[0x50144]==1 and e.pcs[0x4a1d4]==1 and e.pcs[0x509e8]==1
    assert e.pcs[0x113c7c]==0 and not e.usb_errors
    state=list(e.uc.mem_read(0x1804099c,4))
    assert state[2:]==[1,1] and e.connected
    assert e.registers[0xc0c88024]&0x10000 and not e.registers[0xc0780804]&2
    assert all(x['target_executed'] for x in r['context_restores'])
    report={'passed':True,'package_sha256':m['package_sha256'],'candidate_sha256':m['candidate_sha256'],
            'ticks':r['ticks'],'irqs':e.irqs,'usb_state':state,'usb_errors':e.usb_errors,
            'native_usb_initializations':e.pcs[0x50144],'native_usb_attachments':e.pcs[0x509e8],
            'power_pulse_samples':20,'counter_saturated_at':84,'boot_unchanged':True,
            'no_flash_or_spi_writes':True,'usb_electrical_enumeration_proven':False,
            'usb_host_events':r['usb']['events'],
            'limits':['Synthetic connected VBUS, microcode startup FIFO token and TX readiness.',
                      'USB reset reaches stock; later engine/global-NAK completions are not fully modeled.',
                      'Synthetic IRQ/peripheral/EEPROM inputs and DRAM aliases; cache timing unmodeled.'],
            'test_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (out/'startup_validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
if __name__=='__main__':main()
