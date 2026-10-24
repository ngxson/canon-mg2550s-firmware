#!/usr/bin/env python3
"""Emulate the closed-loop paper feed (USB 0x69 / 0x6a).

Scripted mechanism: the LF position moves with the drive command while the
drive is enabled; a sheet is "picked" after enough reverse counts and its
trailing edge clears the paper-end sensor (0xc0c84e08 bit 0) after enough
forward counts. Checks feed+eject, feed+hold then eject, three failed pickups
reporting no-paper, USB stop during pickup, and that the carriage and scanner
drives are never enabled. Synthetic inputs; no claim about physical motion.
"""
import json, struct, sys
from pathlib import Path
APP = Path(__file__).resolve().parents[1]; BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests'))
from model import setup, checked, TICKS_PER_MS  # noqa: E402
from model_printer import PrinterModel  # noqa: E402

LF_C, LF_POS, SENSOR = 0xc0c52000, 0xc0c53100, 0xc0c84e08
PICK_COUNTS, CLEAR_COUNTS = 15000, 12000


class FeedModel(PrinterModel):
    def __init__(self, *a, paper=True, jam=False, **k):
        super().__init__(*a, **k)
        self.pos = 0x1000; self.paper = paper; self.jam = jam; self.present = False
        self.reverse = 0; self.forward = 0; self.enables = 0

    @checked
    def write(self, cpu, offset, size, value, user):
        a = 0xc0000000 + offset
        if a == LF_POS: self.pos = value
        if a == LF_C and value == 0x102: self.enables += 1
        return super().write(cpu, offset, size, value, user)

    @checked
    def read(self, cpu, offset, size, user):
        a = 0xc0000000 + offset
        if a == LF_POS:
            if self.regs.get(LF_C) == 0x102:
                step = (self.regs.get(0xc0c52008, 2000) - 2000) // 40
                self.pos += step
                if step < 0 and not self.present:
                    self.reverse -= step
                    if self.paper and self.reverse >= PICK_COUNTS: self.present = True; self.forward = 0
                if step > 0 and self.present:
                    self.forward += step
                    if self.forward >= CLEAR_COUNTS and not self.jam: self.present = False; self.reverse = 0
            return self.pos & 0x7ffff
        if a == SENSOR: return 0 if self.present else 1
        if a == 0xc0c82008 and self.stage == 'application':   # 0.1 modeled ms per read, like the home test
            self.counter = (self.counter + TICKS_PER_MS // 10) & 0xffffffff
            return self.counter & 0x1fffffff
        return super().read(cpu, offset, size, user)


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes(); app = (APP / 'build/application.bin').read_bytes()
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]}, 'symbols': {'bootloader': bs['bootloader'], 'application': asym}}
    report = {'assumptions': __doc__, 'cases': {}}

    def booted(**kw):
        e = FeedModel(image=(boot + app).ljust(0x200000, b'\xff'), metadata=md, **kw)
        e.run(lambda x: bool(x.info()) and x.info()[2] == 2); return e

    def status(e):
        n = sum(1 for _, name, _ in e.responses if name == 'feed_status')
        e.requests.appendleft(('feed_status', setup(0xc0, 0x6a, 0, 0, 48)))
        e.run(lambda x, n=n: sum(1 for _, name, _ in x.responses if name == 'feed_status') > n, resume=True, budget=50000000)
        return struct.unpack('<12I', [d for _, name, d in e.responses if name == 'feed_status'][-1])

    def run_feed(e, mode, advance=0, budget_polls=12000):
        n = sum(1 for _, name, _ in e.responses if name == 'feed'); st = len(e.stalls)
        e.requests.appendleft(('feed', setup(0x40, 0x69, mode, advance)))
        e.run(lambda x, n=n, st=st: sum(1 for _, name, _ in x.responses if name == 'feed') > n or len(x.stalls) > st, resume=True)
        assert len(e.stalls) == st, e.stalls
        for _ in range(budget_polls):
            s = status(e)
            if s[2] == 0 and s[3] != 1: return s
        raise AssertionError(('feed did not finish', s))

    def clean(e):
        assert all(not (a in (0xc0c50000, 0xc0c56000) and v) for a, v in e.motor_writes), 'other drive enabled'
        assert e.regs.get(LF_C) == 0 and e.regs.get(0xc0c52008) == 2000
        assert not e.erases and not e.programs

    # 1) feed + eject
    e = booted(); s = run_feed(e, 0)
    assert s[3] == 2 and s[4] == 1 and s[5] >= PICK_COUNTS + 2048 and s[6] >= 3762 and s[8] == 0 and s[10] == 0, s   # arm clean after eject
    clean(e); report['cases']['feed_and_eject'] = list(s); print('PASS feed_and_eject', s[4:8])

    # 2) feed + hold at 8192 counts, then eject only
    s = run_feed(e, 1, 128)
    assert s[3] == 2 and s[6] >= 8192 and s[8] == 1 and s[10] & 1, s
    clean(e); print('PASS feed_and_hold', s[4:8])
    s = run_feed(e, 2)
    assert s[3] == 2 and s[8] == 0 and not (s[10] & 1) and s[7] > 0, s
    clean(e); report['cases']['hold_then_eject'] = list(s); print('PASS eject_only', s[4:8])

    # 3) no paper: three attempts, no_paper result
    e2 = booted(paper=False); s = run_feed(e2, 0, budget_polls=30000)
    assert s[3] == 3 and s[4] == 3 and s[8] == 0, s
    clean(e2); report['cases']['no_paper_three_attempts'] = list(s); print('PASS no_paper_three_attempts', s[4:8])

    # 4) USB stop during pickup -> cancelled, motor off
    e3 = booted(paper=False)
    e3.requests.appendleft(('feed', setup(0x40, 0x69, 0, 0)))
    e3.run(lambda x: any(name == 'feed' for _, name, _ in x.responses), resume=True)
    s = status(e3); assert s[2] in (1, 10) and s[3] == 1, s   # pickup or the initial homing move
    e3.requests.appendleft(('stop', setup(0x40, 0x76)))
    e3.run(lambda x: any(name == 'stop' for _, name, _ in x.responses), resume=True)
    s = status(e3); assert s[2] == 0 and s[3] == 5, s
    clean(e3); report['cases']['usb_stop_cancels'] = list(s); print('PASS usb_stop_cancels')

    # 4b) jam: sensor never clears -> jam result after the bounded forward distance
    e4 = booted(jam=True)
    s = run_feed(e4, 0, budget_polls=30000)
    assert s[3] == 7 and s[7] >= 60000 and s[10] & 2, s   # jam leaves the arm dirty
    clean(e4); report['cases']['jam_detected'] = list(s); print('PASS jam_detected', s[4:8])

    # 4c) manual run (USB 0x6b): reverse on, switch to forward, stop; feed refused while running
    e5 = booted()
    for name, val in (('man_rev', 1), ('man_fwd', 2)):
        e5.requests.appendleft((name, setup(0x40, 0x6b, val)))
        e5.run(lambda x, n=name: any(nm == n for _, nm, _ in x.responses), resume=True)
        assert name not in e5.stalls
    assert e5.regs[LF_C] == 0x102 and e5.regs[0xc0c52008] == 2300, (hex(e5.regs[LF_C]), e5.regs[0xc0c52008])
    e5.requests.appendleft(('feed_refused', setup(0x40, 0x69, 0, 0)))
    e5.run(lambda x: 'feed_refused' in x.stalls, resume=True)
    e5.requests.appendleft(('man_stop', setup(0x40, 0x6b, 0)))
    e5.run(lambda x: any(nm == 'man_stop' for _, nm, _ in x.responses), resume=True)
    clean(e5); print('PASS manual_run_switch_stop')

    # 5) invalid mode stalls; management reboot still works after feeding
    e.requests.appendleft(('bad_mode', setup(0x40, 0x69, 3, 0)))
    e.run(lambda x: 'bad_mode' in x.stalls, resume=True)
    before = e.resets
    e.requests.appendleft(('reboot_boot', setup(0x40, 0x41, 1)))
    e.run(lambda x: x.resets == before + 1 and bool(x.info()) and x.info()[2] == 1 and x.info()[3] & 1, resume=True)
    print('PASS invalid_mode_and_reboot')
    report['all_assertions_passed'] = True
    (APP / 'build/validation_feed.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL FEED ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
