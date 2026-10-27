#!/usr/bin/env python3
"""One stock band on paper through the hybrid shim (owner present).

Sequence: home the carriage, power and prepare the head (cartridge read with
keep), feed a sheet to the stock-like handoff and hold, fresh band-prepare
(stock RAM back to the boot snapshot, so the executor's first pass is the
forward one), upload the labelled test raster, run the executor live (the
carriage sweep is ours, firing is stock's position-window engine), eject,
home. Every sheet gets its own label (default HYB + HHMM).

    band_live.py                       # the whole sequence, one sheet in the tray
    band_live.py --motion-only         # sweep with firing disarmed, no paper needed (--no-feed)
    band_live.py --no-feed --no-eject  # sheet already positioned / keep it
"""
import argparse, json, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hybrid_call, band_pattern


def collect_head_counts(dev):
    """Stock c5820's selector -> latch/reset -> read, without its bookkeeping.

    Reading consumes the accumulated counts. These are ASIC counters, not
    proof that ink landed on paper. No head enable or motor write is issued.
    """
    def write(address, value):
        dev.ctrl_transfer(0x40, 0x90, address & 0xffff, address >> 16, b'', timeout=1000)
        dev.ctrl_transfer(0x40, 0x92, value & 0xffff, value >> 16, b'', timeout=1000)
    counts = {}
    hybrid_call.unlock(dev)
    try:
        for lane in (0, 1, 2, 3, 5, 4, 7, 6):
            write(0xc0c29d48, lane)
            write(0xc0c29d44, 0x101)
            counts[str(lane)] = hybrid_call.read_words(dev, 0xc0c29d40)[0]
    finally:
        hybrid_call.lock(dev)
    return counts


