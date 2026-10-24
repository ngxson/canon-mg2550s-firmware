#!/usr/bin/env python3
"""Differentially execute the replacement C and unpatched stock controller.

No hardware/motor model: checks PI arithmetic, anti-windup, clamps, reflected
commands and acceleration. Explicit RAM objects isolate stock f8682/f8772.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from unicorn import Uc,UC_ARCH_ARM,UC_MODE_ARM
from unicorn.arm_const import *

APP=Path(__file__).resolve().parents[1]
RET=0x10000000; M=0x18000000; A=M+0x1c; STATE=0x18001000; STACK=0x1800f000
R=(UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stock',type=Path,required=True)
    args=p.parse_args()
    stock=args.stock.read_bytes(); app=(APP/'build/application.bin').read_bytes()
    assert hashlib.sha256(stock).hexdigest()=='4b032367cff5976aeb426c8723e08d3c431a38e622e245a614791a3e5067e533'
    symbols=json.loads((APP/'build/manifest.json').read_text())['symbols']['application']
    cpu=Uc(UC_ARCH_ARM,UC_MODE_ARM);cpu.ctl_set_cpu_model(UC_CPU_ARM_946)
    for address,size in ((0,0x200000),(0x18000000,0x10000),(0x20020000,0x20000),(RET,0x1000)):
        cpu.mem_map(address,size)
    cpu.mem_write(0,stock);cpu.mem_write(0x20020000,app[32:])
    writes=[]
    cpu.mmio_map(0xc0000000,0x10000000,lambda *a:0,None,
                 lambda uc,o,n,v,u:writes.append((0xc0000000+o,v)),None)
    def word(a,v):cpu.mem_write(a,struct.pack('<I',v&0xffffffff))
    def call(entry,args=(),end=RET,thumb=False,extra=()):
        cpu.reg_write(UC_ARM_REG_CPSR,0x13);cpu.reg_write(UC_ARM_REG_SP,STACK)
        cpu.reg_write(UC_ARM_REG_LR,RET)
        for r,v in zip(R,args):cpu.reg_write(r,v&0xffffffff)
        for i,v in enumerate(args[4:]):word(STACK+i*4,v)
        for r,v in extra:cpu.reg_write(r,v)
        cpu.emu_start(entry|int(thumb),end,count=20000)
        assert cpu.reg_read(UC_ARM_REG_PC)==end
        return cpu.reg_read(UC_ARM_REG_R0)
    def new(name,*args):return call(int(symbols[name],16),args)
    cases=0;examples={}
    for backoff in (0,1):
        n=70 if backoff else 50;lo=1200 if backoff else 400;hi=20000 if backoff else 28000
        for tick in range(n+1):
            old=call(0xf76ce,(lo,hi,n,tick,0),thumb=True)
            assert new('motor_control_ramp',tick,backoff)==old,(tick,old)
            cases+=1
        for direction in (0,1):
            for scenario in ('frozen','moving','overspeed_then_stop'):
                cpu.mem_write(M,b'\0'*0x400);word(M+8,M+0x300);word(M+0x300,0xc0c56000)
                cpu.mem_write(A,bytes([direction]))
                coeff=(315,1000,1000 if backoff else 500,10,0,100,5000,0 if backoff else 3500)
                cpu.mem_write(A+0x8c,struct.pack('<8H',*coeff))
                cpu.mem_write(A+0xc4,struct.pack('<h',5000));new('motor_control_reset',STATE)
                values=[]
                for tick in range(200):
                    wanted=new('motor_control_ramp',tick,backoff)
                    measured=(0 if scenario=='frozen' else min(wanted,tick*300)
                              if scenario=='moving' else 35000 if 40<=tick<100 else 0)
                    word(A+0xb0,wanted);word(A+0xa8,measured)
                    call(0xf8682,(M,A),thumb=True)
                    writes.clear()
                    call(0xf8772,end=0xf87d2,thumb=True,extra=((UC_ARM_REG_R4,M),(UC_ARM_REG_R5,A)))
                    result=new('motor_control_step',STATE,wanted,measured,backoff,direction)
                    old_i,old_c=struct.unpack('<ii',cpu.mem_read(A+0xbc,8))
                    new_i,new_c,new_u,new_w=struct.unpack('<iihB',cpu.mem_read(STATE,11))
                    old_u,old_w=struct.unpack('<hB',cpu.mem_read(A+0xc4,3))
                    assert (new_i,new_c,new_u,new_w)==(old_i,old_c,old_u,old_w)
                    assert writes==[(0xc0c56008,result)],writes
                    values.append(result);cases+=1
                examples[f'{backoff}/{direction}/{scenario}']=values[:20]
    assert bytes(cpu.mem_read(0,len(stock)))==stock
    report={'assumptions':__doc__,'app_sha256':hashlib.sha256(app).hexdigest(),
            'stock_sha256':hashlib.sha256(stock).hexdigest(),'cases':cases,
            'first_20_commands':examples,'all_assertions_passed':True}
    (APP/'build/validation_home_controller.json').write_text(json.dumps(report,indent=2)+'\n')
    print('PASS',cases,'stock/C controller comparisons; firmware unmodified')
if __name__=='__main__':main()
