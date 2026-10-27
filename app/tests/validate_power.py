#!/usr/bin/env python3
"""Emulate the power manager (src/power.c): USB 0xa0 status, 0xa1 control.

Covers the first-implementation contract of BREIF_POWER_MANAGEMENT.md:
inactivity standby with continuous status polling, busy refusal while an
owner (continuous illumination) works, explicit/idempotent standby and wake,
automatic wake on an admitted job, Power-key semantics in the normal map,
parameter validation and rebase, timer disable, millisecond-clock wrap, an
injected readback failure (FAULT) and management (INFO, reboot) while asleep.
Logical register readbacks only; nothing here measures a rail.
"""
import json
import struct
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests'))
sys.path.insert(0, str(APP / 'tests'))
from model import setup                   # noqa: E402
from model_printer import PrinterModel     # noqa: E402

KEY = 0xc0c8d028
SCAN_POWER = 0xc0c85614
LOG = ('log', setup(0xc0, 0x50, 0, 0, 256))
STATUS = ('pwr', setup(0xc0, 0xa0, 0, 0, 64))
READY, ACTIVE, QUIESCING, STANDBY, WAKING, FAULT = range(6)
NAMES = ['ready', 'active', 'quiescing', 'standby', 'waking', 'fault']


class PowerModel(PrinterModel):
    """Lets a test pin the scanner power readback high (injected quiesce failure)."""
    stuck_scanner_power = False

    def read(self, cpu, offset, size, user):
        if self.stuck_scanner_power and 0xc0000000 + offset == SCAN_POWER:
            return 1
        return super().read(cpu, offset, size, user)


def app_mode(e):
    return bool(e.info()) and e.info()[2] == 2


def log_text(e):
    return b''.join(d for g, n, d in e.responses if n == 'log' and g == e.generation)