def verify_band_feed_init(dev):
    """Require stock's boot defaults before submitting a band to the head."""
    groups = ((0xc0c28180, [31, 31]),
              (0xc0c28300, [40 * i for i in range(8)] + [0, 0]),
              (0xc0c28400, [40] * 8 + [0, 0]))
    for address, expected in groups:
        actual = hybrid_call.read_words(dev, address, len(expected))
        if actual != expected:
            raise RuntimeError(f'raster-feed initialization missing at {address:#x}: '
                               f'got {actual}, expected {expected}; executor not submitted')
    print('  raster-feed boot defaults verified (28180/184, lane offsets and sizes)')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--label', default='HYB' + time.strftime('%H%M'))
    ap.add_argument('--motion-only', action='store_true')
    ap.add_argument('--no-feed', action='store_true')
    ap.add_argument('--no-eject', action='store_true')
    ap.add_argument('--no-head', action='store_true', help='skip the cartridge power/prepare step')
    ap.add_argument('--our-cr', action='store_true', help='sweep with our carriage driver instead of stock\'s CR task and servo')
    ap.add_argument('--diagnostic-output', type=Path, help='collect/reset stock head counters before/after the band and save the results')
    ap.add_argument('--control-mark', action='store_true', help='after the band, run the proven bounded black prefire at the stopped carriage position; requires --diagnostic-output')
    ap.add_argument('--advance-ulf', type=int, default=0, help='extra forward stock-LF travel after load (0..1200 Ulf); also applies with --no-feed to a held sheet')
    ap.add_argument('--ambient', type=int, default=50)
    ap.add_argument('--feed-at', type=int, default=8000, help='carriage x (counts from the park wall) during the feed; 0 = stay at the wall')
    ap.add_argument('--serial')
    a = ap.parse_args()
    if a.control_mark and (not a.diagnostic_output or a.motion_only or a.no_head):
        ap.error('--control-mark requires --diagnostic-output and a normal head-prepared pass')
    if not 0 <= a.advance_ulf <= 1200 or (a.advance_ulf and a.motion_only):
        ap.error('--advance-ulf must be 0..1200 and cannot accompany --motion-only')
    dev = hybrid_call.device(a.serial)
    log = bytearray()
    diagnostic = {'label': a.label, 'control_mark_requested': a.control_mark}

    def record(name, data):
        diagnostic[name] = data
        a.diagnostic_output.parent.mkdir(parents=True, exist_ok=True)
        a.diagnostic_output.write_text(json.dumps(diagnostic, indent=2) + '\n')

    def drain():
        for _ in range(16):
            d = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
            if not d: break
            log.extend(d)

    def wait(req, n_words, length, limit=30):
        t0 = time.monotonic()
        while True:
            w = struct.unpack('<%dI' % n_words, bytes(dev.ctrl_transfer(0xc0, req, 0, 0, length, timeout=1000)))
            if w[2] == 0 and w[3] != 1: return w
            if time.monotonic() - t0 > limit: raise TimeoutError(hex(req))
            drain(); time.sleep(0.03)

    def home():
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); w = wait(0x6e, 12, 48)
        print(f'  carriage home: result {w[3]} raw {w[11]:#x}'); assert w[3] == 2, w

    def cr_move(x):
        dev.ctrl_transfer(0x40, 0x6d, x // 64, 0, b'', timeout=1000); w = wait(0x6e, 12, 48)
        print(f'  carriage move to {x}: result {w[3]} x {w[4]}'); assert w[3] == 2, w

    def feed(mode, units=0):
        dev.ctrl_transfer(0x40, 0x69, mode, units, b'', timeout=1000); w = wait(0x6a, 12, 48, 60)
        print(f'  feed mode {mode}: result {w[3]}'); assert w[3] == 2, ('feed', w)

    def cart_read():
        before = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
        dev.ctrl_transfer(0x40, 0x62, 3 | 0x200, 0, b'', timeout=1000)
        t0 = time.monotonic()
        while True:
            s = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
            if s[2] == 2 and s[5] != before[5]: break
            if time.monotonic() - t0 > 30: raise TimeoutError('cartridge')
            drain(); time.sleep(0.05)
        print(f'  head: connection {s[6] & 0xff}/{s[6] >> 8}, prepared {bool(s[7] & 0x10000)}')

    def run(routine, flags=0, index=None):
        status = lambda: struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x98, 0xffff, 0, 64, timeout=5000)))
        before = status()[2]
        dev.ctrl_transfer(0x40, 0x97, routine | flags, a.ambient if index is None else index, b'', timeout=2000)
        t0 = time.monotonic()
        while status()[2] == before:
            if time.monotonic() - t0 > 120: raise TimeoutError('routine')
            time.sleep(0.1)
        st = status()
        rows = [struct.unpack('<4I', bytes(dev.ctrl_transfer(0xc0, 0x98, i, 0, 16, timeout=2000))) for i in range(min(st[4], 160))]
        moves = [(r[1], hex(r[2]), r[3]) for r in rows if r[0] == 1]
        print(f'  routine {routine}|{flags:#x}: result {st[1]:#x} errors {st[3]} exceptions {st[11]} ({time.monotonic() - t0:.1f}s) CR moves {moves}')
        assert st[1] == (0 if routine in (8, 9, 10) else 1), st
        return st

    def control_mark():
        # Existing head_fire.c ID 24, two stock records of 100 pulses. No
        # carriage/feed command: preserve the stock pass's stopped position.
        def status():
            return struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))
        before = status()[17]
        dev.ctrl_transfer(0x40, 0x81, 100, a.ambient, b'', timeout=1000)
        t0 = time.monotonic()
        while True:
            st = status(); drain()
            if st[17] != before or st[3] not in (0, 1): break
            if time.monotonic() - t0 > 20: raise TimeoutError('stationary control mark')
            time.sleep(0.05)
        data = dict(result=st[3], records=st[5], sequence=st[17], x=st[15],
                    adc_before=list(st[10:12]), adc_after=list(st[12:14]),
                    elapsed_ms=st[14], last_record_ms=st[19])
        record('stationary_control', data)
        print('  stationary control (separate from band):', data)
        assert st[3] == 2 and st[5] == 2 and st[17] != before, st

    print('label', a.label)
    print('1. home'); home()
    if not a.no_head and not a.motion_only:
        print('2. head power and prepare'); cart_read()
    if not a.no_feed:
        print('3. stock paper load (routine 8, stock LF task + servo, CR live)'); run(8, 0x100 | 0x800 | (0 if a.our_cr else 0x1000))
        asf = hybrid_call.read_words(dev, 0x180c6f54)[0] & 0xff
        print(f'  stock ASF state byte 0x180c6f54 = {asf} (1 = sheet loaded and aligned)')
        if asf != 1: sys.exit('stock loader did not load a sheet (tray empty or pickup failed); nothing printed')
    if a.advance_ulf:
        print(f'3b. extra stock paper advance: {a.advance_ulf} Ulf')
        run(10, 0x800, index=a.advance_ulf)
    print('4. fresh band-prepare'); run(5, 0x400)
    verify_band_feed_init(dev)
    print('5. raster upload'); r = hybrid_call.upload_raster(dev, band_pattern.encode(band_pattern.build(a.label))); print('  ', r); assert r['mismatches'] == 0, r
    if a.diagnostic_output:
        record('upload', r)
        record('counts_before_band', collect_head_counts(dev))
    print('6. executor live' + (' (motion only)' if a.motion_only else '')); run(7, 0x100 | (0x200 if a.motion_only else 0) | (0 if a.our_cr else 0x1000))
    if a.diagnostic_output:
        counts = collect_head_counts(dev); record('counts_band', counts)
        print('  band counters:', counts)
    if a.control_mark:
        print('6b. stationary control mark at the stopped carriage position')
        control_mark()
        counts = collect_head_counts(dev); record('counts_control', counts)
        print('  stationary control counters:', counts)
    drain(); print(''.join('  ' + l + '\n' for l in log.decode(errors='replace').splitlines() if '[shim]' in l or 'frame' in l or '[cr] done' in l), end='')
    if not a.no_feed and not a.no_eject:
        print('7. stock eject (routine 9)'); run(9, 0x100 | 0x800 | 0x400)
    print('8. home'); home()


if __name__ == '__main__':
    main()
