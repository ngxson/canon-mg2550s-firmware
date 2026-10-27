#!/usr/bin/env python3
"""Print a full test page through the hybrid shim: stock loader, stock LF
steps between bands, stock CR servo and band executor per band, stock eject.
The host is the band scheduler (owner present).

Sequence: home, head power/prepare (cartridge read with keep), stock load
(routine 8), first-line advance (routine 10, `--first-ulf`), then per band:
band geometry (0x9a/0x9b), fresh band-prepare (routine 5 | fresh), raster
upload, executor live with stock CR (routine 7), paper advance by one band
(`--band-ulf`, 2 Ulf per 600 dpi row). Finally stock eject (routine 9) and
home. One status line per band; every page carries its own label.

    page_live.py                         # 20 bands, sheet in the tray
    page_live.py --bands 2 --no-eject    # short test, keep the sheet
    page_live.py --no-feed --first-ulf 0 # sheet already under the head
"""
import argparse, json, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hybrid_call, page_pattern
from band_live import verify_band_feed_init

STOCK_CAP = 2688          # shim: raw CR counter preset at the park wall
PLACE_K = 34272           # window start (raw counts) = 4 * place + PLACE_K, measured in the emulator (validate_hybrid wide band; 34080 while band_run zeroed stock's cached common offset, package 85a4560c / sheet PG2009)
PAPER_X0 = 29000          # sheet edge at the low-count end of our carriage frame (stamp_plan, PLACED scan)


