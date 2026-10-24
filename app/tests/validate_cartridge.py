#!/usr/bin/env python3
"""Emulate the app's read-only cartridge fuse-ROM read (USB 0x62 / 0x63).

Synthetic fuse words and a head-interface gate are served by model_cartridge.py.
The test checks the stock power sequence (connection test, cold preparation,
acquire, release, shutdown), the selector sequence, active-low packing and
complement, the 32/48-bit head widths, field extraction, the repeat read,
request validation, raw mode, a disconnected head, and that the firmware never
writes the fuse-cut GPIO or a non-zero pair word. Waits are modeled time, not
measured time; the model says nothing about the real head electrical state.
"""
import hashlib
import json
import struct
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests'))
sys.path.insert(0, str(APP / 'tests'))
from model import setup                   # noqa: E402
from model_cartridge import CartridgeModel, CTL_ACQ, CTL_COLD, CTL_RES  # noqa: E402
from stock.head_interface import FUSE_CONFIG, PROFILE_CONFIG

LOG = ('log', setup(0xc0, 0x50, 0, 0, 256))
STATUS = ('cart_status', setup(0xc0, 0x63, 0, 0, 64))
RAW, KEEP = 0x100, 0x200

# Synthetic fuse contents: distinctive values in every decoded field.
BLACK = (0x3f            # inklvl bits 0..6
         | 0x5 << 7      # id bits 7..10
         | 0x2a << 11    # rank1 bits 11..16
         | 0x15 << 22    # dest bits 22..27
         | 0x1 << 31)
COLOUR = (0x7f           # inklvl
          | 0x9 << 7     # id
          | 0x13 << 11   # rank1 bits 11..15
          | 0xb << 16    # rank2 bits 16..19
          | 0x2c << 25   # dest bits 25..30
          | 0xbeef << 32)  # high word bits 32..47


def fields(head, low, high):
    return {'id': (low >> 7) & 15,
            'dest': (low >> 25) & 63 if head else (low >> 22) & 63,
            'rank1': (low >> 11) & 31 if head else (low >> 11) & 63,
            'rank2': (low >> 16) & 15 if head else None,
            'inklvl': low & 127, 'high': high}


def app_mode(e):
    return bool(e.info()) and e.info()[2] == 2


def log_text(e):
    return b''.join(d for g, n, d in e.responses if n == 'log' and g == e.generation)


