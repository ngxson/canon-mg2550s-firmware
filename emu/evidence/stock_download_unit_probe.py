#!/usr/bin/env python3
"""Offline selector/decoder coverage and reference inventory for the stock dump.

Requires --harness pointing to the historical fw_dump/gpt analysis tools.
No physical USB/EEPROM access, and no firmware instruction modifications.
The initial-data pointer inventory includes the compressed scatter-load data.
Raw reference candidates are not a complete indirect-control-flow proof.
"""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--harness', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--disassembly', type=Path)
    args = p.parse_args()
    sys.path.insert(0,str(args.harness.resolve()))
    import fwtool as f
    import usb_update_tool as u
    import unpack_data
    from emulate_usb_update import Updater, MODE_OBJECT
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM
    from unicorn.arm_const import (UC_ARM_REG_CPSR, UC_ARM_REG_SP,
        UC_ARM_REG_LR, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
        UC_ARM_REG_PC, UC_CPU_ARM_946)

    class Selector(Updater):
        def __init__(self, samples):
            super().__init__()
            self.samples = samples
            self.panel_reads = 0
        def read(self,cpu,offset,size,user):
            if offset+0xc0000000 == 0xc0c8d028:
                i=self.panel_reads
                self.panel_reads+=1
                return 8 | (self.samples[i] if i<len(self.samples) else 0)
            return super().read(cpu,offset,size,user)

    patterns = {'none': ([],1), 'cancel_release': ([0x20]*128,3),
        'cancel_power_release': ([0x20]*128+[0x60]*16+[0x20]*16,4),
        'both_initially': ([0x60]*128,1)}
    modes=[]
    for marker in (0,0x55,0x5a,0xa5):
        for latch in (0,1,2,3):
            for name,(samples,panel_mode) in patterns.items():
                e=Selector(samples);e.eeprom[9]=marker
                mode=e.call(0x20003440,MODE_OBJECT,latch)
                expected=2 if marker==0xa5 else {1:6,2:7}.get(latch,panel_mode)
                assert mode==expected,(marker,latch,name,mode,expected)
                assert not e.erases and not e.programs and not e.eeprom_writes
                assert bytes(e.uc.mem_read(0,len(f.data)))==f.data
                modes.append(dict(marker=hex(marker),latch=latch,panel=name,
                    mode=mode,flags=bytes(e.uc.mem_read(MODE_OBJECT+0x14,2)).hex(),
                    panel_reads=e.panel_reads))

    # Pure stock functions: actual instructions, synthetic argument objects.
    cpu=Uc(UC_ARCH_ARM,UC_MODE_ARM);cpu.ctl_set_cpu_model(UC_CPU_ARM_946)
    cpu.mem_map(0,0x200000);cpu.mem_write(0,f.data)
    cpu.mem_map(0x10000000,0x100000)
    def call(address,*values):
        cpu.reg_write(UC_ARM_REG_CPSR,0x3f)
        cpu.reg_write(UC_ARM_REG_SP,0x100ff000)
        cpu.reg_write(UC_ARM_REG_LR,0x100fe001)
        for r,v in zip((UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2),values):cpu.reg_write(r,v)
        cpu.emu_start(address|1,0x100fe000,count=100000)
        assert cpu.reg_read(UC_ARM_REG_PC)==0x100fe000
        return cpu.reg_read(UC_ARM_REG_R0)
    reasons=[]
    for low in range(13):
        cpu.mem_write(0x10001000,bytes(64))
        call(0x13792,0x10001000,0x30000+low,2)
        parameters=bytes(cpu.mem_read(0x10001004,5))
        if low==7:assert parameters[:3]==bytes([3,6,2])
        reasons.append(dict(event=hex(0x30000+low),parameters=parameters.hex()))
    bjl=[]
    for command,value,expected in ((b'@FROMUPMODE',b'',0x501),
                                  (b'@FROMUPMODE',b'1',0),
                                  (b'@BOGUS',b'',0)):
        cpu.mem_write(0x10002000,command+b'\0')
        cpu.mem_write(0x10002100,value+b'\0')
        result=call(0x238f8,0x10002000,0x10002100,0x156a70)
        assert result==expected
        bjl.append(dict(command=command.decode(),argument=value.decode(),result=hex(result)))

    targets={0x110,0x1838,0x1810,0x1818,0x1826,0x154146,0x154162,
             0x15417e,0x20003440,0x2000351c,0xe0000c00,0x30007}
    capture=io.StringIO()
    with contextlib.redirect_stdout(capture):f.refs(targets)
    base,initial,_,_=unpack_data.unpack()
    # Include unaligned embedded words and Thumb tail branches missed by refs().
    word_refs=[]
    for area,b,data in [('flash',0,f.data),('initial_data',base,initial)]:
        for pos in range(len(data)-3):
            value=struct.unpack_from('<I',data,pos)[0]
            if value in targets or value&~1 in targets:
                word_refs.append(dict(area=area,address=hex(b+pos),value=hex(value)))
    short_refs=[]
    for a in range(0,len(f.data)-1,2):
        h=struct.unpack_from('<H',f.data,a)[0]
        if h&0xf800==0xe000:
            off=h&0x7ff;off=off-0x800 if off&0x400 else off
            dest=a+4+off*2
            if dest in targets:short_refs.append(dict(source=hex(a),target=hex(dest)))
    result=dict(firmware_sha256=hashlib.sha256(f.data).hexdigest(),
        assertions_passed=True,selector_cases=modes,reset_reason_cases=reasons,
        bjl_lookup_cases=bjl,reference_candidates=capture.getvalue().splitlines(),
        embedded_word_candidates=word_refs,thumb_short_branch_candidates=short_refs,
        scope='Synthetic MMIO selector tests and pure argument-object function tests. No physical entry or end-to-end USB proof.')
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    if args.disassembly:
        ranges=[(0x10c,0xb8,'arm'),(0x1810,0x74,'thumb'),
            (0x118c0,0x50,'arm'),(0x154146,0x50,'thumb'),
            (0x1fe8c,0x54,'thumb'),(0x23476,0x1e,'thumb'),
            (0x236da,0x9a,'thumb'),(0x238f8,0x76,'thumb'),
            (0x24044,0xd0,'thumb'),(0x958c6,0x12c,'thumb'),
            (0x30c66,0x28,'thumb'),(0x136fe,0x94,'thumb'),
            (0x149a4,0xac,'thumb'),(0x147b0,0x66,'thumb'),
            (0x114618,0x34,'thumb'),(0x114eb0,0x1a,'thumb'),
            (0x113fe8,0x32,'thumb'),(0x113ed6,0x30,'thumb')]
        text='Stock firmware SHA256 '+result['firmware_sha256']+'\n'
        text+='Literal pools and switch data in these ranges are not instructions.\n\n'
        text+='\n\n'.join(f.dis(a,n,m) for a,n,m in ranges)
        text+='\n\n'+'\n\n'.join(u.dis(a,n) for a,n in
            [(0x20001d9c,0xe4),(0x20003440,0xdc),(0x20003224,0x78),
             (0x2000351c,0x110),(0x200069f0,0x1c)])
        args.disassembly.write_text(text+'\n')
    print(json.dumps(dict(assertions_passed=True,selector_cases=len(modes),
                         reset_reason_cases=len(reasons),bjl_cases=len(bjl))))


if __name__=='__main__':main()
