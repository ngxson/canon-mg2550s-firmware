#!/usr/bin/env python3
"""Emulate the stationary black prefire (USB 0x81 / 0x82) against the brief's register list.

The oracle below is the ordered stock sequence from fw_dump/gpt/BRIEF_head_fire.md
(response section 4, steps 1..12, ROM work ID 24) with the waveform rows of
BRIEF_head_drive.md section 3, written down independently of the firmware. The
test checks: refusal before a cartridge read, with a wrong rank, with an
unreferenced carriage, with out-of-range parameters; the exact write sequence
for the default request (ambient 50: black selector 3, colour selector 2); two
completions with mask 3 and count-1 = 99 on lanes 0/1 while the interface is
acquired and configured; ambient bins choosing other rows; a reduced count; the
dry run never arming; the timeout path (no completion) cleaning up and
disarming; owner release and shutdown afterwards; the fuse-cut GPIO never
written; and management served during the sequence. Modeled time only.
"""
import hashlib, json, struct, sys
from pathlib import Path
APP = Path(__file__).resolve().parents[1]; BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests'))
from model import setup  # noqa: E402
from model_head import HeadModel, HE_STATUS, HE_CTRL, HE_ARM, HE_SELECT, ENGINE_LO, ENGINE_HI, CR_WORK  # noqa: E402
from model_cartridge import CTL_ACQ, CTL_COLD, CTL_RES  # noqa: E402

LOG = ('log', setup(0xc0, 0x50, 0, 0, 256))
KEEP = 0x200
# Fuse words carrying the ranks the embedded waveform rows were computed for.
BLACK37 = 0x3f | 5 << 7 | 37 << 11 | 0x15 << 22
COLOUR16_7 = 0x7f | 9 << 7 | 16 << 11 | 7 << 16 | 0x2c << 25 | 0xbeef << 32
BLACK40 = 0x3f | 5 << 7 | 40 << 11 | 0x15 << 22

# Brief 4 section 3 rows (P0..4 then tail); entries 5..15 repeat P4.
BK37 = {2: ([0x002000df, 0x001e00d8, 0x001b00ce, 0x001b00ce, 0x001b00ce], 0x0014014c),
        3: ([0x002900e5, 0x002800df, 0x002600d6, 0x002600d6, 0x002600d6], 0x0014014c),
        4: ([0x003300ec, 0x003200e6, 0x003100dd, 0x003100dd, 0x003100dd], 0x0014014c)}
LCL16 = {1: ([0x001a0051, 0x0018004d, 0x00160049, 0x00140048, 0x00140046], 0x001400ae),
         2: ([0x0025005a, 0x00240056, 0x00220053, 0x00210050, 0x001f004c], 0x001400ae),
         3: ([0x002f0062, 0x002e005f, 0x002d005c, 0x002c005a, 0x002b0057], 0x001400ae),
         4: ([0x00380069, 0x00380067, 0x00370065, 0x00370063, 0x00360060], 0x001400ae)}
SCL13 = {1: ([0x0020005d, 0x0020005a, 0x001f0057, 0x001f0057, 0x001e0055], 0x001400ae),
         2: ([0x00280063, 0x00280061, 0x0027005e, 0x0027005e, 0x0026005c], 0x001400ae),
         3: ([0x00310069, 0x00310067, 0x00300065, 0x00300065, 0x00300063], 0x001400ae),
         4: ([0x0037006f, 0x0037006d, 0x0037006b, 0x0037006b, 0x00370069], 0x001400ae)}
SELECTORS = {30: (4, 4), 40: (3, 3), 50: (3, 2), 55: (2, 2), 70: (2, 1)}   # ambient -> (bk, cl)
RECORDS = [(3, 0, 0xfffffc00, 0xffffffff), (0, 3, 0xffffffff, 0xffffffff)]  # sel_a, sel_b, +18, +1c (black ID 0x24)
COLOUR_RECORDS = [(0xa4, 0xa4, 0xffffffff, 0xfffffc00), (0x58, 0x58, 0xffffffff, 0xfffffc00)]  # colour ID 0x25


