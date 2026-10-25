"""Execute stock 0xaac50 on the geometry struct with the ESP32 job's values."""
import struct, sys
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_MEM_WRITE, UC_HOOK_CODE
from unicorn.arm_const import *
img=open('/project/emu/output/stockapp/telemetry_v3/candidate.bin','rb').read()
BASE=0x1806c93c; STOP=0x1fff00
def run(y,h,paper_h,res=600):
    u=Uc(UC_ARCH_ARM,UC_MODE_ARM); u.mem_map(0,0x200000); u.mem_write(0,img); u.mem_map(0x18000000,0x100000)
    u.mem_write(BASE+0x34,struct.pack('<H',res)); u.mem_write(BASE+0x3a,struct.pack('<h',y))
    u.mem_write(BASE+0x3e,struct.pack('<H',h)); u.mem_write(0x1806c97e,struct.pack('<H',paper_h))
    writes=[]
    u.hook_add(UC_HOOK_MEM_WRITE,lambda uc,a,addr,size,val,ud: writes.append((hex(addr),size,val)) if 0x18000000<=addr<0x18100000 else None)
    u.hook_add(UC_HOOK_CODE,lambda uc,addr,size,ud: uc.emu_stop() if addr==STOP else None)
    u.reg_write(UC_ARM_REG_CPSR,0x30); u.reg_write(UC_ARM_REG_SP,0x180f0000); u.reg_write(UC_ARM_REG_LR,STOP|1)
    trace=[]
    u.hook_add(UC_HOOK_CODE,lambda uc,addr,size,ud: trace.append(hex(addr)))
    try: u.emu_start(0xaac51,0,count=200)
    except Exception as e: print('fault',e,'pc',hex(u.reg_read(UC_ARM_REG_PC)),'r0',hex(u.reg_read(UC_ARM_REG_R0)),'r1',hex(u.reg_read(UC_ARM_REG_R1)),'r2',hex(u.reg_read(UC_ARM_REG_R2)),'sp',hex(u.reg_read(UC_ARM_REG_SP)),'trace',trace[-6:])
    return struct.unpack('<H',u.mem_read(BASE+0x3e,2))[0], writes
for y,h,ph in ((75,6816,7016),(75,6545,7016),(75,6000,7016),(75,6816,6600),(0,7016,7016),(59,6484,6600)):
    out,w=run(y,h,ph); print(f'y={y} h={h} paper_h={ph} -> active_h={out} (paper_h-y-396={ph-y-396})  writes={w}')
