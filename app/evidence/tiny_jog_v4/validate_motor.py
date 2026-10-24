#!/usr/bin/env python3
"""Execute the app's tiny Copy-key jogs; no physical motor model is claimed.

Tests real ARM code, key debounce, output polarity, deadline with frozen
feedback, injected fault/feedback inputs, and USB/I2C recovery. Emulated time
advances 0.05 ms per counter read in the app (not cycle-accurate).
"""
import hashlib
import json
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests'))
from model import Model, checked, setup, TICKS_PER_MS

P, C, KEY, CLOCK = 0xc0c57000, 0xc0c56000, 0xc0c8d028, 0xc0c82008
BW, COLOUR, CANCEL = 0x400, 0x1000, 0x20


class MotorModel(Model):
    def __init__(self, **kw):
        self.keys = 0
        self.serial_stuck = False
        self.feedback = 'frozen'
        self.drives = []
        self.drive = None
        self.outputs = []
        self.key_reads = 0
        super().__init__(**kw)

    @checked
    def read(self, cpu, offset, size, user):
        a = 0xc0000000 + offset
        if a == CLOCK and self.stage == 'application':
            self.ticks += 1
            self.counter = (self.counter + TICKS_PER_MS // 20) & 0xffffffff
            return self.counter
        if a == KEY:
            self.key_reads += 1
            return self.keys
        if a == 0xc0c50218 and self.stage == 'application' and self.serial_stuck:
            return 1
        if self.drive and a == P+0x100:
            if self.feedback == 'travel':
                self.regs[a] += 64
            elif self.feedback == 'wrong':
                return self.drive['x0'] - 32
            elif self.feedback == 'slow':
                age=((self.counter-self.drive['start'])&0xffffffff)/TICKS_PER_MS
                return self.drive['x0'] + int(age)  # 1 count/ms, target not reached
            elif self.feedback == 'jitter':
                return self.drive['x0'] + (self.ticks % 3) * 2  # 0/2/4, no net progress
            elif self.feedback == 'stalls_later':
                age=((self.counter-self.drive['start'])&0xffffffff)/TICKS_PER_MS
                return self.drive['x0'] + min(int(age),40)
        if self.drive and a == C+0x104 and self.feedback == 'complete':
            return 1
        return super().read(cpu, offset, size, user)

    @checked
    def write(self, cpu, offset, size, value, user):
        super().write(cpu, offset, size, value, user)
        a = 0xc0000000 + offset
        if self.stage != 'application':
            return
        self.outputs.append((a, value))
        if a in (0xc0c50000, 0xc0c52000):
            assert value == 0, 'other motor energized'
        if a == C and value == 0x102:
            assert self.drive is None
            self.drive = dict(start=self.counter, x0=self.regs[P+0x100], values=[],
                              registers={hex(k): v for k,v in self.regs.items()
                                         if P <= k < P+0x500 or C <= k < C+0x108
                                         or 0xc0c41000 <= k < 0xc0c41500 or k == 0xc0c81090})
            self.drives.append(self.drive)
        if a == C+8 and self.drive:
            self.drive['values'].append(value)
        if a == C and value == 0 and self.drive:
            self.drive['duration_ms'] = ((self.counter-self.drive['start']) & 0xffffffff) / TICKS_PER_MS
            self.drive = None


def main():
    boot = (BOOT/'build/boot_region.bin').read_bytes()
    app = (APP/'build/application.bin').read_bytes()
    meta = {'layout': {'version':2, 'application':[0x10000,0x200000]}, 'symbols': {
        'bootloader':json.loads((BOOT/'build/manifest.json').read_text())['symbols']['bootloader'],
        'application':json.loads((APP/'build/manifest.json').read_text())['symbols']['application']}}
    image = (boot+app).ljust(0x200000,b'\xff')
    report = {'app_sha256':hashlib.sha256(app).hexdigest(), 'assumptions':__doc__, 'cases':{}}

    def booted(keys=0):
        e = MotorModel(image=image, metadata=meta)
        e.keys = keys
        e.run(lambda e:bool(e.info()) and e.info()[2]==2)
        pump(e,20)
        return e

    def pump(e,ms):
        end = e.counter + int(ms*TICKS_PER_MS)
        e.run(lambda e:e.counter>=end, resume=True)

    def record(name,e):
        assert not e.erases and not e.programs
        assert bytes(e.uc.mem_read(0,0x10000))==boot
        if e.drives:
            assert e.drive is None
            assert e.regs[C]==0 and e.regs[C+8]==2000
            assert all(0 < d['duration_ms'] <= 150.6 for d in e.drives)
        report['cases'][name] = {'drives':e.drives,'protected_prefix_unchanged':True,
                                 'erases':e.erases,'programs':e.programs}
        print('PASS',name,flush=True)

    # No 0x70 command: keys work immediately. Frozen feedback must still stop.
    e = booted()
    for key in (BW,COLOUR):
        e.keys=key
        pump(e,80)
        e.keys=0
        pump(e,330)
    assert len(e.drives)==2
    assert all(2000 < v <= 2120 for v in e.drives[0]['values'])
    assert all(1880 <= v < 2000 for v in e.drives[1]['values'])
    assert max(e.drives[0]['values']) == 2120
    assert min(e.drives[1]['values']) == 1880
    assert all(25 <= d['duration_ms'] < 26 for d in e.drives), 'frozen feedback must stall'
    for d in e.drives:
        regs=d['registers']
        for a,v in ((0xc0c41404,4),(0xc0c41408,5),(0xc0c41418,10),
                    (0xc0c81090,0x3f),(P,0x200d),(P+0x148,1),(P+0x308,1)):
            assert regs[hex(a)]==v,(hex(a),regs[hex(a)],v)
    for a in (0xc0c51000,0xc0c53000,P):
        assert any(addr==a and v&0x20 for addr,v in e.outputs)
    record('both_copy_keys_frozen_feedback_stock_setup',e)

    e=booted(); e.keys=BW; pump(e,650)
    assert len(e.drives)==1
    record('held_key_does_not_repeat',e)
    e=booted(BW); pump(e,40)
    assert not e.drives
    e.keys=0; pump(e,5); e.keys=BW; pump(e,80)
    assert len(e.drives)==1
    record('held_at_boot_requires_release',e)
    e=booted(); e.keys=BW|COLOUR; pump(e,40)
    assert not e.drives
    record('both_keys_no_drive',e)

    e=booted(); e.keys=BW
    e.run(lambda e:e.drive is not None,resume=True)
    e.keys=CANCEL; pump(e,8)
    assert len(e.drives)==1 and e.drives[0]['duration_ms'] < 3
    assert e.regs.get(0xc0c85614,0)==0, 'Cancel must not toggle lamp after stopping'
    record('cancel_stops_immediately',e)
    for feedback in ('travel','wrong','complete'):
        e=booted(); e.feedback=feedback; e.keys=BW; pump(e,80)
        assert len(e.drives)==1 and e.drives[0]['duration_ms'] < 32
        record('stop_on_'+feedback,e)

    e=booted(); e.serial_stuck=True; e.keys=BW; pump(e,40)
    assert not e.drives and e.regs[0xc0c41008]==0
    record('serial_timeout_no_drive',e)

    e=booted(); e.keys=COLOUR
    e.run(lambda e:e.drive is not None,resume=True)
    before=len(e.responses)
    e.requests.appendleft(('info_during_jog',setup(0xc0,0x42,length=32)))
    e.run(lambda e:any(n=='info_during_jog' for _,n,_ in e.responses[before:]),resume=True)
    assert e.drive is not None, 'INFO was not serviced until after pulse'
    e.requests.appendleft(('reboot_boot',setup(0x40,0x41,1)))
    e.run(lambda e:e.resets==2 and bool(e.info()) and e.info()[2]==1 and e.info()[3]&1,resume=True)
    record('usb_info_and_reboot_during_jog',e)

    # Endpoint must stop in either direction at the stock-scaled 512 counts.
    for key,sign in ((BW,1),(COLOUR,-1)):
        e=booted();e.keys=key
        e.run(lambda e:e.drive is not None,resume=True)
        target=e.drive['registers'][hex(P+0x120)]
        assert target==e.drive['x0']+sign*512
        e.regs[P+0x100]=target
        pump(e,2)
        assert e.drive is None and e.drives[0]['duration_ms']<2
        record('512_count_stop_'+str(sign),e)

    e=booted();e.feedback='slow';e.keys=BW;pump(e,180)
    assert len(e.drives)==1 and 150 <= e.drives[0]['duration_ms'] < 151
    record('independent_150ms_deadline_while_progressing',e)
    e=booted();e.feedback='jitter';e.keys=BW;pump(e,80)
    assert len(e.drives)==1 and 25 <= e.drives[0]['duration_ms'] < 27
    record('jitter_does_not_reset_progress_deadline',e)
    e=booted();e.feedback='stalls_later';e.keys=BW;pump(e,100)
    assert len(e.drives)==1 and 64 <= e.drives[0]['duration_ms'] < 67
    record('motion_then_stall_stops',e)

    e=booted(); e.command=3  # offer only after application boot
    e.run(lambda e:e.resets==2 and bool(e.info()) and e.info()[2]==1 and e.info()[3]&1,resume=True)
    assert 0xc0 in e.results
    record('i2c_reboot_retained',e)

    e=booted()
    e.requests.appendleft(('invalid_jog',setup(0x40,0x73,2)))
    pump(e,20)
    assert not e.drives and e.stalls
    record('invalid_direction_rejected',e)
    report['all_assertions_passed']=True
    (APP/'build/validation_motor.json').write_text(json.dumps(report,indent=2)+'\n')
    print('ALL MOTOR ASSERTIONS PASSED',flush=True)


if __name__=='__main__':
    main()