def place_for_x(x_start):
    """Stock common placement Q+0x3a that puts column 0 at our carriage count
    x_start. Stock quantizes the window start to 64 raw counts (4 columns), so
    x_start is rounded to the nearest 64-count raw position first."""
    raw = (x_start + STOCK_CAP + 32) // 64 * 64
    return (raw - PLACE_K) // 4


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--label', default='PG' + time.strftime('%H%M'))
    ap.add_argument('--cols', type=int, default=4224, help='columns per band (multiple of 32); 4224 = 178.8 mm, the most the shim arena holds in one sweep')
    ap.add_argument('--rows', type=int, default=304, help='BK nozzle rows per band (multiple of 8, <= 304: 38 eight-row groups)')
    ap.add_argument('--seg-cols', type=int, default=0, help='columns per carriage sweep (multiple of 32); 0 = whole band in one sweep. Stock allocates one buffer per sweep, so wide pages are printed as several sweeps per band')
    ap.add_argument('--bands', type=int, default=21)
    ap.add_argument('--x-start', type=int, default=35008, help='our carriage count of column 0 (sheet spans about 29000..108300)')
    ap.add_argument('--place', type=int, help='explicit stock placement Q+0x3a instead of --x-start')
    ap.add_argument('--first-ulf', type=int, default=650, help='stock LF advance after the loader before band 0 (stock used 653 for a 3.2 mm top margin)')
    ap.add_argument('--band-ulf', type=int, default=None, help='stock LF advance between bands (default 2 * rows)')
    ap.add_argument('--start-band', type=int, default=0, help='resume at this band (sheet already positioned for it)')
    ap.add_argument('--no-feed', action='store_true'); ap.add_argument('--no-eject', action='store_true'); ap.add_argument('--no-head', action='store_true')
    ap.add_argument('--verify', default='sample', choices=('full', 'sample', 'none'))
    ap.add_argument('--x-scale', type=float, default=1.0, help='horizontal compensation, e.g. 1.024 (PG2052 measured 0.977 of the designed width)')
    ap.add_argument('--no-dummy', action='store_true', help='skip the blank first pass; band 0 then prints two nozzle groups higher than the rest (PG2041)')
    ap.add_argument('--fresh-each', action='store_true', help='restore the stock RAM snapshot before every band (PG2009 behaviour: only band 0 printed, stock then believes the head is still powered)')
    ap.add_argument('--ambient', type=int, default=50)
    ap.add_argument('--log', type=Path, help='JSON record of the run')
    ap.add_argument('--serial')
    a = ap.parse_args()
    band_ulf = a.band_ulf if a.band_ulf is not None else 2 * a.rows
    seg = a.seg_cols or a.cols
    if a.cols % 32 or seg % 32 or a.cols % seg: ap.error('--cols and --seg-cols must be multiples of 32 and --seg-cols must divide --cols')
    segments = [(c0, c0 + seg) for c0 in range(0, a.cols, seg)]
    place = a.place if a.place is not None else place_for_x(a.x_start)
    if not 0 <= a.first_ulf <= 1200 or not 1 <= band_ulf <= 1200: ap.error('LF steps must be 0..1200 Ulf')
    dev = hybrid_call.device(a.serial)
    log = bytearray(); record = {'label': a.label, 'cols': a.cols, 'rows': a.rows, 'bands': a.bands, 'place': place, 'first_ulf': a.first_ulf, 'band_ulf': band_ulf, 'steps': []}

    def save():
        if a.log: a.log.parent.mkdir(parents=True, exist_ok=True); a.log.write_text(json.dumps(record, indent=1) + '\n')

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

    def run(routine, flags=0, index=None, limit=120):
        status = lambda: struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x98, 0xffff, 0, 64, timeout=5000)))
        before = status()[2]
        dev.ctrl_transfer(0x40, 0x97, routine | flags, a.ambient if index is None else index, b'', timeout=2000)
        t0 = time.monotonic()
        while status()[2] == before:
            if time.monotonic() - t0 > limit: raise TimeoutError('routine')
            time.sleep(0.1)
        st = status()
        rows = [struct.unpack('<4I', bytes(dev.ctrl_transfer(0xc0, 0x98, i, 0, 16, timeout=2000))) for i in range(min(st[4], 160))]
        moves = [(r[1], hex(r[2]), r[3]) for r in rows if r[0] == 1]
        lf = [(r[1], hex(r[2]), r[3]) for r in rows if r[0] == 4]
        waits = sum(1 for r in rows if r[0] == 9)
        line = f'routine {routine}|{flags:#x}: result {st[1]:#x} errors {st[3]} exceptions {st[11]} waits {waits} ({time.monotonic() - t0:.1f}s)'
        if moves: line += f' CR {moves}'
        if lf: line += f' LF {lf}'
        print('  ' + line)
        record['steps'].append({'routine': routine, 'flags': flags, 'index': index, 'result': st[1], 'errors': st[3], 'exceptions': st[11], 'seconds': round(time.monotonic() - t0, 1), 'cr': moves, 'lf': lf})
        save()
        assert st[1] == (0 if routine in (8, 9, 10) else 1), st
        return st

    def shim_lines():
        drain()
        text = log.decode(errors='replace'); log.clear()
        return [l for l in text.splitlines() if '[shim]' in l and ('band' in l or 'LF' in l or 'timeout' in l or 'returned' in l)]

    page = page_pattern.build(a.label, a.cols, a.rows, a.bands)
    if a.x_scale != 1.0: page.img = page_pattern.stretch_columns(page.img, a.x_scale)
    x_end = a.x_start + 16 * a.cols
    print(f'label {a.label}: {a.cols} x {page.height} dots, {a.bands} bands x {len(segments)} sweeps of {seg} columns, placement {place} (columns at our x {a.x_start}..{x_end}, {(a.x_start - PAPER_X0) / 16 / page_pattern.PX_PER_MM:.1f} mm from the sheet edge), first advance {a.first_ulf} Ulf, band pitch {band_ulf} Ulf')
    record['segments'] = segments
    t_start = time.monotonic()
    print('1. home'); home()
    if not a.no_head:
        print('2. head power and prepare'); cart_read()
    if not a.no_feed:
        print('3. stock paper load (routine 8)'); run(8, 0x100 | 0x800 | 0x1000)
        asf = hybrid_call.read_words(dev, 0x180c6f54)[0] & 0xff
        print(f'  stock ASF state byte = {asf} (1 = sheet loaded and aligned)')
        if asf != 1: sys.exit('stock loader did not load a sheet; nothing printed')
    if a.first_ulf and a.start_band == 0:
        print(f'3b. first-line advance {a.first_ulf} Ulf'); run(10, 0x800, index=a.first_ulf)
    first_sweep = True
    if not a.no_dummy and not a.fresh_each:
        # The pass right after a snapshot restore maps selection entries to nozzle
        # groups two lower than every later pass (sheet PG2041 seams). Spend that
        # pass on an all-zero raster: a sweep without ink or paper advance, so all
        # real bands share one mapping.
        print('3c. blank first pass (fresh snapshot, zero raster, no advance)')
        hybrid_call.band_params(dev, seg, a.rows, place)
        run(5, 0x400); first_sweep = False
        verify_band_feed_init(dev)
        r = hybrid_call.upload_raster(dev, bytes(seg // 16 * 1280), verify='sample'); assert r.get('mismatches', 0) == 0, r
        run(7, 0x100 | 0x1000)
    for b in range(a.start_band, a.bands):
        t_band = time.monotonic()
        print(f'band {b:02d}/{a.bands}: {len(segments)} x (prepare, upload, fire)' + ('' if b == a.bands - 1 else f', then advance {band_ulf} Ulf'))
        for c0, c1 in segments:
            raster = page_pattern.encode_band(page.img[b * a.rows:(b + 1) * a.rows, c0:c1])
            seg_place = place if a.place is not None else place_for_x(a.x_start + 16 * c0)
            hybrid_call.band_params(dev, c1 - c0, a.rows, seg_place)
            # Fresh snapshot only for the first sweep: later sweeps keep stock's own
            # band state, so the executor repeats the head power-up its pass end released.
            run(5, 0x400 if (first_sweep or a.fresh_each) else 0); first_sweep = False
            verify_band_feed_init(dev)
            m6 = hybrid_call.manager6(dev)
            assert m6['width'] == c1 - c0 and m6['size'] >= len(raster), m6
            r = hybrid_call.upload_raster(dev, raster, verify={'full': True, 'sample': 'sample', 'none': False}[a.verify])
            print(f'  columns {c0}..{c1} placement {seg_place}: upload', r); assert r.get('mismatches', 0) == 0, r
            run(7, 0x100 | 0x1000)
            for l in shim_lines(): print('   ', l)
        if b < a.bands - 1: run(10, 0x800, index=band_ulf)
        print(f'  band {b:02d} done in {time.monotonic() - t_band:.1f}s, total {time.monotonic() - t_start:.0f}s')
    if not a.no_eject:
        print('4. stock eject (routine 9)'); run(9, 0x100 | 0x800 | 0x400)
    print('5. home'); home()
    print(f'done: {a.label} in {time.monotonic() - t_start:.0f}s')


if __name__ == '__main__':
    main()