def status(e):
    data = [d for g, n, d in e.responses if n == 'pwr' and g == e.generation and len(d) == 64]
    if not data:
        return None
    w = struct.unpack('<16I', data[-1])
    assert w[0] == 0x50575231, hex(w[0])
    return dict(state=w[1] & 0xff, reason=w[1] >> 8 & 0xff, result=w[1] >> 16 & 0xff, stage=w[1] >> 24,
                timeout=w[2], idle_ms=w[3], owners=w[4], error=w[5], caps=w[6], flags=w[7],
                motor_c=w[8], motor_cmp=w[9], transitions=w[10], standbys=w[11], wakes=w[12], now=w[13])


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes()
    app = (APP / 'build/application.bin').read_bytes()
    factory = (boot + app).ljust(0x200000, b'\xff')
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
          'symbols': {'bootloader': bs['bootloader'], 'application': asym}}
    e = PowerModel(image=factory, metadata=md)
    e.regs[KEY] = 0
    e.run(app_mode)

    def pump(predicate, budget=20000000, rounds=8, slices=60):
        """Run in slices, keeping status (and log) requests queued the whole time:
        continuous host polling must not keep the printer awake."""
        for _ in range(slices):
            for _ in range(rounds):
                e.requests.append(STATUS); e.requests.append(LOG)
            e.run(lambda x: predicate(x) or not any(n == 'pwr' for n, _ in x.requests), resume=True, budget=budget)
            if predicate(e):
                return
        raise AssertionError(f'condition not reached; status={status(e)} log={log_text(e)[-600:]!r}')

    def settle(ms=30):
        t = status(e)['now'] if status(e) else 0
        pump(lambda x: status(x) is not None and status(x)['now'] - t >= ms)

    def press(key, predicate):
        """Hold a panel key until `predicate`, release it and let the debounce see the release."""
        e.regs[KEY] = key
        pump(predicate)
        e.regs[KEY] = 0
        settle()

    def wait_state(s):
        pump(lambda x: status(x) is not None and status(x)['state'] == s)
        return status(e)

    def n_status(x):
        return sum(1 for g, nm, d in x.responses if nm == 'pwr' and g == x.generation)

    def control(name, op, arg=0):
        n = len(e.stalls)
        e.requests.appendleft((name, setup(0x40, 0xa1, op, arg)))
        pump(lambda x: any(g == x.generation and nm == name for g, nm, d in x.responses) or len(x.stalls) > n, rounds=4)
        k = n_status(e)
        pump(lambda x: n_status(x) > k, rounds=4)          # a status taken after the request completed
        return not (len(e.stalls) > n and e.stalls[-1] == name)

    def led_frames(since):
        return e.led_patterns['application'][since:]

    # 1. boot defaults
    pump(lambda x: status(x) is not None, rounds=4)
    s = status(e)
    assert s['state'] == READY and s['timeout'] == 60 and s['caps'] == 0x0f and s['owners'] == 0, s
    # (the boot banner may already have scrolled out of the log ring; status is the contract)
    print('PASS boot: ready, 60 s default, caps 0x0f, no owners')

    # 2. parameter validation and rebase
    assert not control('t_bad', 3, 3601), 'timeout 3601 s must be refused'
    assert status(e)['result'] == 3, status(e)
    assert not control('op_bad', 9), 'unknown op must be refused'
    assert control('t2', 3, 2) and status(e)['timeout'] == 2
    assert status(e)['state'] == READY and status(e)['idle_ms'] < 1500, status(e)
    print('PASS control: invalid timeout/op stalled (result=invalid), 2 s accepted and rebased')

    # 3. inactivity standby with continuous polling; panel goes dark; scanner power 0; no motor writes
    frames0 = len(e.led_patterns['application']); nmotor0 = len(e.motor_writes)
    s = wait_state(STANDBY)
    assert s['reason'] == 1 and s['standbys'] == 1 and s['owners'] == 0, s
    assert e.regs.get(SCAN_POWER, 0) == 0 and not (s['flags'] & 1), s
    pump(lambda x: b'[power] panel dark' in log_text(x), rounds=8)
    dark = [f for f in led_frames(frames0)]
    assert dark and dark[-1] == (0, 0, 0, 0), dark[-4:]
    assert len(e.motor_writes) == nmotor0, e.motor_writes[nmotor0:]
    assert s['motor_c'] == 0, s
    print(f'PASS idle standby: after {s["idle_ms"]} ms idle with status polling; panel dark; 0xc0c85614=0; motor untouched')

    # 4. management while asleep: INFO still answers, standby is idempotent
    assert app_mode(e), 'INFO must answer in standby'
    assert control('sb_again', 1) and status(e)['state'] == STANDBY and status(e)['standbys'] == 1
    print('PASS asleep: INFO answers, repeated standby is a no-op')

    # 5. explicit wake: ready, panel relit, nothing energised
    frames1 = len(e.led_patterns['application'])
    assert control('wake', 2)
    s = wait_state(READY)
    assert s['wakes'] == 1 and s['reason'] == 2, s
    pump(lambda x: len(x.led_patterns['application']) > frames1 + 4, rounds=8)
    assert any(f != (0, 0, 0, 0) for f in led_frames(frames1)), led_frames(frames1)[-4:]
    assert e.regs.get(SCAN_POWER, 0) == 0 and len(e.motor_writes) == nmotor0
    assert control('wake_again', 2) and status(e)['wakes'] == 1
    print('PASS wake: ready, panel relit, scanner/motor still off, idempotent')

    # 6. an owner (continuous illumination via Cancel) refuses explicit standby and blocks the timer
    press(0x20, lambda x: b'illumination ON' in log_text(x))
    s = wait_state(ACTIVE)
    assert s['owners'] & 0x4 and e.regs.get(SCAN_POWER) == 1, s
    assert not control('sb_busy', 1), 'standby must be refused while the lamp owns the scanner'
    s = status(e); assert s['result'] == 2 and s['state'] == ACTIVE, s
    e.uc.mem_write(asym_addr(asym, 'milliseconds'), struct.pack('<I', read_u32(e, asym, 'milliseconds') + 10000))
    pump(lambda x: status(x) is not None and status(x)['idle_ms'] >= 0, rounds=6)
    assert status(e)['state'] == ACTIVE and e.regs.get(SCAN_POWER) == 1, status(e)
    print('PASS busy: standby refused (result=busy, owners=0x%x), lamp stays on past the timeout' % s['owners'])

    # 7. owner releases -> ready -> timer -> standby; lamp admitted while asleep wakes automatically
    press(0x20, lambda x: b'illumination OFF' in log_text(x))
    wait_state(READY)
    s = wait_state(STANDBY)
    assert s['standbys'] == 2, s
    press(0x20, lambda x: log_text(x).count(b'illumination ON') >= 2)
    s = wait_state(ACTIVE)
    assert s['wakes'] == 2 and b'[power] waking' in log_text(e) and not (s['flags'] & 2), s
    press(0x20, lambda x: log_text(x).count(b'illumination OFF') >= 2)
    wait_state(READY)
    print('PASS auto wake: job admitted in standby -> waking -> active, panel relit; release -> ready')

    # 8. Power key: standby when idle, wake when asleep
    press(0x40, lambda x: status(x)['state'] == STANDBY)
    s = status(e); assert s['reason'] == 3, s
    press(0x40, lambda x: status(x)['state'] == READY)
    s = status(e); assert s['reason'] == 3, s
    print('PASS power key: standby / wake from the panel')

    # 9. timeout 0 disables the timer; a later setting rebases
    assert control('t0', 3, 0)
    e.uc.mem_write(asym_addr(asym, 'milliseconds'), struct.pack('<I', read_u32(e, asym, 'milliseconds') + 100000))
    pump(lambda x: status(x)['idle_ms'] >= 100000, rounds=6)
    assert status(e)['state'] == READY, status(e)
    assert control('t3', 3, 3) and status(e)['state'] == READY and status(e)['idle_ms'] < 1500, status(e)
    print('PASS timer: 0 disables (100 s idle stays ready); new setting rebases the idle age')

    # 10. millisecond-clock wrap: idle timing spans 2^32 ms
    # (state is READY with a 3 s timer; move the clock and the idle anchor together to 1 s before the wrap)
    e.uc.mem_write(asym_addr(asym, 'milliseconds'), struct.pack('<I', 0xffffffff - 1000))
    e.uc.mem_write(asym_addr(asym, 'last_active_ms'), struct.pack('<I', 0xffffffff - 1000))
    s = wait_state(STANDBY)
    assert s['now'] < 0x10000 and s['idle_ms'] >= 3000, s
    print('PASS wrap: standby fired across the millisecond wrap (now=0x%x idle=%d)' % (s['now'], s['idle_ms']))

    # 11. injected readback failure -> FAULT with stage and error, wake recovers, then standby works
    assert control('wake3', 2) and wait_state(READY)
    e.stuck_scanner_power = True
    assert not control('sb_fault', 1), 'a failed quiesce must not be acknowledged'
    s = status(e)
    assert s['state'] == FAULT and s['error'] == 1 and s['stage'] == 2 and s['result'] == 4, s
    assert b'quiesce failed stage 2' in log_text(e)
    e.stuck_scanner_power = False
    assert control('wake4', 2) and wait_state(READY)
    assert control('sb_ok', 1) and status(e)['state'] == STANDBY
    print('PASS fault: injected scanner-power readback -> fault(stage 2, error 1), wake clears, standby again ok')

    # 12. reboot into rescue from standby
    e.requests.appendleft(('reboot', setup(0x40, 0x41, 1)))
    e.run(lambda x: x.stage == 'bootloader', resume=True, budget=120000000)
    assert e.stage == 'bootloader'
    print('PASS rescue: reboot request honoured while asleep')
    print('ALL POWER ASSERTIONS PASSED')


def asym_addr(asym, name):
    return int(asym[name], 16)


def read_u32(e, asym, name):
    return struct.unpack('<I', e.uc.mem_read(asym_addr(asym, name), 4))[0]


if __name__ == '__main__':
    main()
