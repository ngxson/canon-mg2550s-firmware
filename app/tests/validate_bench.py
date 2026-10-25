#!/usr/bin/env python3
"""Emulate the feed-bench button map (USB 0x8b = 1): Copy B/W places a sheet, Copy Colour advances
1024 counts per press, Power toggles the carriage between park and 60000, Cancel ejects a held sheet.
Uses the scripted feed and carriage mechanisms of the page-job suite and drives the key register
through press/release sequences; checks the requests the presses issue, the log lines with the feed
counter and sensor, and that the default map is unaffected."""
import json, struct, sys
from pathlib import Path
APP = Path(__file__).resolve().parents[1]; BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests'))
from model import setup  # noqa: E402
sys.path.insert(0, str(APP / 'tests'))
from validate_page_job import PageModel  # noqa: E402

KEY = 0xc0c8d028
LOG = ('log', setup(0xc0, 0x50, 0, 0, 256))


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes(); app = (APP / 'build/application.bin').read_bytes()
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]}, 'symbols': {'bootloader': bs['bootloader'], 'application': asym}}
    e = PageModel(image=(boot + app).ljust(0x200000, b'\xff'), metadata=md)
    e.run(lambda x: bool(x.info()) and x.info()[2] == 2)

    def command(name, req, value=0, index=0, expect_stall=False):
        n = sum(1 for _, nm, _ in e.responses if nm == name); st = len(e.stalls)
        e.requests.appendleft((name, setup(0x40, req, value, index)))
        e.run(lambda x, n=n, st=st: sum(1 for _, nm, _ in x.responses if nm == name) > n or len(x.stalls) > st, resume=True)
        assert (len(e.stalls) > st) == expect_stall, (name, e.stalls)

    def status(req, n, length, name):
        k = sum(1 for _, nm, _ in e.responses if nm == name); e.requests.appendleft((name, setup(0xc0, req, 0, 0, length)))
        e.run(lambda x, k=k: sum(1 for _, nm, _ in x.responses if nm == name) > k, resume=True, budget=50000000)
        return struct.unpack('<%dI' % n, [d for _, nm, d in e.responses if nm == name][-1])

    def log():
        for _ in range(64):                                       # drain until the ring is empty
            k = sum(1 for _, nm, _ in e.responses if nm == 'log'); e.requests.appendleft(LOG)
            e.run(lambda x, k=k: sum(1 for _, nm, _ in x.responses if nm == 'log') > k, resume=True, budget=50000000)
            if not [d for _, nm, d in e.responses if nm == 'log'][-1]: break
        return b''.join(d for g, n, d in e.responses if n == 'log' and g == e.generation)

    def press(bits, hold_polls=40):
        e.regs[KEY] = 8 | bits
        for _ in range(hold_polls): status(0x6a, 12, 48, 'feed')
        e.regs[KEY] = 8
        for _ in range(10): status(0x6a, 12, 48, 'feed')

    def wait_feed(polls=200000):
        for _ in range(polls):
            s = status(0x6a, 12, 48, 'feed')
            if s[2] == 0 and s[3] != 1: return s
        raise AssertionError('feed did not finish')

    def wait_cr(polls=6000):
        for _ in range(polls):
            s = status(0x6e, 12, 48, 'cr')
            if s[2] == 0 and s[3] != 1: return s
        raise AssertionError('carriage did not finish')

    e.regs[KEY] = 8                                            # real idle value
    command('map1', 0x8b, 1); assert b'[btn] map 1' in log()
    command('badmap', 0x8b, 2, 0, expect_stall=True)
    # Power: park (homing) then to 60000, then park again
    press(0x40); s = wait_cr(); assert s[3] == 2 and s[10] & 1
    press(0x40); s = wait_cr(); assert s[3] == 2 and abs((s[4] if s[4] < 1 << 31 else s[4] - (1 << 32)) - 60000) <= 640, s[4]
    press(0x40); s = wait_cr(); assert s[3] == 2
    t = log(); assert b'[bench] carriage: park' in t and b'[bench] carriage: to 60000' in t, t[-400:]
    print('PASS power_toggles_carriage')
    # Copy B/W: place (hold then advance 8000)
    press(0x400); s = wait_feed(); s = wait_feed()
    t = log(); assert b'[bench] place: hold 8000' in t and b'[bench] place: advance 8000' in t and b'[bench] placed' in t, t[-600:]
    assert s[3] == 2 and s[10] & 1, s                          # ok, holding
    print('PASS bw_places_sheet')
    # Copy Colour: advance 1024, three presses, counter moves by 1024 each
    p0 = s[9]
    for i in range(3):
        press(0x1000); s = wait_feed(); assert s[3] == 2, s
    t = log(); assert t.count(b'[bench] advance 1024') == 3, t[-500:]
    assert abs(((s[9] - p0) & 0x7ffff) - 3 * 1024) <= 192, (p0, s[9])
    print('PASS colour_advances_1024')
    # Cancel: eject when holding
    press(0x20); s = wait_feed(); assert s[3] == 2 and not (s[10] & 1), s
    assert b'[bench] eject' in log()
    print('PASS cancel_ejects')
    # back to map 0: Colour toggles the manual transport run (old behaviour), Cancel stops it
    command('map0', 0x8b, 0); press(0x1000); s = status(0x6a, 12, 48, 'feed')
    press(0x20); s = wait_feed()
    assert b'[bench] advance' not in log()[-300:]
    assert e.regs.get(0xc0c52000) == 0 and not e.erases and not e.programs
    print('PASS default_map_restored')
    (APP / 'build/validation_bench.json').write_text(json.dumps({'assumptions': __doc__, 'all_assertions_passed': True}, indent=2) + '\n')
    print('ALL BENCH ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
