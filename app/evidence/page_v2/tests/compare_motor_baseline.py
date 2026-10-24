#!/usr/bin/env python3
"""Compare complete motor MMIO writes against an independently saved app image.

Executes each ARM image behind the production bootloader. Inert motor MMIO,
frozen position and a modeled counter; this is not proof of physical motion.
The baseline image must be the user's pre-takeover forward-working build.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
APP=Path(__file__).resolve().parents[1]
BOOT=APP.parent/'bootloader'
sys.path.insert(0,str(BOOT/'tests'))
from model import Model,checked,setup,TICKS_PER_MS
P,C,CLOCK=0xc0c57000,0xc0c56000,0xc0c82008
BASE_HASH='ea0972750ec576445a99822ee8deec8b823548ae437a365960a1261735084919'

def selected(a):
    return (0xc0c41000<=a<0xc0c41420 or 0xc0c50000<=a<0xc0c58000
            or a in (0xc0c85604,0xc0c85624,0xc0c81090,0xc0140400))

class Trace(Model):
    def __init__(self,**kwargs):
        self.writes=[];self.drives=[];self.drive=None
        super().__init__(**kwargs)
    def observe(self,cpu,address,size,user):
        # The original idle loop never reads this counter. Advance modeled
        # idle time at service_poll too; otherwise a timed host wait cannot
        # finish. During a pulse, both images use only counter-read ticks.
        if self.stage=='application' and self.drive is None:
            self.counter=(self.counter+TICKS_PER_MS//20)&0xffffffff
        super().observe(cpu,address,size,user)
    def host_step(self):
        # Drain logs while idle. Repeated INFO on the old app recalculates its
        # entire flash CRC, exhausting the instruction budget between ticks.
        if self.stage=='application' and self.configured and self.info() and not self.requests:
            self.requests.append(('idle_log',setup(0xc0,0x50,length=256)))
        super().host_step()
    @checked
    def read(self,cpu,offset,size,user):
        if offset+0xc0000000==CLOCK and self.stage=='application':
            self.ticks+=1;self.counter=(self.counter+TICKS_PER_MS//20)&0xffffffff
            return self.counter
        return super().read(cpu,offset,size,user)
    @checked
    def write(self,cpu,offset,size,value,user):
        super().write(cpu,offset,size,value,user)
        a=offset+0xc0000000
        if self.stage!='application' or not selected(a):return
        self.writes.append([a,value])
        if a==C and value==0x102:
            assert self.drive is None
            self.drive={'start':self.counter,'waveform':[]};self.drives.append(self.drive)
        if a==C+8 and self.drive is not None:
            self.drive['waveform'].append([round((self.counter-self.drive['start'])/TICKS_PER_MS,4),value])
        if a==C and value==0 and self.drive is not None:
            self.drive['duration_ms']=round((self.counter-self.drive['start'])/TICKS_PER_MS,4)
            self.drive=None

def run(build,direction):
    boot=(BOOT/'build/boot_region.bin').read_bytes();app=(build/'application.bin').read_bytes()
    metadata={'layout':{'version':2,'application':[0x10000,0x200000]},'symbols':{
        'bootloader':json.loads((BOOT/'build/manifest.json').read_text())['symbols']['bootloader'],
        'application':json.loads((build/'manifest.json').read_text())['symbols']['application']}}
    e=Trace(image=(boot+app).ljust(0x200000,b'\xff'),metadata=metadata)
    e.run(lambda e:bool(e.info()) and e.info()[2]==2)
    print('Booted',build,'first direction',direction,flush=True)
    def pump(ms):
        end=e.counter+int(ms*TICKS_PER_MS)
        try:
            e.run(lambda e:e.counter>=end,resume=True)
        except AssertionError:
            print('pump:',e.counter,end,'responses',len(e.responses),
                  [n for _,n,_ in e.responses[-10:]],flush=True)
            raise
    pump(20)
    # Cold first move and a warm second move in the opposite direction.
    for i,d in enumerate((direction,1-direction)):
        e.requests.appendleft(('jog',setup(0x40,0x73,d)))
        e.run(lambda e:len(e.drives)==i+1 and e.drive is None,resume=True)
        pump(330)  # complete cleanup, cooldown and ordinary management polling
    assert not e.erases and not e.programs
    assert bytes(e.uc.mem_read(0,0x10000))==boot
    assert e.regs[C]==0 and e.regs[C+8]==2000
    e.requests.appendleft(('after_jogs',setup(0xc0,0x42,length=32)))
    e.run(lambda e:any(n=='after_jogs' for _,n,_ in e.responses),resume=True)
    return {'sha256':hashlib.sha256(app).hexdigest(),'writes':e.writes,'drives':e.drives,
            'protected_prefix_unchanged':True,'management_responsive':True}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--candidate',type=Path,default=APP/'build')
    p.add_argument('--output',type=Path,default=APP/'build/validation_motor_baseline.json')
    args=p.parse_args()
    assert hashlib.sha256((args.baseline/'application.bin').read_bytes()).hexdigest()==BASE_HASH
    report={'assumptions':__doc__,'baseline_sha256':BASE_HASH,'cases':[]}
    for direction in (0,1):
        old=run(args.baseline,direction);new=run(args.candidate,direction)
        if old['writes']!=new['writes']:
            import difflib
            print('\n'.join(difflib.unified_diff(
                [f'{a:08x}={v:08x}' for a,v in old['writes']],
                [f'{a:08x}={v:08x}' for a,v in new['writes']],fromfile='baseline',tofile='candidate')))
        assert old['writes']==new['writes'],'motor MMIO write sequence changed'
        for o,n in zip(old['drives'],new['drives']):
            assert [v for _,v in o['waveform']]==[v for _,v in n['waveform']]
            assert abs(o['duration_ms']-n['duration_ms'])<.6
            assert all(abs(ot-nt)<.3 for (ot,_),(nt,_) in zip(o['waveform'],n['waveform']))
        report['cases'].append({'first_direction':direction,'baseline':old,'candidate':new})
        print('PASS complete cold+warm motor write sequence and waveform, first direction',direction)
    report['all_assertions_passed']=True
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print('ALL BASELINE COMPARISONS PASSED')
if __name__=='__main__':main()
