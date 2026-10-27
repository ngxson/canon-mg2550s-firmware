#!/usr/bin/env python3
"""Call one stock routine in place on the board (hybrid shape B, USB 0x95/0x96)
and read stock RAM back through the tunnel.

    hybrid_call.py call 0xe8611            # Thumb address (bit 0 set), r0..r3 = 0
    hybrid_call.py read 0x18079aa8 [n]     # n words of stock RAM (read-only range)
    hybrid_call.py check                   # the two brief-16 initializers and their RAM effects

The tunnel key unlocks writes and calls for this session; the firmware refuses
addresses outside the stock image and anything while the mechanism is busy.
"""
import argparse, struct, sys
import usb.core

KEY = 0x7d4e


def device(serial=None):
    for dev in usb.core.find(find_all=True, idVendor=0x04a9):
        if serial is None or dev.serial_number == serial:
            return dev
    sys.exit('printer not found')


def unlock(dev): dev.ctrl_transfer(0x40, 0x93, KEY, 0, b'', timeout=1000)
def lock(dev): dev.ctrl_transfer(0x40, 0x93, 0, 0, b'', timeout=1000)


def read_words(dev, address, n=1):
    dev.ctrl_transfer(0x40, 0x90, address & 0xffff, address >> 16, b'', timeout=1000)
    data = bytes(dev.ctrl_transfer(0xc0, 0x91, 0, 0, 4 * n, timeout=1000))
    return list(struct.unpack('<%dI' % n, data))


def call(dev, address):
    dev.ctrl_transfer(0x40, 0x95, address & 0xffff, address >> 16, b'', timeout=5000)
    r = struct.unpack('<6I', bytes(dev.ctrl_transfer(0xc0, 0x96, 0, 0, 24, timeout=1000)))
    assert r[0] == 0x42594848, r
    return {'address': hex(r[1]), 'r0': hex(r[2]), 'calls': r[3], 'refused': r[4], 'unlocked': r[5]}


def band_params(dev, cols, rows, place, mode=1, colour=False):
    """USB 0x9a/0x9b: columns (multiple of 32), nozzle rows (multiple of 8, <= 304), stock placement Q+0x3a
    (int16), stock print mode (1 black 600 dpi, 7 black + C/M/Y 600 dpi) and whether the C/M/Y groups are selected."""
    dev.ctrl_transfer(0x40, 0x9a, cols, rows | (0x800 if colour else 0) | (mode << 12), b'', timeout=1000)
    dev.ctrl_transfer(0x40, 0x9b, place & 0xffff, 0, b'', timeout=1000)


def raster_mode(dev, mode):
    """0x9b wIndex=3: raster storage start, 0x100 = 64-byte aligned (default), 0..63 = explicit byte shift (alignment tests)."""
    global RASTER_MODE
    RASTER_MODE = mode & 0x1ff
    dev.ctrl_transfer(0x40, 0x9b, mode & 0x1ff, 3, b'', timeout=1000)


def restore_mask(dev, mask):
    """0x9b wIndex=4: snapshot RAM ranges re-copied before each non-fresh prepare (bit 0 lane records 0x180cd000.., 1 builder block, 2 0x18070000.., 3 0x1802e000.., 4 band-task/LF/CR state, 5 head work lists); diagnostic."""
    dev.ctrl_transfer(0x40, 0x9b, mask & 0xff, 4, b'', timeout=1000)


def finish_mask(dev, mask):
    """0x9b wIndex=2: which steps of the stock post-pass tail band_finish runs (bit 0 e281a, 1 e7052, 2 flag clears, 3 e2a3e, 4 e2c40, 5 e753c, 6 descriptor hand-back); diagnostic."""
    dev.ctrl_transfer(0x40, 0x9b, mask & 0xff, 2, b'', timeout=1000)


RASTER_MODE = 0x100   # mirrors the firmware's raster_mode: 0x100 = 64-byte aligned raster start, else an explicit shift 0..63


def manager(dev, group=6):
    """Stock print-buffer manager header (group 0..6; 6 = black, 0/2/4 = C/M/Y): slots, width, stride, size, base, raster."""
    w = read_words(dev, 0x1879b09c + 0x1c * group, 7)
    slots = w[0] & 0xffff
    raw = w[6] + 24 * slots if w[6] else 0
    raster = ((raw + 63) & ~63 if RASTER_MODE & 0x100 else raw + (RASTER_MODE & 63)) if raw else 0
    return {'slots': slots, 'width': w[1] & 0xffff, 'stride': w[1] >> 16, 'size': w[2], 'base': w[6], 'raster': raster, 'raster_unaligned': raw}


def manager6(dev): return manager(dev, 6)


