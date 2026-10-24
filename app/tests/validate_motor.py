#!/usr/bin/env python3
"""Regress bounded jogs and key guards alongside the home controller.
Synthetic position/clock inputs; no physical motion claim.
"""
import hashlib,json,sys
from pathlib import Path
APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP/'tests'))
from validate_home import HomeModel,P,C,KEY,CLOCK,BOOT
from model import setup,TICKS_PER_MS

def main():
    boot=(BOOT/'build/boot_region.bin').read_bytes();app=(APP/'build/application.bin').read_bytes()
    meta={'layout':{'version':2,'application':[0x10000,0x200000]},'symbols':{
        'bootloader':json.loads((BOOT/'build/manifest.json').read_text())['symbols']['bootloader'],
        'application':json.loads((APP/'build/manifest.json').read_text())['symbols']['application']}}
    report={'app_sha256':hashlib.sha256(app).hexdigest(),'assumptions':__doc__,'cases':{}}
    def pump(e,ms):
        end=e.counter+ms*TICKS_PER_MS;e.run(lambda e:e.counter>=end,resume=True)
    def booted(keys=0):
        e=HomeModel(image=(boot+app).ljust(0x200000,b'\xff'),metadata=meta)
        e.keys=keys;e.behavior='frozen';e.run(lambda e:bool(e.info()) and e.info()[2]==2)
        pump(e,20);return e
    def record(name,e):
        assert not e.erases and not e.programs
        assert bytes(e.uc.mem_read(0,0x10000))==boot
        assert e.drive is None
        if e.drives:
            assert e.regs[C]==0 and e.regs[C+8]==2000
            assert all(0<d['duration_ms']<40.6 for d in e.drives)
        report['cases'][name]={'drives':e.drives,'protected_prefix_unchanged':True}
        print('PASS',name,flush=True)
    e=booted()
    for direction in (0,1):
        e.requests.appendleft(('jog',setup(0x40,0x73,direction)));pump(e,50);pump(e,330)
    assert len(e.drives)==2
    assert max(e.drives[0]['values'])==2600 and min(e.drives[1]['values'])==1400
    assert all(v>=2000 for v in e.drives[0]['values'])
    assert all(v<=2000 for v in e.drives[1]['values'])
    assert all(40<=d['duration_ms']<40.6 for d in e.drives)
    record('both_usb_jog_polarities_frozen_deadline',e)
    e=booted();e.keys=0x400;pump(e,650);assert len(e.drives)==1
    record('held_bw_key_no_repeat',e)
    e=booted(0x400);pump(e,40);assert not e.drives
    e.keys=0;pump(e,5);e.keys=0x400;pump(e,50);assert len(e.drives)==1
    record('startup_held_key_requires_release',e)
    e=booted();e.keys=0x1400;pump(e,40);assert not e.drives
    record('both_keys_do_not_start',e)
    e=booted();e.keys=0x400;e.run(lambda e:e.drive is not None,resume=True)
    e.keys=0x20;pump(e,8);assert e.drives[0]['duration_ms']<3
    assert e.regs.get(0xc0c85614,0)==0
    record('cancel_stops_without_toggling_lamp',e)
    for direction in (0,1):
        e=booted();e.behavior='normal'
        e.requests.appendleft(('jog',setup(0x40,0x73,direction)));pump(e,50)
        assert len(e.drives)==1 and e.drives[0]['duration_ms']<18
        record('travel_guard_'+str(direction),e)
    e=booted();e.serial_stuck=True;e.keys=0x400;pump(e,40)
    assert not e.drives and e.regs[0xc0c41008]==0;record('serial_timeout_no_drive',e)
    e=booted();e.requests.appendleft(('invalid',setup(0x40,0x73,2)));pump(e,20)
    assert not e.drives and 'invalid' in e.stalls;record('invalid_direction',e)
    e=booted();e.regs[0xc0c52000]=0x102;e.keys=0x400;pump(e,40)
    assert not e.drives and e.regs[0xc0c52000]==0x102
    record('shared_setup_refuses_other_active_motor',e)
    e=booted();e.keys=0x400;e.run(lambda e:e.drive is not None,resume=True)
    e.requests.appendleft(('overlap',setup(0x40,0x73,1)));pump(e,15)
    assert len(e.drives)==1 and 'overlap' in e.stalls;record('overlap_stops',e)
    report['all_assertions_passed']=True
    (APP/'build/validation_motor.json').write_text(json.dumps(report,indent=2)+'\n')
    print('ALL MOTOR ASSERTIONS PASSED',flush=True)
if __name__=='__main__':main()
