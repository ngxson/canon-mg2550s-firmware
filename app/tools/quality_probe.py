#!/usr/bin/env python3
"""One labelled sheet to separate sweep width, density and nozzle-row defects.

Uses the installed hybrid's normal band executor; never changes heater words.
Every raster byte is read back before firing. ASIC dot counts are captured
after each sweep; they establish data activity, not ink deposition. The same
marks stay at the same carriage position for every width.
Run without --print to inspect the patterns and their expected dot totals.
"""
import argparse
import json
import struct
import time
from pathlib import Path

import numpy as np
import hybrid_call
import page_pattern
from band_live import collect_head_counts, verify_band_feed_init
from page_live import place_for_x

VARIANTS = [('C1024A', 1024, True), ('C1024B', 1024, True),
            ('K1024', 1024, False), ('K2112', 2112, False),
            ('K4224', 4224, False), ('C2112', 2112, True)]
GROUPS = {'K': 6, 'C': 0, 'M': 2, 'Y': 4}
ROWS = 304


def alternate_rows(img, phase):
    """Use physical rows 0..15 in each 32-row group; second pass feeds 16 rows.

    The output at paper row r is assigned to exactly one pass, never printed
    twice. This is a measured-nozzle workaround, not a heater timing change.
    """
    out = np.zeros_like(img)
    for r in range(img.shape[0] - phase):
        if r % 32 < 16:
            out[r] = img[r + phase]
    return out