def status(e):
    data = [d for g, n, d in e.responses if n == 'cart_status' and g == e.generation]
    return struct.unpack('<16I', data[-1]) if data else None


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes()
    app = (APP / 'build/application.bin').read_bytes()
    factory = (boot + app).ljust(0x200000, b'\xff')
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
          'symbols': {'bootloader': bs['bootloader'], 'application': asym}}
    report = {'assumptions': __doc__, 'cases': {},
              'application_sha256': hashlib.sha256(app).hexdigest(),
              'boot_region_sha256': hashlib.sha256(boot).hexdigest()}

    def booted(**kw):
        fuse = kw.pop('fuse', {0: BLACK, 1: COLOUR})
        e = CartridgeModel(image=factory, metadata=md, fuse=fuse, **kw)
        e.run(app_mode)
        return e

    def read(e, value, wait=0):
        heads = value & 0xff
        seq = status(e)[5] if status(e) else 0
        e.requests.appendleft(('cart_read', setup(0x40, 0x62, value, wait)))
        done = lambda x: status(x) is not None and status(x)[2] == 2 and status(x)[3] == heads and status(x)[5] == seq + 1
        for _ in range(400):                       # status is answered during the sequence's long waits
            n = sum(1 for _, name, _ in e.responses if name == 'cart_status')
            e.requests.appendleft(STATUS)
            e.run(lambda x, n=n: sum(1 for _, name, _ in x.responses if name == 'cart_status') > n,
                  resume=True, budget=50000000)
            if done(e):
                break
        assert done(e), status(e)
        marker = b'sense_idle' if value & RAW else b'head interface'
        for _ in range(60):
            if marker in log_text(e):
                break
            e.requests.appendleft(LOG)
            e.run(lambda x: True, resume=True, budget=200000)
        e.run(lambda x: marker in log_text(x), resume=True, budget=20000000)
        return status(e)

    def clean(e, powered_off=True):
        assert not e.fusecut_writes, e.fusecut_writes
        assert not e.bad_triggers, e.bad_triggers
        assert all(e.regs.get(a, 0) == 0 for a in (0xc0c2954c, 0xc0c29540, 0xc0c29550))
        if powered_off:
            assert all(e.regs.get(a, 0) == 0 for a in (CTL_ACQ, CTL_COLD, CTL_RES)), \
                {hex(a): e.regs.get(a) for a in (CTL_ACQ, CTL_COLD, CTL_RES)}
        assert not e.erases and not e.programs

    # 1) Both heads through the full stock power sequence, default 300 us wait.
    e = booted()
    s = read(e, 3)
    assert s[0] == 0x5443474d and s[1] == 2 and s[2] == 2 and s[3] == 3 and s[5] == 1, s
    assert s[4] == 28800, s[4]                      # 300 us in 96 MHz ticks
    assert s[6] == 0x0101, hex(s[6])                # connection test: both heads answered
    assert s[7] == 0x20000, hex(s[7])               # no options, not left prepared, head block initialized
    black, colour = s[8:12], s[12:16]
    assert black[0] == black[1] == BLACK & 0xffffffff, [hex(x) for x in black]
    assert black[2] == 0, black
    assert colour[0] == colour[1] == COLOUR & 0xffffffff, [hex(x) for x in colour]
    assert colour[2] == (COLOUR >> 32) & 0xffff == 0xbeef, hex(colour[2])
    assert e.unpowered_triggers == e.unconfigured_triggers == 0
    expect = [(0, b) for b in range(32)] * 2 + [(1, b) for b in range(32)] * 2 + [(1, b) for b in range(32, 48)]
    assert e.fuse_triggers == expect, e.fuse_triggers[:40]
    # Control order: resource up (test), down, up (cold), cold up, acquire up, acquire down, cold down, resource down.
    ctl = [(a, v) for a, v in e.control_writes if a in (CTL_ACQ, CTL_COLD, CTL_RES)]
    acq_writes = [v for a, v in ctl if a == CTL_ACQ]
    assert acq_writes[-3:] == [1, 0, 0] and (CTL_COLD, 1) in ctl and ctl[-1] == (CTL_RES, 0), ctl
    assert ctl.index((CTL_COLD, 1)) < ctl.index((CTL_ACQ, 1)) < ctl.index((CTL_COLD, 0)) < len(ctl) - 1
    clean(e)
    t = log_text(e)
    fb, fc = fields(0, black[0], black[2]), fields(1, colour[0], colour[2])
    assert fb == {'id': 5, 'dest': 0x15, 'rank1': 0x2a, 'rank2': None, 'inklvl': 63, 'high': 0}, fb
    assert fc == {'id': 9, 'dest': 0x2c, 'rank1': 0x13, 'rank2': 0xb, 'inklvl': 127, 'high': 0xbeef}, fc
    assert b'[cart] connection black=1 colour=1' in t, t
    assert b'stable id=5 dest=21 rank1=42 inklvl=63' in t, t
    assert b'stable id=9 dest=44 rank1=19 rank2=11 inklvl=127' in t, t
    assert b'head interface shut down' in t, t
    report['cases']['both_heads_power_sequence'] = {'status': [hex(x) for x in s], 'black': fb, 'colour': fc,
                                                    'controls': [(hex(a), v) for a, v in ctl]}
    print('PASS both_heads_power_sequence', fb, fc, flush=True)

    # 2) Keep prepared, then a second read skips the connection test / cold path.
    s = read(e, 2 | KEEP, 200)
    assert s[3] == 2 and s[4] == 192000 and s[7] == KEEP | 0x10000 | 0x20000, [hex(x) for x in s]
    assert e.regs[CTL_COLD] == 1 and e.regs[CTL_RES] == 1 and e.regs[CTL_ACQ] == 0
    assert s[12:16] == colour
    clean(e, powered_off=False)
    n_ctl = len(e.control_writes)
    s = read(e, 1)
    assert s[7] == 0x20000 and s[8:12] == black
    assert (CTL_COLD, 1) not in e.control_writes[n_ctl:]      # no second cold preparation
    clean(e)
    report['cases']['keep_prepared_then_shutdown'] = {'status': [hex(x) for x in s]}
    print('PASS keep_prepared_then_shutdown', flush=True)

    # 3) Raw mode reproduces the first physical attempt: no power sequence, stuck sense.
    e2 = booted()
    s = read(e2, 3 | RAW)
    assert s[6] == 0 and s[7] == RAW, [hex(x) for x in s]
    assert s[8] == s[9] == 0xffffffff and s[12] == 0xffffffff and s[14] == 0xffff, [hex(x) for x in s]
    assert not e2.control_writes and e2.unpowered_triggers == len(e2.fuse_triggers)
    clean(e2)
    report['cases']['raw_mode_matches_first_attempt'] = {'status': [hex(x) for x in s]}
    print('PASS raw_mode_matches_first_attempt', flush=True)

    # 4) Colour head not answering the connection loop is reported, read still bounded.
    e3 = booted(heads_connected=(True, False))
    s = read(e3, 3)
    assert s[6] == 0x0201, hex(s[6])
    clean(e3)
    report['cases']['colour_connection_failure_reported'] = {'status': [hex(x) for x in s]}
    print('PASS colour_connection_failure_reported', flush=True)

    # 4b) Controls alone and the failed subset must NOT produce valid fuse data.
    e3b = booted()
    s = read(e3b, 1 | 0x800)
    assert s[7] == 0x800 and e3b.regs[0xc0c29590] == 7 and e3b.regs[0xc0c29070] == 0
    assert s[6] == 0x0101 and s[8] == s[9] == 0xffffffff
    assert not e3b.head.config_writes and e3b.unconfigured_triggers == 64
    assert e3b.unpowered_triggers == 0  # control sequence completed, still no response
    cold = list(s)
    clean(e3b)
    for address, value in {**FUSE_CONFIG, **PROFILE_CONFIG}.items():
        e3b.head.write(address, 4, value)  # explicitly seed the earlier partial setup
    s = read(e3b, 1 | 0x800)
    assert s[8] == s[9] == 0xffffffff and e3b.unconfigured_triggers == 128
    subset = list(s)
    clean(e3b)
    # Default firmware fills the rest; subsequent shutdown does not erase config.
    s = read(e3b, 1)
    expect_cfg = [(0xc0c29588, 0), (0xc0c29590, 0x14040007), (0xc0c29598, 0x700), (0xc0c295a0, 0x1f07000b),
                  (0xc0c295a8, 0x07010002), (0xc0c29580, 1), (0xc0c29070, 0x02022033), (0xc0c291d0, 0x3fc),
                  (0xc0c29184, 0)]
    assert all(e3b.regs.get(a) == v for a, v in expect_cfg), {hex(a): e3b.regs.get(a) for a, v in expect_cfg}
    assert s[7] == 0x20000 and s[8] == BLACK & 0xffffffff, [hex(x) for x in s]
    clean(e3b)
    writes = len(e3b.head.config_writes)
    full = list(s)
    s = read(e3b, 1 | 0x800)
    assert s[8] == s[9] == BLACK & 0xffffffff and s[7] == 0x20800
    assert len(e3b.head.config_writes) == writes and e3b.unconfigured_triggers == 128
    clean(e3b)
    report['cases']['head_block_init_and_retention'] = {
        'cold_no_init': [hex(x) for x in cold], 'subset_only': [hex(x) for x in subset],
        'full_init': [hex(x) for x in full], 'retained': [hex(x) for x in s],
        'blocked_samples': e3b.unconfigured_triggers,
        'scope': 'Empirical group contract; individual necessary words not isolated.'}
    print('PASS head_block_init_and_retention', flush=True)

    # 4c) Replay captured data as INPUTS, then check real ARM packing/USB output.
    capture = APP / 'evidence/cartridge/read_default_after_fix.json'
    heads = json.loads(capture.read_text())['heads']
    fuse = {i: int(heads[n]['raw_low'], 16) | int(heads[n]['raw_high'], 16) << 32
            for i, n in enumerate(('black', 'colour'))}
    ep = booted(fuse=fuse)
    s = read(ep, 3)
    for i, name in enumerate(('black', 'colour')):
        got = s[8 + i * 4:12 + i * 4]
        expected = heads[name]
        assert got[:3] == tuple(int(expected[k], 16)
                               for k in ('raw_low', 'raw_low_repeat', 'raw_high'))
        assert all(expected['fields'][k] == v for k, v in fields(i, got[0], got[2]).items())
    assert ep.unconfigured_triggers == ep.unpowered_triggers == 0
    clean(ep)
    report['cases']['captured_words_fixture'] = {
        'input': str(capture.relative_to(APP)), 'input_sha256': hashlib.sha256(capture.read_bytes()).hexdigest(),
        'status': [hex(x) for x in s],
        'scope': 'Captured words supplied to model; verifies packing, not independent hardware discovery.'}
    print('PASS captured_words_fixture', flush=True)

    # 5) Rejected requests stall: no heads, bad mask, unknown option, wait above the cap.
    e4 = booted()
    for name, value, index in (('no_heads', 0, 0), ('bad_mask', 4, 0), ('bad_option', 0x1001, 0),
                               ('wait_too_long', 1, 201)):
        e4.requests.appendleft((name, setup(0x40, 0x62, value, index)))
        e4.run(lambda x, n=name: n in x.stalls, resume=True)
    assert not e4.fuse_triggers and not e4.control_writes
    e4.requests.append(STATUS)
    e4.run(lambda x: status(x) is not None, resume=True)
    assert status(e4)[2] == 0 and status(e4)[3] == 0
    clean(e4)
    report['cases']['invalid_requests_stall'] = {'stalls': e4.stalls}
    print('PASS invalid_requests_stall', flush=True)

    # 6) Management stays alive: INFO answered during the long settle, reboot after.
    e5 = booted()
    e5.requests.appendleft(('cart_read', setup(0x40, 0x62, 1)))
    e5.run(lambda x: any(n == 'cart_read' for _, n, _ in x.responses), resume=True, budget=50000000)
    for _ in range(400):
        n = sum(1 for _, name, _ in e5.responses if name == 'cart_status')
        e5.requests.appendleft(STATUS)
        e5.run(lambda x, n=n: sum(1 for _, name, _ in x.responses if name == 'cart_status') > n,
               resume=True, budget=50000000)
        if status(e5)[2] == 2:
            break
    assert status(e5)[2] == 2 and status(e5)[3] == 1
    # The whole sequence runs inside one cartridge_poll (predicates are only evaluated in the main
    # loop), so INFO replies recorded between the request and the first status reply prove that
    # usb_poll ran during the sequence's waits.
    names = [n for _, n, _ in e5.responses]
    between = names[names.index('cart_read') + 1:names.index('cart_status')]
    assert between.count('info') >= 5, ('management not served during the sequence', between.count('info'))
    before = e5.resets
    e5.requests.appendleft(('reboot_boot', setup(0x40, 0x41, 1)))
    e5.run(lambda x: x.resets == before + 1 and bool(x.info()) and x.info()[2] == 1 and x.info()[3] & 1,
           resume=True)
    assert bytes(e5.uc.mem_read(0, 0x10000)) == boot
    report['cases']['management_during_and_after'] = e5.summary()
    print('PASS management_during_and_after', flush=True)

    report['all_assertions_passed'] = True
    (APP / 'build/validation_cartridge.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL CARTRIDGE ASSERTIONS PASSED', flush=True)


if __name__ == '__main__':
    main()
