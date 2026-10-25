#!/usr/bin/env python3
"""Emulate text by stamping while crawling: planner -> USB -> stamp job -> work engine -> virtual paper.

The head engine model completes a few status reads after each arm and captures
the record words and carriage position per fire; the carriage has friction and
momentum. A short text is planned with the hypothesis nozzle map (eight nozzles
per bit, cleared bit fires), uploaded as stamp entries (0x83/0x84 words, 0x85
commit), and run (0x87) at the floor crawl speed. Every captured fire is painted
onto the virtual paper at its carriage position. Checks: all entries fired, none
missed; each fire within one pixel pitch of its entry; the record words are the
planned ones; the rendered dots reproduce the text; the engine is idle and the
head interface shut down afterwards; a cancel during the crawl cleans up; a dry
job arms nothing; bad lists are refused.
"""
import json, struct, sys
from pathlib import Path
APP = Path(__file__).resolve().parents[1]; BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests')); sys.path.insert(0, str(APP / 'tools'))
from model import setup  # noqa: E402
from model_head import HeadModel, HE_ARM  # noqa: E402
from model_cartridge import CTL_ACQ, CTL_COLD, CTL_RES  # noqa: E402
from stamp_plan import NozzleMap, VirtualPaper, text_bitmap, plan, COUNTS_PER_COLUMN  # noqa: E402