def planes(label, name, cols, colour):
    result = {}
    for ink in ('KCMY' if colour else 'K'):
        width = cols if ink == 'K' else 2 * cols
        page = page_pattern.Page(width, ROWS, 1)
        height = ROWS if ink == 'K' else 160
        x = {'K': 800, 'C': 280, 'M': 450, 'Y': 620}[ink]
        page.img[:height, x:x + 32] = True       # solid, all rows
        page.img[:height, x + 40:x + 42] = True # same rows, two-column line
        page.img[:height, x + 50:x + 82:2] = True  # half horizontal density
        # Local row staircase: repeated at each 32-row boundary, two columns
        # per row. Reveals a missing/displaced row beside the solid bar.
        for r in range(height):
            page.img[r, x + 90 + 2 * (r % 32):x + 92 + 2 * (r % 32)] = True
        if ink == 'K':
            page.text(24, 8, label, 4)
            page.text(24, 48, name, 4)
            for r in range(0, ROWS, 32):
                page.hline(r, 720, 760)
                page.text(650, r + 4, str(r), 2)
        result[ink] = page.img
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--label', default='Q' + time.strftime('%d%H%M', time.gmtime()))
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--print', dest='do_print', action='store_true')
    ap.add_argument('--alternate-nozzles', action='store_true',
                    help='single-pass control then the same marks using two disjoint row passes, 16 rows apart')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    record = {'label': a.label, 'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'x_start': 35008, 'first_ulf': 650, 'pitch_ulf': 640, 'passes': [], 'steps': []}
    variants = ([('SINGLE', 2112, True), ('ALT16', 2112, True), ('ALT16', 2112, True)]
                if a.alternate_nozzles else VARIANTS)
    phases = [None, 0, 16] if a.alternate_nozzles else [None] * len(variants)
    advances = [0, 640, 32] if a.alternate_nozzles else [0] + [640] * (len(variants) - 1)
    record['alternate_nozzles'] = a.alternate_nozzles
    patterns = []
    for (name, cols, colour), phase in zip(variants, phases):
        imgs = planes(a.label, name, cols, colour)
        if phase is not None:
            for ink, original in imgs.items():
                first, second = alternate_rows(original, 0), alternate_rows(original, 16)
                landed = first.astype(np.uint8)
                landed[16:] += second[:-16]
                assert np.array_equal(landed, original.astype(np.uint8)), 'missing or duplicate paper rows'
            imgs = {ink: alternate_rows(img, phase) for ink, img in imgs.items()}
        for ink, img in imgs.items():
            slots = (81 if ink == 'K' else 61) if colour else 80
            data = page_pattern.encode_band(img, slots)
            assert np.array_equal(page_pattern.decode_band(data, ROWS, img.shape[1], slots), img)
        dots = {ink: int(img.sum()) for ink, img in imgs.items()}
        print(name, cols, 'phase', phase, dots, flush=True)
        patterns.append(imgs)
    if not a.do_print:
        return

    dev = hybrid_call.device()
    log = bytearray()

    def save():
        (a.out / f'{a.label}.json').write_text(json.dumps(record, indent=2) + '\n')
        (a.out / f'{a.label}.log').write_bytes(log)

    def drain():
        for _ in range(32):
            data = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
            if not data:
                break
            log.extend(data)

    def status():
        return struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x98, 0xffff, 0, 64, timeout=5000)))

    def run(routine, flags=0, index=50):
        before = status()[2]
        start = time.monotonic()
        dev.ctrl_transfer(0x40, 0x97, routine | flags, index, b'', timeout=2000)
        while True:
            s = status()
            if s[2] != before:
                break
            if time.monotonic() - start > 120:
                raise TimeoutError(f'routine {routine}')
            drain()
            time.sleep(0.05)
        drain()
        record['steps'].append({'routine': routine, 'flags': flags, 'index': index,
                                'status': list(s), 'seconds': round(time.monotonic() - start, 2)})
        save()
        assert s[1] == (0 if routine in (8, 9, 10) else 1) and s[11] == 0, s
        print('routine', routine, 'result', s[1], 'errors', s[3], flush=True)
        return s

    def home():
        dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000)
        start = time.monotonic()
        while True:
            s = struct.unpack('<12I', bytes(dev.ctrl_transfer(0xc0, 0x6e, 0, 0, 48, timeout=1000)))
            if s[2] == 0 and s[3] != 1:
                assert s[3] == 2, s
                return
            if time.monotonic() - start > 30:
                raise TimeoutError('home')
            drain()
            time.sleep(0.05)

    try:
        home()
        before = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))[5]
        dev.ctrl_transfer(0x40, 0x62, 3 | 0x200, 0, b'', timeout=1000)
        start = time.monotonic()
        while True:
            s = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
            if s[2] == 2 and s[5] != before:
                assert s[7] & 0x10000, s
                break
            if time.monotonic() - start > 30:
                raise TimeoutError('head preparation')
            drain()
            time.sleep(0.05)
        run(8, 0x100 | 0x800 | 0x1000)
        assert hybrid_call.read_words(dev, 0x180c6f54)[0] & 0xff == 1, 'sheet not loaded'
        run(10, 0x800, 650)
        hybrid_call.finish_mask(dev, 0xff)
        hybrid_call.raster_mode(dev, 0x100)
        hybrid_call.restore_mask(dev, 0)
        for i, ((name, cols, colour), imgs, phase, advance) in enumerate(zip(variants, patterns, phases, advances)):
            if advance:
                run(10, 0x800, advance)
            hybrid_call.band_params(dev, cols, ROWS, place_for_x(35008), mode=7 if colour else 1, colour=colour)
            run(5, 0x400 if i == 0 else 0)
            verify_band_feed_init(dev)
            rec = {'name': name, 'cols': cols, 'colour': colour, 'phase': phase, 'advance_ulf': advance, 'uploads': {},
                   'expected_plane_dots': {ink: int(img.sum()) for ink, img in imgs.items()}}
            record['passes'].append(rec)
            for ink, img in imgs.items():
                group = GROUPS[ink]
                manager = hybrid_call.manager(dev, group)
                upload = hybrid_call.upload_raster(dev, page_pattern.encode_band(img, manager['slots']), verify=True, group=group)
                rec['uploads'][ink] = {'manager': manager, **upload}
                assert upload['mismatches'] == 0, upload
            rec['counters_before_cleared'] = collect_head_counts(dev)
            run(7, 0x100 | 0x1000)
            rec['counters'] = collect_head_counts(dev)
            print(name, 'head counts', rec['counters'], flush=True)
            save()
        run(9, 0x100 | 0x800 | 0x400)
        home()
        record['completed'] = True
    finally:
        drain()
        save()
    print('Printed and ejected', a.label, flush=True)


if __name__ == '__main__':
    main()