def expand(row):
    p, tail = row
    return [p[j if j < 5 else 4] for j in range(16)], tail


def oracle(bk_sel, cl_sel, count, mode='fire', records=(0, 1), colour=False, custom=None):
    """Ordered (addr, value) writes to the engine block and CR work words, brief steps 1..12."""
    t = []
    t += [(0xc0c29230, 0x03000300), (0xc0c29234, 0x00030003), (0xc0c29238, 0), (0xc0c29280, 0), (0xc0c29284, 0),
          (0xc0c29288, 0), (0xc0c29200, 0), (0xc0c29204, 0)]                                     # 1
    for base, tail_addr, row in ((0xc0c29850, 0xc0c295dc, BK37[bk_sel]), (0xc0c29700, 0xc0c295d0, LCL16[cl_sel]),
                                 (0xc0c297e0, 0xc0c295d8, SCL13[cl_sel])):                        # 2
        words, tail = expand(row)
        t += [(base + 4 * j, words[j]) for j in range(16)] + [(tail_addr, tail)]
    t += [(0xc0c29190, 0), (0xc0c29580, 1), (0xc0c291a0, 0), (0xc0c295f0, 0), (0xc0c29584, 0),
          (0xc0c295b0, 0x080c0009), (0xc0c29600, 0x00020002)]                                     # 3
    t += [(0xc0c29608, 1), (0xc0c29d50, 0)]                                                        # 4
    sh = {HE_STATUS: 0, HE_CTRL: 0, 0xc0c29ad0: 0}                                                 # RMW shadow

    def rmw(a, clear, set_):
        sh[a] = (sh[a] & ~clear) | set_; return (a, sh[a])
    recs = [(3, 0) + tuple(custom)] if custom else (COLOUR_RECORDS if colour else RECORDS)
    for r, (sel_a, sel_b, w18, w1c) in enumerate(recs):
        if r not in records: continue
        union = sel_a | sel_b
        t += [(0xc0c29ae4, (((16000 if union & 3 else 9600) - 1) >> 4) - 1)]                     # 5
        t += [(0xc0c29ab0, 0), (0xc0c29ac8, 0), rmw(0xc0c29ad0, 0, 1), (0xc0c29aa0, union)]       # 6
        t += [(0xc0c29174, 0x00121c12), (0xc0c291e0, w18), (0xc0c291e8, w1c), (0xc0c29ac0, sel_b),
              (0xc0c290a0, 0), (0xc0c290b0, 0), (0xc0c290c0, 0)]                                   # 7
        t += [(0xc0c29b10 + 4 * i, 0) for i in range(10)]                                          # 8
        t += [(0xc0c29b10 + 4 * i, count - 1) for i in range(10) if union >> i & 1] + [(0xc0c29174, 0x00121c12)]
        if mode == 'dry':
            continue
        t += [rmw(HE_STATUS, 1, 0), rmw(HE_CTRL, 0, 1), (HE_ARM, 1), (HE_SELECT, union)]           # 9
        if mode == 'timeout':
            break
        sh[HE_STATUS] |= 1                                                                        # engine completion
        t += [rmw(HE_CTRL, 1, 0), rmw(HE_STATUS, 1, 0), (HE_ARM, 0)]                               # 10
    t += [rmw(HE_CTRL, 1, 0), rmw(HE_STATUS, 1, 0), (0xc0c29aa0, 0), (0xc0c29ac0, 0), (0xc0c29ab0, 0),
          (0xc0c29ac8, 0), (CR_WORK[0], 0), (CR_WORK[1], 0)]                                       # 12
    if mode == 'timeout':
        t += [(HE_ARM, 0), (HE_SELECT, 0)]                                                         # non-stock disarm
    t += [(0xc0c290b0, 0xfc)]                                                                      # non-stock: restore the init word
    return t


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes(); app = (APP / 'build/application.bin').read_bytes()
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]}, 'symbols': {'bootloader': bs['bootloader'], 'application': asym}}
    report = {'assumptions': __doc__, 'cases': {}, 'application_sha256': hashlib.sha256(app).hexdigest()}

    def booted(fuse=None, **kw):
        e = HeadModel(image=(boot + app).ljust(0x200000, b'\xff'), metadata=md,
                      fuse={0: BLACK37, 1: COLOUR16_7} if fuse is None else fuse, **kw)
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

    def cart_read(e):
        seq = poll(e, 'cart_status', 0x63, 16, 64)[5]
        command(e, 'cart_read', 0x62, 3 | KEEP, 0)
        for _ in range(400):
            s = poll(e, 'cart_status', 0x63, 16, 64)
            if s[2] == 2 and s[3] == 3 and s[5] == seq + 1: return s
        raise AssertionError(('cartridge read did not finish', s))

    def home(e):
        command(e, 'home', 0x6c)
        for _ in range(6000):
            s = poll(e, 'cr_status', 0x6e, 12, 48)
            if s[2] == 0 and s[3] != 1: assert s[3] == 2 and s[10] & 1, s; return
        raise AssertionError('homing did not finish')

    hf = lambda e: poll(e, 'hf_status', 0x82, 20, 80)

    def drain_log(e):
        n = sum(1 for _, nm, _ in e.responses if nm == 'log')
        e.requests.appendleft(LOG)
        e.run(lambda x, n=n: sum(1 for _, nm, _ in x.responses if nm == 'log') > n, resume=True, budget=50000000)

    def log_text(e):
        return b''.join(d for g, n, d in e.responses if n == 'log' and g == e.generation)

    def drain_all(e):
        for _ in range(64):
            n = len(e.responses); drain_log(e)
            if all(not d for _, nm, d in e.responses[n:] if nm == 'log'): return
        raise AssertionError('log ring never empty')

    def fire(e, value=0, index=0):
        seq = hf(e)[17]; drain_all(e)
        command(e, 'fire', 0x81, value, index)
        for _ in range(600):                      # status is answered during the sequence's waits
            s = hf(e); drain_log(e)
            if s[2] == 2 and s[17] == seq + 1:
                for _ in range(8): drain_log(e)
                return s
        raise AssertionError(('prefire did not finish', s))

    def engine(trace):
        return [(a, v) for a, v in trace if (ENGINE_LO <= a < ENGINE_HI and not 0xc0c29540 <= a <= 0xc0c29550) or a in CR_WORK]

    def check_sequence(e, n0, bk_sel, cl_sel, count, mode='fire', records=(0, 1), colour=False, custom=None):
        t = e.trace[n0:]
        got, want = engine(t), oracle(bk_sel, cl_sel, count, mode, records, colour, custom)
        for i, (g, w) in enumerate(zip(got, want)):
            assert g == w, ('step mismatch at write %d: got %s want %s' % (i, (hex(g[0]), hex(g[1])), (hex(w[0]), hex(w[1]))))
        assert len(got) == len(want), (len(got), len(want), [(hex(a), hex(v)) for a, v in got[len(want):][:6]])
        acq = [i for i, w in enumerate(t) if w == (CTL_ACQ, 1)]; rel = [i for i, w in enumerate(t) if w == (CTL_ACQ, 0)]
        first = next(i for i, w in enumerate(t) if w == got[0]); last = max(i for i, w in enumerate(t) if w == got[-1])
        assert acq and acq[-1] < first, 'engine written before the owner acquire'
        assert any(i > last for i in rel), 'owner not released after the sequence'
        return got

    def clean(e, shut=True):
        assert not e.fusecut_writes and not e.bad_triggers
        assert e.regs.get(HE_ARM, 0) == 0 and e.regs.get(HE_CTRL, 0) & 1 == 0 and not e.work_armed
        assert e.regs.get(0xc0c29aa0) == 0 and e.regs.get(0xc0c51d14) == 0 and e.regs.get(0xc0c51d1c) == 0
        if shut: assert all(e.regs.get(a, 0) == 0 for a in (CTL_ACQ, CTL_COLD, CTL_RES)), {hex(a): e.regs.get(a) for a in (CTL_ACQ, CTL_COLD, CTL_RES)}
        assert not e.erases and not e.programs
        assert e.regs.get(0xc0c50000) == 0
        assert not e.head.missing_config(), e.head.missing_config()

    # 1) Refusals: before any cartridge read (carriage homed), then before homing.
    e = booted(); home(e)
    command(e, 'fire_norank', 0x81, 0, 0, expect_stall=True)
    s = hf(e); assert s[2] == 0 and s[3] == 3, s          # rank refusal
    assert not engine(e.trace) and not e.arms
    e_unref = booted(); cart_read(e_unref)
    command(e_unref, 'fire_unref', 0x81, 0, 0, expect_stall=True)
    s = hf(e_unref); assert s[3] == 4, s                  # carriage refusal
    assert not e_unref.arms
    for name, value, index in (('count_low', 14, 0), ('count_high', 101, 0), ('bad_opt', 0x4000, 0), ('colour_count_high', 0x1000 | 101, 0), ('ambient_high', 0, 201)):
        command(e, name, 0x81, value, index, expect_stall=True); assert hf(e)[3] == 8, name
    report['cases']['refusals'] = {'stalls': e.stalls + e_unref.stalls}
    print('PASS refusals (no rank, no carriage reference, parameters)')

    # 2) Default request: cartridge read (37 / 16,7), carriage homed, ambient 50 -> BK 3, CL 2.
    s = cart_read(e); assert s[8] == BLACK37 and s[12] == COLOUR16_7 & 0xffffffff, [hex(x) for x in s]
    n0 = len(e.trace); s = fire(e)
    assert s[0] == 0x4648474d and s[1] == 1 and s[3] == 2 and s[4] == 100 and s[5] == 2 and s[6] == 3, [hex(x) for x in s]
    assert s[7] == 50 and s[8] == 3 and s[9] == 2, s[7:10]
    assert s[10] == 732 and s[11] == 731 and s[12] == 732 and s[13] == 731, s[10:14]
    assert s[16] == 0 and s[18] == 0, (hex(s[16]), s[18])          # shut down afterwards, status bit cleared
    got = check_sequence(e, n0, 3, 2, 100)
    assert len(e.fires) == 2 and len(e.arms) == 2, (len(e.fires), len(e.arms))
    for f in e.fires:
        assert f['mask'] == 3 and f['union'] == 3 and f['interval'] == 998, f
        assert f['counts'] == [99, 99] + [0] * 8, f['counts']
        # During the stock prefire the mode-8 word 0xc0c290b0 differs from the block-init group; controls are held.
        assert f['controls'] and f['acquired'] and f['commit'] == 1 and f['missing'] == ['0xc0c290b0'], f
        assert f['bk'] == expand(BK37[3])[0] and f['bk_tail'] == 0x0014014c
        assert f['lcl'] == expand(LCL16[2])[0] and f['scl'] == expand(SCL13[2])[0]
    assert e.fires[0]['rec18'] == 0xfffffc00 and e.fires[0]['sel_b'] == 0
    assert e.fires[1]['rec18'] == 0xffffffff and e.fires[1]['sel_b'] == 3
    assert all(a['acquired'] and a['controls'] for a in e.arms)
    clean(e)
    assert not e.head.missing_config(), e.head.missing_config()      # init group restored after the sequence
    s2 = cart_read(e); assert s2[8] == BLACK37 and e.unconfigured_triggers == 0   # fuse read still works afterwards
    t = log_text(e)
    assert b'[head] prefire count=100 ambient/2C=50 bk_sel=3 cl_sel=2' in t, t[-600:]
    assert b'[head] done result=2 records=2' in t, t[-600:]
    report['cases']['default_prefire'] = {'status': [hex(x) for x in s], 'engine_writes': len(got),
                                          'fires': [{k: ([hex(w) if isinstance(w, int) else w for w in v] if isinstance(v, list) else v) for k, v in f.items()} for f in e.fires]}
    print('PASS default_prefire: %d ordered engine writes match the brief, 2 completions mask 3 count 100' % len(got))

    # 3) Ambient bins select the other rows; reduced count programs count-1.
    for amb, (bk, cl) in SELECTORS.items():
        n0 = len(e.trace); nf = len(e.fires); s = fire(e, 50, amb)
        assert s[3] == 2 and s[8] == bk and s[9] == cl and s[4] == 50, (amb, s[3:10])
        check_sequence(e, n0, bk, cl, 50)
        assert e.fires[nf]['counts'][:2] == [49, 49] and e.fires[nf]['bk'] == expand(BK37[bk])[0]
        assert e.fires[nf]['lcl'] == expand(LCL16[cl])[0] and e.fires[nf]['scl'] == expand(SCL13[cl])[0]
        clean(e)
    report['cases']['ambient_bins'] = {str(k): v for k, v in SELECTORS.items()}
    print('PASS ambient_bins', SELECTORS)

    # 4) Dry run: all configuration writes, never armed, kept prepared.
    n0 = len(e.trace); na = len(e.arms); s = fire(e, 0x100 | KEEP, 0)
    assert s[3] == 6 and s[5] == 2 and s[16] == 0x300 | 0x10000, [hex(x) for x in s]
    check_sequence(e, n0, 3, 2, 100, 'dry')
    assert len(e.arms) == na and e.regs.get(HE_ARM, 0) == 0
    clean(e, shut=False); assert e.regs[CTL_COLD] == 1 and e.regs[CTL_RES] == 1 and e.regs[CTL_ACQ] == 0
    report['cases']['dry_run'] = {'status': [hex(x) for x in s]}
    print('PASS dry_run (no arm, interface left prepared)')

    # 4b) Single-record options: record 1 only, record 2 only; both together refused.
    for opt, rec, w18, selb in ((0x400, (0,), 0xfffffc00, 0), (0x800, (1,), 0xffffffff, 3)):
        n0 = len(e.trace); nf = len(e.fires); s = fire(e, opt, 0)
        assert s[3] == 2 and s[5] == 1 and s[16] == opt, [hex(x) for x in s]
        check_sequence(e, n0, 3, 2, 100, 'fire', rec)
        assert len(e.fires) == nf + 1 and e.fires[nf]['rec18'] == w18 and e.fires[nf]['sel_b'] == selb and e.fires[nf]['mask'] == 3
        clean(e)
    command(e, 'both_only', 0x81, 0xc00, 0, expect_stall=True); assert hf(e)[3] == 8
    print('PASS single_record_options')

    # 4c) Colour list ID 0x25: lanes 2,5,7 then 3,4,6, count 600, interval 598, same banks.
    n0 = len(e.trace); nf = len(e.fires); s = fire(e, 0x1000, 0)
    assert s[3] == 2 and s[4] == 600 and s[5] == 2 and s[6] == 0xfc and s[16] == 0x1000, [hex(x) for x in s]
    check_sequence(e, n0, 3, 2, 600, 'fire', colour=True)
    f1, f2 = e.fires[nf], e.fires[nf + 1]
    assert f1['mask'] == 0xa4 and f1['interval'] == 598 and f1['counts'] == [0, 0, 599, 0, 0, 599, 0, 599, 0, 0], f1
    assert f2['mask'] == 0x58 and f2['counts'] == [0, 0, 0, 599, 599, 0, 599, 0, 0, 0] and f2['rec18'] == 0xffffffff
    assert e.regs.get(0xc0c291e8) == 0xfffffc00 and f1['lcl'] == expand(LCL16[2])[0]
    clean(e)
    n0 = len(e.trace); s = fire(e, 0x1000 | 0x400 | 50, 0)          # record 1 only, count 300
    assert s[3] == 2 and s[4] == 300 and s[5] == 1; check_sequence(e, n0, 3, 2, 300, 'fire', (0,), colour=True); clean(e)
    print('PASS colour_list_id25')

    # 4d) Custom record words (experiment): one black record with host-set 291e0/291e8.
    command(e, 'word_a', 0x83, 0xfffe, 0xffff); command(e, 'word_b', 0x84, 0x0000, 0x8000)
    n0 = len(e.trace); nf = len(e.fires); s = fire(e, 0x2000 | 15, 0)
    assert s[3] == 2 and s[4] == 15 and s[5] == 1 and s[16] == 0x2000, [hex(x) for x in s]
    check_sequence(e, n0, 3, 2, 15, 'fire', (0,), custom=(0xfffffffe, 0x80000000))
    assert e.fires[nf]['rec18'] == 0xfffffffe and e.regs.get(0xc0c291e8) == 0x80000000 and e.fires[nf]['mask'] == 3 and e.fires[nf]['counts'][:2] == [14, 14]
    clean(e)
    command(e, 'custom_colour', 0x81, 0x2000 | 0x1000, 0, expect_stall=True); assert hf(e)[3] == 8
    print('PASS custom_record_words')

    # 5) Wrong black rank: refused even after a complete read.
    e2 = booted(fuse={0: BLACK40, 1: COLOUR16_7}); home(e2); cart_read(e2); n0 = len(e2.trace)
    command(e2, 'fire_rank40', 0x81, 0, 0, expect_stall=True)
    assert hf(e2)[3] == 3 and not e2.arms and not engine(e2.trace[n0:]), (hf(e2)[3], e2.arms, engine(e2.trace[n0:]))
    print('PASS wrong_rank_refused')

    # 6) Timeout: the engine never completes -> one arm, stock cleanup, disarm, release, shutdown.
    e3 = booted(never_complete=True); home(e3); cart_read(e3)
    n0 = len(e3.trace); s = fire(e3)
    assert s[3] == 5 and s[5] == 0 and s[19] >= 1000, [hex(x) for x in s]
    check_sequence(e3, n0, 3, 2, 100, 'timeout')
    assert len(e3.arms) == 1 and not e3.fires
    clean(e3)
    report['cases']['timeout'] = {'status': [hex(x) for x in s]}
    print('PASS timeout_cleanup wait_ms=%d' % s[19])

    # 7) Management stays alive during the blocking sequence; reboot works afterwards.
    e4 = booted(); home(e4); cart_read(e4)
    e4.requests.appendleft(('fire', setup(0x40, 0x81, 0, 0)))
    e4.run(lambda x: any(n == 'fire' for _, n, _ in x.responses), resume=True, budget=50000000)
    for _ in range(600):
        if hf(e4)[2] == 2: break
    names = [n for _, n, _ in e4.responses]
    between = names[names.index('fire') + 1:names.index('hf_status')]
    assert between.count('info') >= 3, ('management not served during the sequence', between)
    before = e4.resets
    e4.requests.appendleft(('reboot_boot', setup(0x40, 0x41, 1)))
    e4.run(lambda x: x.resets == before + 1 and bool(x.info()) and x.info()[2] == 1 and x.info()[3] & 1, resume=True)
    assert bytes(e4.uc.mem_read(0, 0x10000)) == boot
    report['cases']['management'] = e4.summary()
    print('PASS management_during_and_after')

    report['all_assertions_passed'] = True
    (APP / 'build/validation_head_fire.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL HEAD FIRE ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