def raster_base(dev):
    st = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x98, 0xffff, 0, 64, timeout=5000)))
    assert st[0] == 0x4e555248, st
    base = st[13]
    if not base: sys.exit('no band prepared (run band-prepare first)')
    return (base & 0x07ffffff) | 0x18000000


def upload_raster(dev, data, verify=True, group=6):
    """0x99 then bulk OUT (EP1) into print-buffer manager `group` (6 = black, 0/2/4 = C/M/Y);
    returns a dict with the readback comparison.

    verify: True = every byte read back through the tunnel, 'sample' = the first
    and last KiB plus 16 spread 256-byte chunks, False = none.
    """
    import time
    m = manager(dev, group)
    limit = min(m['width'] // 16 * m['stride'], m['size']) if m['slots'] else 0
    assert 0 < len(data) <= limit, (len(data), group, m)
    raster = (m['raster'] & 0x07ffffff) | 0x18000000
    dev.ctrl_transfer(0x40, 0x99, len(data) & 0xffff, (len(data) >> 16) | ((group + 1) << 8), b'', timeout=2000)
    t0 = time.monotonic()
    for i in range(0, len(data), 512):
        dev.write(0x01, data[i:i + 512], timeout=2000)
    sent = time.monotonic() - t0
    if not verify: return {'bytes': len(data), 'seconds': round(sent, 2)}
    chunks = list(range(0, len(data), 256))
    if verify == 'sample' and len(chunks) > 24:
        chunks = chunks[:4] + chunks[-4:] + [chunks[len(chunks) * k // 16] for k in range(16)]
    bad = []; checked = 0
    for off in chunks:
        n = min(256, len(data) - off)
        back = struct.pack('<%dI' % (n // 4), *read_words(dev, raster + off, n // 4))
        bad += [off + i for i in range(n) if back[i] != data[off + i]]; checked += n
    return {'bytes': len(data), 'seconds': round(sent, 2), 'raster': hex(raster), 'checked': checked, 'mismatches': len(bad), 'first_bad': bad[:8]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--serial')
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('call'); c.add_argument('address', type=lambda v: int(v, 0))
    r = sub.add_parser('read'); r.add_argument('address', type=lambda v: int(v, 0)); r.add_argument('n', type=int, nargs='?', default=1)
    sub.add_parser('check')
    ras = sub.add_parser('raster', help='upload a tile-layout band raster file after band-prepare and verify it by readback')
    ras.add_argument('file')
    bp = sub.add_parser('band', help='set the band geometry for the next band-prepare/band-run (USB 0x9a/0x9b)')
    bp.add_argument('--cols', type=int, default=320); bp.add_argument('--rows', type=int, default=8); bp.add_argument('--place', type=int, default=5000)
    run = sub.add_parser('run', help='run a stock maintenance routine through the shim (0x97) and print its trace')
    run.add_argument('routine', choices=('cap', 'uncap', 'wipe', 'clean', 'band-prepare', 'band-run', 'band-fire', 'feed', 'eject', 'advance'))
    run.add_argument('--distance', type=int, help='advance only: 1..1200 Ulf, forward through the stock LF controller; --lf-live moves paper')
    run.add_argument('--type', type=int, default=1, help='cleaning type 1..10')
    run.add_argument('--live', action='store_true', help='drive the carriage for CR requests (needs a homed carriage)')
    run.add_argument('--motion-only', action='store_true', help='with --live: sweep but disarm firing (no paper needed)')
    run.add_argument('--lf-live', action='store_true', help='run stock LF requests through the stock LF task and servo (moves the feed motor)')
    run.add_argument('--fresh', action='store_true', help='stock RAM back to the boot snapshot first')
    run.add_argument('--cr-stock', action='store_true', help='with --live: CR requests run stock\'s CR task and servo (moves the carriage)')
    run.add_argument('--ambient', type=int, default=50, help='cached head temperature, half-degrees C')
    run.add_argument('--stage', type=int, default=0, help='diagnostic stop: 1 after RAM init, 2 after SVC registration, 3 after presets')
    a = ap.parse_args()
    if a.cmd == 'run':
        if a.routine == 'advance' and (a.distance is None or not 1 <= a.distance <= 1200 or a.stage):
            ap.error('advance requires --distance 1..1200 and no --stage')
        if a.routine != 'advance' and a.distance is not None:
            ap.error('--distance is only for advance')
    dev = device(a.serial)
    if a.cmd == 'read':
        for i, w in enumerate(read_words(dev, a.address, a.n)):
            print(f'{a.address + 4*i:#010x}: {w:#010x}')
        return
    if a.cmd == 'band':
        band_params(dev, a.cols, a.rows, a.place); print('band geometry set:', a.cols, 'columns,', a.rows, 'rows, placement', a.place)
        return
    if a.cmd == 'raster':
        data = open(a.file, 'rb').read()
        print(upload_raster(dev, data))
        return
    if a.cmd == 'run':
        import time
        KIND = {1: 'CR_MOVE', 2: 'CR_PROFILE', 3: 'CR_RELEASE', 4: 'LF_MOVE', 5: 'STATUS_GET', 6: 'STATUS_PUB', 7: 'NVM', 8: 'NOTIFY', 9: 'WAIT_TIMEOUT', 10: 'DELAY', 11: 'ROUTINE', 12: 'UNKNOWN'}
        routine = {'cap': 1, 'uncap': 2, 'wipe': 3, 'clean': 4, 'band-prepare': 5, 'band-run': 6, 'band-fire': 7, 'feed': 8, 'eject': 9, 'advance': 10}[a.routine] | (a.type << 4) | (0x100 if a.live else 0) | (0x200 if a.motion_only else 0) | (0x800 if a.lf_live else 0) | (0x400 if a.fresh else 0) | (0x1000 if a.cr_stock else 0)
        status = lambda: struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x98, 0xffff, 0, 64, timeout=5000)))
        before = status()[2]
        index = a.distance if a.routine == 'advance' else a.ambient | (a.stage << 8)
        dev.ctrl_transfer(0x40, 0x97, routine, index, b'', timeout=2000)
        t0 = time.monotonic()
        while True:
            try:
                if status()[2] != before: break
            except usb.core.USBError as exc:
                print(f'USB stopped answering after {time.monotonic()-t0:.1f}s: {exc}'); sys.exit(2)
            if time.monotonic() - t0 > 600: sys.exit('routine did not finish in 600 s')
            time.sleep(0.2)
        st = status()
        if a.live:
            cr = struct.unpack('<12I', bytes(dev.ctrl_transfer(0xc0, 0x6e, 0, 0, 48, timeout=2000)))
            print(f'carriage: x={cr[4]} reference={cr[5]:#x} raw={cr[11]:#x} result={cr[3]}')
        print(f'result={st[1]:#x} errors={st[3]} trace={st[4]} flags={st[5]:#x} cr_position={st[6]} exceptions={st[11]} kind={st[8]} lr={st[9]:#x} spsr={st[10]:#x} ({time.monotonic()-t0:.1f}s)')
        for i in range(min(st[4], 160)):
            k, x, y, z = struct.unpack('<4I', bytes(dev.ctrl_transfer(0xc0, 0x98, i, 0, 16, timeout=2000)))
            print(f'  {KIND.get(k, k):12} {x:#x} {y:#x} {z:#x}')
        if a.routine in ('band-prepare', 'band-run', 'band-fire'):
            q, base, rc0, rc1 = st[12], st[13], st[14], st[15]
            print(f'Q={q:#x} manager6 base={base:#x} adc14={rc0} a8422={rc1}')
            if base:
                alias = (base & 0x07ffffff) | 0x18000000
                raster = alias + 0x780
                data = b''.join(struct.pack('<%dI' % 64, *read_words(dev, raster + 256 * i, 64)) for i in range(16))
                nz = [(i, b) for i, b in enumerate(data) if b]
                print(f'raster storage at {raster:#x}: {len(nz)} nonzero bytes in the first 4 KiB')
                for i, b in nz[:48]: print(f'   +{i:#06x} = {b:#04x}')
                if q:
                    qa = (q & 0x07ffffff) | 0x18000000
                    words = read_words(dev, qa, 64)
                    print('Q first 64 words:', ' '.join(f'{wd:#x}' for wd in words))
        return
    unlock(dev)
    try:
        if a.cmd == 'call':
            print(call(dev, a.address))
        else:
            before = read_words(dev, 0x18079aa8), read_words(dev, 0x18079330), read_words(dev, 0x18079a98, 4)
            print('before: repeat', [hex(v) for v in before[0]], 'gates', [hex(v) for v in before[1]], 'thresholds', before[2])
            print('0xe8610:', call(dev, 0xe8611))
            print('0xc763e:', call(dev, 0xc763f))
            repeat, gates, thr = read_words(dev, 0x18079aa8), read_words(dev, 0x18079330), read_words(dev, 0x18079a98, 4)
            print('after:  repeat', [hex(v) for v in repeat], 'gates', [hex(v) for v in gates], 'thresholds', thr)
            ok = repeat[0] == 1 and gates[0] & 0xffffff == 0x010101 and thr == [20160, 43200, 64800, 129600]
            print('PASS' if ok else 'FAIL', 'stock initializers ran in place from flash')
    finally:
        lock(dev)


if __name__ == '__main__':
    main()
