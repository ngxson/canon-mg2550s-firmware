#!/usr/bin/env python3
"""Run the actual app through homing and fault paths behind the production bootloader.

Position inputs are scripted, not a physical motor/encoder simulation. They
exercise sequencing, polarity, deadlines, rejection of false home, and recovery.
Clock advances 0.05 modeled ms/read; no claim about electrical drive is made.
"""
import hashlib,json,struct,sys
from pathlib import Path
APP=Path(__file__).resolve().parents[1];BOOT=APP.parent/'bootloader'
sys.path.insert(0,str(BOOT/'tests'))
from model import Model,checked,setup,TICKS_PER_MS
P,C,KEY,CLOCK=0xc0c57000,0xc0c56000,0xc0c8d028,0xc0c82008

class HomeModel(Model):
    def __init__(self,**kw):
        self.keys=0;self.behavior='normal';self.drives=[];self.drive=None
        self.hard_end=0x700000-4096;self.serial_stuck=False;self.clock_jump=False
        self.writes=[];self.first_arm=None
        super().__init__(**kw)
    @checked
    def read(self,cpu,offset,size,user):
        a=0xc0000000+offset
        if a==CLOCK and self.stage=='application':
            step=TICKS_PER_MS//20
            if self.clock_jump and self.drive:step=8*TICKS_PER_MS;self.clock_jump=False
            self.counter=(self.counter+step)&0xffffffff;return self.counter & 0x1fffffff
        if a==KEY:return self.keys
        if a==0xc0c50218 and self.serial_stuck:return 1
        if self.drive:
            d=self.drive;age=((self.counter-d['start'])&0xffffffff)/TICKS_PER_MS
            distance=int(age*(8 if self.behavior=='keeps_moving' else 32))
            if a==P+0x100:
                x=d['x0'];direction=d['direction'];number=len(self.drives)
                if self.behavior=='frozen':pass
                elif self.behavior=='wrong':x+=distance
                elif direction:
                    end=(0 if self.behavior=='keeps_moving' else self.hard_end)+(
                        5000 if self.behavior=='wrong_return_end' and number==3 else 0)
                    x=max(end,x-distance)
                else:
                    if self.behavior=='backoff_stuck':distance=0
                    x=min(d['target'],x+distance)
                self.regs[a]=x;return x
            if a==P+0x304:return 4 if d['direction'] else 0
            if a==P+0x200:return 0xffffff # invalid period; explicit zero-speed input
            if a==C+0x104:
                if self.behavior=='early_completion' and len(self.drives)==2:return 1
                return int(not d['direction'] and self.regs.get(P+0x100)==d['target'])
        return super().read(cpu,offset,size,user)
    @checked
    def write(self,cpu,offset,size,value,user):
        super().write(cpu,offset,size,value,user);a=0xc0000000+offset
        if self.stage!='application':return
        self.writes.append((a,value))
        if a in (0xc0c50000,0xc0c52000):assert value==0,'other motor enabled'
        if a==C and value==0x102:
            assert self.drive is None
            if self.first_arm is None:self.first_arm=dict(self.regs)
            x=self.regs[P+0x100];target=self.regs[P+0x120]
            self.drive={'x0':x,'target':target,'direction':int(target<x),
                        'start':self.counter,'values':[]}
            self.drives.append(self.drive)
        if a==C+8 and self.drive:self.drive['values'].append(value)
        if a==C and value==0 and self.drive:
            self.drive['duration_ms']=((self.counter-self.drive['start'])&0xffffffff)/TICKS_PER_MS
            self.drive=None