BLACK37 = 0x3f | 5 << 7 | 37 << 11 | 0x15 << 22
COLOUR16_7 = 0x7f | 9 << 7 | 16 << 11 | 7 << 16 | 0x2c << 25 | 0xbeef << 32
TEXT, X0, COUNT = 'HI', 60000, 15


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes(); app = (APP / 'build/application.bin').read_bytes()
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]}, 'symbols': {'bootloader': bs['bootloader'], 'application': asym}}
    report = {'assumptions': __doc__, 'cases': {}}

    def booted(**kw):
        e = HeadModel(image=(boot + app).ljust(0x200000, b'\xff'), metadata=md, fuse={0: BLACK37, 1: COLOUR16_7}, **kw)
        e.regs[0xc0c90030] = 732; e.regs[0xc0c90028] = 731; e.regs[0xc0c90010] = 0x40
        e.run(lambda x: bool(x.info()) and x.info()[2] == 2); return e

    def poll(e, name, req, n_words, length):
        n = sum(1 for _, nm, _ in e.responses if nm == name)
        e.requests.appendleft((name, setup(0xc0, req, 0, 0, length)))
        e.run(lambda x, n=n: sum(1 for _, nm, _ in x.responses if nm == name) > n, resume=True, budget=50000000)
        return struct.unpack('<%dI' % n_words, [d for _, nm, d in e.responses if nm == name][-1])

    def command(e, name, req, value=0, index=0, expect_stall=False):
        n = sum(1 for _, nm, _ in e.responses if nm == name); st = len(e.stalls)
        e.requests.appendleft((name, setup(0x40, req, value, index)))
        e.run(lambda x, n=n, st=st: sum(1 for _, nm, _ in x.responses if nm == name) > n or len(x.stalls) > st, resume=True)
        assert (len(e.stalls) > st) == expect_stall, (name, e.stalls)

    def wait(e, req, n_words, length, polls=8000):
        for _ in range(polls):
            s = poll(e, 'st%x' % req, req, n_words, length)
            if s[2] == 0 and s[3] != 1: return s
        raise AssertionError(('did not finish', req, s))

    def prepare(e):
        seq = poll(e, 'cart', 0x63, 16, 64)[5]; command(e, 'cart_read', 0x62, 3 | 0x200, 0)
        for _ in range(400):
            s = poll(e, 'cart', 0x63, 16, 64)
            if s[2] == 2 and s[5] == seq + 1: break
        assert s[8] == BLACK37
        command(e, 'home', 0x6c); s = wait(e, 0x6e, 12, 48); assert s[3] == 2

    def upload(e, stops):
        command(e, 'clear', 0x86)
        for i, st in enumerate(stops):
            command(e, 'wa%d' % i, 0x83, st.word_a & 0xffff, st.word_a >> 16)
            command(e, 'wb%d' % i, 0x84, st.word_b & 0xffff, st.word_b >> 16)
            command(e, 'ci%d' % i, 0x85, i, st.x // 64)

    def idle_clean(e):
        assert all(e.regs.get(a, 0) == 0 for a in (CTL_ACQ, CTL_COLD, CTL_RES, HE_ARM, 0xc0c29aa0)), {hex(a): e.regs.get(a) for a in (CTL_ACQ, CTL_COLD, CTL_RES, HE_ARM, 0xc0c29aa0)}
        assert not e.fusecut_writes and e.regs.get(0xc0c50000) == 0

    nmap = NozzleMap(); cols = text_bitmap(TEXT); stops = plan(cols, nmap, X0); pitch = nmap.group_rows * COUNTS_PER_COLUMN

    # 1) the text
    e = booted(); prepare(e); upload(e, stops)
    n0 = len(e.fires)
    command(e, 'run', 0x87, COUNT, 0); s = wait(e, 0x88, 12, 48)
    assert s[3] == 2 and s[4] == len(stops) and s[5] == len(stops) and s[6] == 0, [hex(x) for x in s]
    fires = e.fires[n0:]; assert len(fires) == len(stops)
    paper = VirtualPaper(nmap); lags = []
    for st, f in zip(stops, fires):
        assert f['rec18'] == st.word_a and f['rec1c'] == st.word_b and f['mask'] == 3 and f['counts'][:2] == [COUNT - 1] * 2, (st, f)
        assert f['x'] >= st.x - 64 and f['x'] - st.x <= pitch, (st.x, f['x'])   # firmware sees the counter in steps of 64
        lags.append(f['x'] - st.x); paper.fire(f['x'], 0, f['rec18'], f['rec1c'])
    idle_clean(e)
    # Entries are sorted by x (ascending), i.e. text columns in reverse; render in text order.
    order = sorted(range(len(stops)), key=lambda i: stops[i].columns[0])
    rendered = '\n'.join(''.join('#' if any((fires[i]['x'] // COUNTS_PER_COLUMN, r * nmap.group_rows + k) in paper.dots for k in range(nmap.group_rows)) else '.'
                                  for i in order) for r in range(7))
    expected = '\n'.join(''.join('#' if cols[stops[i].columns[0]][r] else '.' for i in order) for r in range(7))
    print(rendered)
    assert rendered == expected, ('rendered text differs', rendered, expected)
    report['cases']['text'] = {'text': TEXT, 'stops': len(stops), 'fire_lag_counts': lags, 'job_ms': s[8], 'rendered': rendered}
    print('PASS text %r: %d entries fired while crawling, lag counts %s, job %d ms' % (TEXT, len(stops), lags, s[8]))

    # 2) dry job: nothing armed, all entries "fired" (configured), interface shut down
    n0 = len(e.arms); command(e, 'run_dry', 0x87, 0x100 | COUNT, 0); s = wait(e, 0x88, 12, 48)
    assert s[3] == 2 and s[5] == len(stops) and len(e.arms) == n0, [hex(x) for x in s]
    idle_clean(e); print('PASS dry_job')

    # 3) cancel during the crawl: cleanup, release, cancelled result
    command(e, 'run2', 0x87, COUNT, 0)
    for _ in range(3000):
        s = poll(e, 'st88', 0x88, 12, 48)
        if s[2] == 2: break
    assert s[2] == 2, s
    command(e, 'stop', 0x76); s = wait(e, 0x88, 12, 48); assert s[3] == 4, s
    idle_clean(e); print('PASS cancel_during_crawl')

    # 4) refusals: unsorted list, first entry too near the wall, empty list, bad count
    command(e, 'clear2', 0x86); command(e, 'c0', 0x85, 0, 50000 // 64); command(e, 'c1', 0x85, 1, 40000 // 64)
    command(e, 'run_unsorted', 0x87, COUNT, 0, expect_stall=True); assert poll(e, 'st88', 0x88, 12, 48)[3] == 6
    command(e, 'clear3', 0x86); command(e, 'c2', 0x85, 0, 1000 // 64)
    command(e, 'run_near_wall', 0x87, COUNT, 0, expect_stall=True); assert poll(e, 'st88', 0x88, 12, 48)[3] == 6
    command(e, 'clear4', 0x86); command(e, 'run_empty', 0x87, COUNT, 0, expect_stall=True)
    upload(e, stops[:2]); command(e, 'run_badcount', 0x87, 101, 0, expect_stall=True)
    print('PASS refusals')

    report['all_assertions_passed'] = True
    (APP / 'build/validation_stamp.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL STAMP ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
