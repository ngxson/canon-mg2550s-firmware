#!/usr/bin/env python3
"""Emulate the dry-run page job (USB 0x71/0x72) over scripted carriage and feed mechanisms.

Combines the carriage model (position follows command between two walls) and
the feed model (sheet picked after enough reverse counts, trailing edge clears
after enough forward counts). Checks a 3-band page completes with the expected
sub-sequence (home, feed, sweep/advance alternation, eject, park), that a
no-paper tray yields feed_failed with motors off, and that USB stop aborts.
"""
import json, struct, sys
from pathlib import Path
APP = Path(__file__).resolve().parents[1]; BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests'))
from model import setup, checked, TICKS_PER_MS  # noqa: E402
from model_printer import PrinterModel  # noqa: E402

CR_C, CR_POS, LF_C, LF_POS, SENSOR = 0xc0c50000, 0xc0c51100, 0xc0c52000, 0xc0c53100, 0xc0c84e08
TRAVEL, PICK, CLEAR = 0x1d000, 15000, 12000


class PageModel(PrinterModel):
    def __init__(self, *a, paper=True, **k):
        super().__init__(*a, **k)
        self.wall = 0x40000; self.cr = self.wall + 30000
        self.lf = 0x1000; self.paper = paper; self.present = False; self.rev = 0; self.fwd = 0
        self.events = []

    @checked
    def write(self, cpu, offset, size, value, user):
        a = 0xc0000000 + offset
        if a == CR_POS: self.wall = value - (self.cr - self.wall); self.cr = value
        if a == LF_POS: self.lf = value
        if a in (CR_C, LF_C) and value == 0x102: self.events.append('cr_on' if a == CR_C else 'lf_on')
        return super().write(cpu, offset, size, value, user)

    @checked
    def read(self, cpu, offset, size, user):
        a = 0xc0000000 + offset
        if a == CR_POS:
            if self.regs.get(CR_C) == 0x102:
                self.cr = max(self.wall, min(self.wall + TRAVEL, self.cr + (self.regs.get(0xc0c50008, 2000) - 2000) // 8))
            return (self.cr & 0x7ffff) & ~63
        if a == LF_POS:
            if self.regs.get(LF_C) == 0x102:
                step = (self.regs.get(0xc0c52008, 2000) - 2000) // 40
                self.lf += step
                if step < 0 and not self.present:
                    self.rev -= step
                    if self.paper and self.rev >= PICK: self.present = True; self.fwd = 0
                if step > 0 and self.present:
                    self.fwd += step
                    if self.fwd >= CLEAR: self.present = False; self.rev = 0
            return self.lf & 0x7ffff
        if a == SENSOR: return 0 if self.present else 1
        if a == 0xc0c82008 and self.stage == 'application':
            self.counter = (self.counter + TICKS_PER_MS // 4) & 0xffffffff
            return self.counter & 0x1fffffff
        return super().read(cpu, offset, size, user)


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes(); app = (APP / 'build/application.bin').read_bytes()
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]}, 'symbols': {'bootloader': bs['bootloader'], 'application': asym}}
    report = {'assumptions': __doc__, 'cases': {}}

    def booted(**kw):
        e = PageModel(image=(boot + app).ljust(0x200000, b'\xff'), metadata=md, **kw)
        e.run(lambda x: bool(x.info()) and x.info()[2] == 2); return e

    def status(e):
        n = sum(1 for _, name, _ in e.responses if name == 'pj_status')
        e.requests.appendleft(('pj_status', setup(0xc0, 0x72, 0, 0, 40)))
        e.run(lambda x, n=n: sum(1 for _, name, _ in x.responses if name == 'pj_status') > n, resume=True, budget=50000000)
        return struct.unpack('<10I', [d for _, name, d in e.responses if name == 'pj_status'][-1])

    def command(e, name, req, value=0, index=0, expect_stall=False):
        n = sum(1 for _, nm, _ in e.responses if nm == name); st = len(e.stalls)
        e.requests.appendleft((name, setup(0x40, req, value, index)))
        e.run(lambda x, n=n, st=st: sum(1 for _, nm, _ in x.responses if nm == name) > n or len(x.stalls) > st, resume=True)
        assert (len(e.stalls) > st) == expect_stall, (name, e.stalls)

    def wait_idle(e, polls=40000):
        states = []
        for _ in range(polls):
            s = status(e)
            if not states or states[-1] != s[2]: states.append(s[2])
            if s[2] == 0 and s[3] != 1: return s, states
        raise AssertionError(('did not finish', s))

    def clean(e):
        assert e.regs.get(CR_C) == 0 and e.regs.get(LF_C) == 0 and not e.regs.get(0xc0c56000)
        assert not e.erases and not e.programs

    # 1) three bands
    e = booted()
    command(e, 'dry', 0x71, 3 | (30 << 8), 2000 // 64); s, states = wait_idle(e)
    assert s[3] == 2 and s[4] == 3, s
    assert states[:3] == [1, 2, 3] and states[-2:] == [6, 0], states
    assert states.count(3) == 3 and states.count(4) == 2 and 5 in states, states
    assert not e.present
    clean(e); report['cases']['three_bands'] = {'status': list(s), 'states': states}
    print('PASS three_bands', 'sweep_ms', s[8], 'states', states)

    # 2) no paper -> feed_failed, motors off, carriage parked
    e2 = booted(paper=False)
    command(e2, 'dry', 0x71, 2, 0); s, states = wait_idle(e2, polls=200000)
    assert s[3] == 3, s
    clean(e2); report['cases']['no_paper'] = {'status': list(s), 'states': states}; print('PASS no_paper_feed_failed')

    # 3) stop during a sweep
    e3 = booted()
    command(e3, 'dry', 0x71, 4, 0)
    for _ in range(20000):
        s = status(e3)
        if s[2] == 3: break
    assert s[2] == 3, s
    command(e3, 'stop', 0x76)
    s = status(e3); assert s[2] == 0 and s[3] == 5, s
    clean(e3); print('PASS stop_during_sweep')

    report['all_assertions_passed'] = True
    (APP / 'build/validation_page_job.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL PAGE JOB ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
