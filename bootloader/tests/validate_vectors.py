#!/usr/bin/env python3
"""Exercise real vector wrappers/API instructions and reset-time disarming.

Exception entry is modeled by setting architectural mode/LR/SPSR and starting
at the actual flash vector. No dispatch instruction or C return is substituted.
"""
import hashlib
import json
from pathlib import Path
import struct
from model import Model, BUILD, ROOT, setup
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import *

STATE=0xe0000880
MAGIC=0x3158564d
TABLE=0x20028000
RETURN=0x207fd000
STACK=0x207fe000
REGS=[UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
      UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
      UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11,
      UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_CPSR,
      UC_ARM_REG_SPSR]
MODES=[0xdb,0xd3,0xd7,0xd7,0xd3,0xd2,0xd1]


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    meta=json.loads((BUILD/'manifest.json').read_text())
    image=(BUILD/'factory.bin').read_bytes()
    symbols={k:int(v,16) for k,v in meta['symbols']['stage0'].items()}
    cases={}

    def fresh():
        e=Model(image=image,metadata=meta)
        e.uc.mem_map(0x18000000,0x800000)
        return e

    def call(e,name,arg=0,cpsr=0xd3):
        cpu=e.uc
        cpu.reg_write(UC_ARM_REG_CPSR,cpsr)
        cpu.reg_write(UC_ARM_REG_SP,STACK)
        cpu.reg_write(UC_ARM_REG_R0,arg)
        cpu.reg_write(UC_ARM_REG_LR,RETURN)
        cpu.emu_start(symbols[name],RETURN,count=20000)
        check(cpu.reg_read(UC_ARM_REG_PC)==RETURN,f'{name}: did not return')
        return cpu.reg_read(UC_ARM_REG_R0)

    def install(e,targets=None,cpsr=0xd3,pointer=TABLE):
        if targets is None: targets=[0x10100+4*i for i in range(7)]
        e.uc.mem_write(TABLE,struct.pack('<7I',*targets))
        return call(e,'boot_vectors_install',pointer,cpsr)

    def dispatch(e,index,target,alignment=0):
        cpu=e.uc
        cpu.reg_write(UC_ARM_REG_CPSR,0xa8000000|MODES[index])
        for i,reg in enumerate(REGS[:13]): cpu.reg_write(reg,0x12340000+i*0x101)
        cpu.reg_write(UC_ARM_REG_SP,0xe0000ef0+alignment)
        cpu.reg_write(UC_ARM_REG_LR,0x00123456)
        cpu.reg_write(UC_ARM_REG_SPSR,0x68000030) # Thumb/User return context, Q+flags
        before=[cpu.reg_read(r) for r in REGS]
        writes=[]
        h=cpu.hook_add(UC_HOOK_MEM_WRITE,
            lambda c,a,addr,size,v,u:writes.append((addr,size)),
            begin=0xe0000900,end=0xe0000fff)
        cpu.emu_start(4+index*4,target,count=20000)
        cpu.hook_del(h)
        check(cpu.reg_read(UC_ARM_REG_PC)==target,f'vector {index} did not reach target')
        check([cpu.reg_read(r) for r in REGS]==before,f'vector {index} clobbered state')
        used=before[13]-min(a for a,s in writes)
        check(used<=0x100,f'vector {index} early stack exceeds 256 bytes')
        return used

    def record(name,e,**extra):
        check(bytes(e.uc.mem_read(0,0x10000))==image[:0x10000],'boot bytes changed')
        cases[name]=dict(passed=True,**extra)
        print('PASS',name,flush=True)

    words=struct.unpack_from('<6I',image,0x40)
    check(words==(MAGIC,1,24,STATE,symbols['boot_vectors_install'],symbols['boot_vectors_clear']),
          'published ABI differs from linked symbols')
    for region,base in [('flash',0x10100),('ram',0x20020100)]:
        for alignment in (0,4):
            e=fresh(); targets=[base+4*i for i in range(7)]
            check(install(e,targets)==1,'valid install rejected')
            depths=[dispatch(e,i,t,alignment) for i,t in enumerate(targets)]
            record(f'all_seven_{region}_handlers_sp_mod8_{alignment}',e,max_stack_bytes=max(depths))

    e=fresh();check(install(e,[0]*7)==1,'zero/default entries rejected')
    for i in range(7):dispatch(e,i,symbols['fault'])
    record('all_zero_handlers_use_fault',e)

    bad_targets={'boot_flash':0x100,'boot_ram':0x20010000,'thumb':0x10101,
        'halfword':0x10102,'mmio':0xc0780000,'outside_flash':0x200000,
        'past_payload':0x10020+len((BUILD/'application.bin').read_bytes())-32,
        'past_ram_payload':0x20020000+len((BUILD/'application.bin').read_bytes())-32,
        'wraparound':0xfffffffc,'header':0x10000}
    for name,target in bad_targets.items():
        e=fresh();check(install(e)==1,'baseline registration')
        targets=[0x10100]*7;targets[5]=target
        check(install(e,targets)==0,'bad target accepted: '+name)
        check(int.from_bytes(e.uc.mem_read(STATE,4),'little')==0,'failed install still armed')
        dispatch(e,1,symbols['fault'])
        record('reject_target_'+name,e)

    for name,pointer in [('null',0),('unaligned',TABLE+1),('mmio',0xc0780000),
                         ('ram_edge',0x207ffff0),('sram_edge',0xe0000ff0),
                         ('flash_edge',0x1ffff0)]:
        e=fresh();check(install(e,pointer=pointer)==0,'bad input pointer accepted')
        record('reject_table_'+name,e)
    for name,cpsr in [('irq_enabled',0x53),('fiq_enabled',0x93),('user',0xd0)]:
        e=fresh();check(install(e,cpsr=cpsr)==0,'bad caller state accepted')
        record('reject_caller_'+name,e)

    for name,offset,value in [('magic',0,0),('magic_inverse',4,0),('version',8,2),
                             ('target',12+5*4,0x10104),('inverse',40+5*4,0)]:
        e=fresh();check(install(e)==1,'baseline registration')
        e.uc.mem_write(STATE+offset,struct.pack('<I',value))
        dispatch(e,5,symbols['fault'])
        record('reject_corrupt_'+name,e)
    e=fresh();check(install(e)==1,'baseline registration')
    e.uc.mem_write(STATE+12+5*4,struct.pack('<I',0x100))
    e.uc.mem_write(STATE+40+5*4,struct.pack('<I',0xfffffeff))
    dispatch(e,5,symbols['fault']);record('dispatch_rechecks_target_bounds',e)

    for name,offset,value in [('length_zero',8,0),('length_unaligned',8,5),
                             ('length_overflow',8,0xfffffff0),('magic',0,0),
                             ('version',4,1),('load',12,0x20010000),('entry',16,4),('flags',24,1)]:
        e=fresh();e.uc.mem_write(0x10000+offset,struct.pack('<I',value))
        check(install(e)==0,'bad app header accepted')
        record('reject_header_'+name,e)
    e=fresh();check(install(e)==1,'baseline registration')
    call(e,'boot_vectors_clear')
    check(bytes(e.uc.mem_read(STATE,68))==bytes(68),'clear did not wipe table')
    dispatch(e,1,symbols['fault']);record('clear_disables_and_wipes',e)

    for invalid in (False,True):
        e=fresh();check(install(e)==1,'baseline registration')
        if invalid:e.uc.mem_write(0x10000,b'BAD!')
        # Model the same chip reset/warm entry, retaining poisoned SRAM and
        # prior exception banks. The real reset entry must disarm forwarding.
        if invalid:
            e.run(lambda m: bool(m.info()) and bool(m.info()[3]&1))
        else:
            e.run(lambda m: 0x20020000 in m.entered)
        check(int.from_bytes(e.uc.mem_read(STATE,4),'little')==0,'reset retained registration')
        dispatch(e,1,symbols['fault'])
        record('reset_disarms_'+('invalid_app_rescue' if invalid else 'valid_app'),e)

    # Keep the full current source app as a backwards-compatibility check.
    app_path=ROOT.parent/'app/build/application.bin'
    app_meta=ROOT.parent/'app/build/manifest.json'
    if app_path.exists() and app_meta.exists():
        app=app_path.read_bytes()
        combined=(image[:0x10000]+app).ljust(0x200000,b'\xff')
        metadata=json.loads(json.dumps(meta))
        metadata['symbols']['application']=json.loads(app_meta.read_text())['symbols']['application']
        e=Model(image=combined,metadata=metadata)
        e.run(lambda m: bool(m.info()) and m.info()[2]==2)
        e.requests.appendleft(('rescue',setup(0x40,0x41,1)))
        e.run(lambda m: m.resets==2 and bool(m.info()) and m.info()[2]==1 and bool(m.info()[3]&1),resume=True)
        record('current_source_app_boot_and_management_reboot',e,
               application_sha256=hashlib.sha256(app).hexdigest())
    else:
        raise AssertionError('current app build required for compatibility test')

    report=dict(all_assertions_passed=True,cases=cases,
        stage0_sha256=hashlib.sha256(image[:0x4000]).hexdigest(),
        factory_sha256=hashlib.sha256(image).hexdigest(),
        model='Real ARM wrappers and C validator; architectural exception entry supplied by harness')
    (BUILD/'vectors_validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f'ALL {len(cases)} VECTOR CASES PASSED',flush=True)


if __name__=='__main__':main()