def main():
    boot=(BOOT/'build/boot_region.bin').read_bytes();app=(APP/'build/application.bin').read_bytes()
    symbols=json.loads((APP/'build/manifest.json').read_text())['symbols']['application']
    meta={'layout':{'version':2,'application':[0x10000,0x200000]},'symbols':{
        'bootloader':json.loads((BOOT/'build/manifest.json').read_text())['symbols']['bootloader'],
        'application':symbols}}
    # This ARM EABI toolchain stores these small enum objects in one byte.
    # Every finished result is also checked through the public 32-bit USB reply.
    def state(e):return e.uc.mem_read(int(symbols['home_result'],16),1)[0]
    def booted():
        e=HomeModel(image=(boot+app).ljust(0x200000,b'\xff'),metadata=meta)
        e.run(lambda e:bool(e.info()) and e.info()[2]==2)
        return e
    def start(e,button=False):
        if button:
            end=e.counter+20*TICKS_PER_MS;e.run(lambda e:e.counter>=end,resume=True)
            e.keys=0x1000
        else:e.requests.appendleft(('home',setup(0x40,0x74)))
        e.run(lambda e:e.drive is not None,resume=True)
    report={'assumptions':__doc__,'app_sha256':hashlib.sha256(app).hexdigest(),'cases':{}}
    def record(name,e,expected=None):
        assert not e.erases and not e.programs
        assert bytes(e.uc.mem_read(0,0x10000))==boot
        assert e.drive is None and e.regs[C]==0 and e.regs[C+8]==2000
        if expected is not None:
            assert state(e)==expected,(name,state(e))
            e.requests.appendleft(('home_status',setup(0xc0,0x75,length=32)))
            e.run(lambda e:any(n=='home_status' for _,n,_ in e.responses),resume=True)
            data=next(d for _,n,d in e.responses if n=='home_status')
            assert struct.unpack('<8I',data)[:3]==(0x4d48474d,1,expected)
        assert all(d['duration_ms']<=22000.5 for d in e.drives)
        report['cases'][name]={'result':expected,'drives':e.drives,'protected_prefix_unchanged':True}
        print('PASS',name,flush=True)
    for behavior in ('normal','frozen','backoff_stuck','wrong','wrong_return_end','early_completion'):
        print('RUN',behavior,flush=True)
        e=booted();e.behavior=behavior;start(e)
        try:
            e.run(lambda e:state(e) in (2,3,4),resume=True,budget=100000000)
        except AssertionError:
            print('DEBUG',state(e),'counter',e.counter,'drive_count',len(e.drives),
                  {n:int.from_bytes(e.uc.mem_read(int(symbols[n],16),4),'little')
                   for n in ('phase','started','last_ms','window_ms','window_progress')},flush=True)
            raise
        if behavior=='normal':
            assert [d['direction'] for d in e.drives]==[1,0,1]
            assert min(e.drives[0]['values'])==1400
            assert all(v<=2000 for d in (e.drives[0],e.drives[2]) for v in d['values'])
            assert all(v>=2000 for v in e.drives[1]['values'])
        record(behavior,e,2 if behavior=='normal' else 3)
    e=booted();e.hard_end=0x700000;start(e)
    e.run(lambda e:state(e) in (2,3,4),resume=True,budget=100000000)
    record('already_at_home_requires_backoff_and_repeat',e,2)
    e=booted();e.counter=(1<<29)-500*TICKS_PER_MS;start(e)
    e.run(lambda e:state(e) in (2,3,4),resume=True,budget=100000000)
    record('raw_counter_rollover_during_home',e,2)
    e=booted();e.behavior='keeps_moving';start(e)
    e.run(lambda e:state(e)==3,resume=True,budget=300000000)
    assert 22000<=e.drives[0]['duration_ms']<22001
    record('independent_seek_deadline',e,3)
    for how in ('button','usb','delayed_tick'):
        e=booted();start(e)
        if how=='button':e.keys=0x20
        elif how=='usb':e.requests.appendleft(('cancel',setup(0x40,0x76)))
        else:e.clock_jump=True
        e.run(lambda e:state(e) in (2,3,4),resume=True)
        assert e.drives[0]['duration_ms']<12
        record('stop_'+how,e,3 if how=='delayed_tick' else 4)
    e=booted();e.serial_stuck=True;e.requests.appendleft(('home',setup(0x40,0x74)))
    e.run(lambda e:state(e)==3,resume=True);assert not e.drives
    record('serial_failure_no_drive',e,3)
    e=booted();start(e)
    e.requests.appendleft(('info_during_home',setup(0xc0,0x42,length=32)))
    e.run(lambda e:any(n=='info_during_home' for _,n,_ in e.responses),resume=True)
    assert e.drive is not None
    e.requests.appendleft(('reboot_boot',setup(0x40,0x41,1)))
    e.run(lambda e:e.resets==2 and bool(e.info()) and e.info()[2]==1 and e.info()[3]&1,resume=True)
    record('live_usb_info_and_reboot',e)
    e=booted();e.behavior='frozen';start(e)
    e.run(lambda e:state(e)==3,resume=True,budget=100000000)
    e.command=3
    e.run(lambda e:e.resets==2 and bool(e.info()) and e.info()[2]==1 and e.info()[3]&1,resume=True)
    assert 0xc0 in e.results;record('i2c_rescue_after_failed_home',e)
    report['all_assertions_passed']=True
    (APP/'build/validation_home.json').write_text(json.dumps(report,indent=2)+'\n')
    print('ALL HOME ASSERTIONS PASSED',flush=True)
if __name__=='__main__':main()
