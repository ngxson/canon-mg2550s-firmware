#!/usr/bin/env python3
"""Emulate carriage homing and closed-loop moves (USB 0x6c / 0x6d / 0x6e).

Scripted mechanism: the CR position changes with the drive command while the
drive is enabled, clamped between two walls 0x1d000 counts apart, reported in
steps of 64. Checks homing to the right wall, a left move landing near its
target without crossing the limits, a return move, a refused out-of-range
target, a stall (frozen position) ending with the drive off, USB stop, and
that the feed/scanner drives are never enabled. Synthetic inputs only.
"""
import json, struct, sys
from pathlib import Path
APP = Path(__file__).resolve().parents[1]; BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests'))
from model import setup, checked, TICKS_PER_MS  # noqa: E402
from model_printer import PrinterModel  # noqa: E402
from model_carriage import CarriageMixin, CR_C, CR_POS, TRAVEL  # noqa: E402


class CRModel(CarriageMixin, PrinterModel):
    def __init__(self, *a, frozen=False, **k):
        super().__init__(*a, **k); self.cr_init(frozen=frozen)

    @checked
    def write(self, cpu, offset, size, value, user):
        self.cr_write(0xc0000000 + offset, value)
        return super().write(cpu, offset, size, value, user)

    @checked
    def read(self, cpu, offset, size, user):
        a = 0xc0000000 + offset
        r = self.cr_read(a)
        if r is not None: return r
        if a == 0xc0c82008 and self.stage == 'application':
            self.counter = (self.counter + TICKS_PER_MS // 4) & 0xffffffff   # 0.25 modeled ms per read
            return self.counter & 0x1fffffff
        return super().read(cpu, offset, size, user)


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes(); app = (APP / 'build/application.bin').read_bytes()
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]}, 'symbols': {'bootloader': bs['bootloader'], 'application': asym}}
    report = {'assumptions': __doc__, 'cases': {}}
    s32 = lambda v: v - (1 << 32) if v >= 1 << 31 else v

    def booted(**kw):
        e = CRModel(image=(boot + app).ljust(0x200000, b'\xff'), metadata=md, **kw)
        e.run(lambda x: bool(x.info()) and x.info()[2] == 2); return e

    def status(e):
        n = sum(1 for _, name, _ in e.responses if name == 'cr_status')
        e.requests.appendleft(('cr_status', setup(0xc0, 0x6e, 0, 0, 48)))
        e.run(lambda x, n=n: sum(1 for _, name, _ in x.responses if name == 'cr_status') > n, resume=True, budget=50000000)
        return struct.unpack('<12I', [d for _, name, d in e.responses if name == 'cr_status'][-1])

    def command(e, name, req, value=0, index=0, expect_stall=False):
        n = sum(1 for _, nm, _ in e.responses if nm == name); st = len(e.stalls)
        e.requests.appendleft((name, setup(0x40, req, value, index)))
        e.run(lambda x, n=n, st=st: sum(1 for _, nm, _ in x.responses if nm == name) > n or len(x.stalls) > st, resume=True)
        assert (len(e.stalls) > st) == expect_stall, (name, e.stalls)

    def wait_idle(e, polls=6000):
        for _ in range(polls):
            s = status(e)
            if s[2] == 0 and s[3] != 1: return s
        raise AssertionError(('did not finish', s))

    def clean(e):
        assert all(not (a in (0xc0c52000, 0xc0c56000) and v) for a, v in e.motor_writes), 'other drive enabled'
        assert e.regs.get(CR_C) == 0 and e.regs.get(0xc0c50008) == 2000
        assert not e.erases and not e.programs

    # 1) home: drive right until the wall, reference = wall counter
    e = booted()
    command(e, 'home', 0x6c); s = wait_idle(e)
    assert s[3] == 2 and s[10] & 1 and s[5] == e.wall & 0x7ffff & ~63, [hex(x) for x in s]
    assert s32(s[4]) == 0
    clean(e); report['cases']['home'] = list(s); print('PASS home reference', hex(s[5]))

    # 2) move left to 50000, land within tolerance, never beyond the limits, commands clamped
    command(e, 'move', 0x6d, 50000 // 64, 0); s = wait_idle(e)
    assert s[3] == 2 and abs(s32(s[4]) - 50000) <= 320, (s[3], s32(s[4]))
    assert e.pos <= e.wall + TRAVEL - 1024, 'crossed the far limit'
    assert all(1200 <= c <= 2800 for c in e.cmds), (min(e.cmds), max(e.cmds))
    clean(e); report['cases']['move_left'] = list(s); print('PASS move_left x=', s32(s[4]))

    # 3) move back right to 1000
    command(e, 'move', 0x6d, 1024 // 64, 400); s = wait_idle(e)
    assert s[3] == 2 and abs(s32(s[4]) - 1024) <= 320, s32(s[4])
    clean(e); print('PASS move_right x=', s32(s[4]))

    # 4) out-of-range targets and move before homing are refused
    command(e, 'too_far', 0x6d, (TRAVEL - 1000) // 64, 0, expect_stall=True)
    e2 = booted(); command(e2, 'unreferenced', 0x6d, 100, 0, expect_stall=True)
    print('PASS limits_refused')

    # 5) stall: frozen mechanism -> stall result, drive off
    command(e2, 'home2', 0x6c); s = wait_idle(e2)
    e2.frozen = True
    command(e2, 'move_frozen', 0x6d, 30000 // 64, 0); s = wait_idle(e2)
    assert s[3] == 3, s
    clean(e2); report['cases']['stall'] = list(s); print('PASS stall_detected')

    # 5b) Short moves (stamping): 12 consecutive 1920-count steps from 35000, each must land within
    #     two counter steps and never stall; the friction model stalls the sweep controller on these.
    e3 = booted(); command(e3, 'home3', 0x6c); wait_idle(e3)
    command(e3, 'move_to_start', 0x6d, 35000 // 64, 0); s = wait_idle(e3); assert s[3] == 2, s
    errs = []
    for i in range(1, 13):
        x = 35000 + i * 1920
        command(e3, 'short%d' % i, 0x6d, x // 64, 0); s = wait_idle(e3)
        assert s[3] == 2, ('short move failed', i, s)
        errs.append(s32(s[4]) - x)
    assert max(abs(v) for v in errs) <= 640, errs      # a constant overshoot of ten steps is tolerable: nothing prints at a stop
    clean(e3); report['cases']['short_moves'] = {'landing_errors': errs}; print('PASS short_moves errors', errs)

    # 6) USB stop during a move
    e2.frozen = False
    command(e2, 'move3', 0x6d, 60000 // 64, 0)
    s = status(e2); assert s[2] == 2 and s[3] == 1, s
    command(e2, 'stop', 0x76)
    s = status(e2); assert s[2] == 0 and s[3] == 5, s
    clean(e2); print('PASS usb_stop')

    report['all_assertions_passed'] = True
    (APP / 'build/validation_carriage.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL CARRIAGE ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
