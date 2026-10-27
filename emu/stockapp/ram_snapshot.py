#!/usr/bin/env python3
"""Boot-state snapshot of the stock RAM pages the borrowed routines touch.

Stock's C++ object graph (kernel objects, pools, mailboxes, print-buffer
managers, NVM descriptors, calibration) is built by its root task's component
initialisation, which also brings up USB, scanner and panel. Instead of
borrowing that, the hybrid restores the state of the pages the fixtures
touched, taken from the public emulator's stock boot (readiness 4, the
reference EEPROM). Pages that are entirely zero are listed but not stored.

Output: a JSON index and a raw blob the hybrid package builder appends to the
app image; the shim copies the pages back at init before its own presets.
This is an explicitly tracked intermediate stock dependency.
"""
import argparse, hashlib, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'emu')]
from stock.boot import BootTrace  # noqa: E402

ALWAYS = [(0x1811b000, 0x1811d000)]   # kernel state and scheduler tables


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--eeprom', type=Path, default=ROOT / 'emu/inputs/eeprom.bin')
    ap.add_argument('--reports', nargs='+', type=Path, default=[ROOT / 'emu/output/fixture/maintenance.json', ROOT / 'emu/output/fixture/band_prepared.json'])
    ap.add_argument('--exclude', nargs='*', default=['0x100f0000-0x10100000'], help='fixture scratch/stack ranges to drop')
    ap.add_argument('--output', type=Path, default=ROOT / 'emu/output/fixture/stock_ram_snapshot')
    opts = ap.parse_args()

    pages = set()
    for r in opts.reports:
        for a, b in json.loads(r.read_text())['ram_pages_touched_4k']:
            lo, hi = int(a, 16), int(b, 16)
            if 0x10100000 <= lo < 0x10800000:          # low DRAM window aliases 0x18100000..
                lo += 0x08000000; hi += 0x08000000
            pages.update(range(lo >> 12, hi >> 12))
    for a, b in ALWAYS:
        pages.update(range(a >> 12, b >> 12))
    for ex in opts.exclude:
        a, b = (int(v, 16) for v in ex.split('-'))
        pages -= set(range(a >> 12, b >> 12))
    # SRAM 0xe0000000 is initialised by the shim itself (scatter copy/zero) and
    # holds the bootloader's vector state: never part of the snapshot.
    pages = sorted(p for p in pages if 0x18000000 <= p << 12 < 0x18800000)

    fx = BootTrace(ready_level=4, eeprom=opts.eeprom)
    fx.boot(); fx.run_boot(ticks=300, budget=12_000_000)
    # Store 256-byte blocks that are not all zero, merged into runs; the shim
    # zero-fills the rest itself.
    blob = bytearray(); index = []; zero_pages = 0
    for p in pages:
        data = bytes(fx.uc.mem_read(p << 12, 4096))
        if not any(data):
            index.append({'address': hex(p << 12), 'zero': True}); zero_pages += 1; continue
        run = None
        for k in range(0, 4096, 256):
            chunk = data[k:k + 256]
            if any(chunk):
                if run and run['address_int'] + run['length'] == (p << 12) + k:
                    run['length'] += 256; blob += chunk
                else:
                    run = {'address_int': (p << 12) + k, 'address': hex((p << 12) + k), 'offset': len(blob), 'length': 256}
                    index.append(run); blob += chunk
    for e in index: e.pop('address_int', None)
    opts.output.parent.mkdir(parents=True, exist_ok=True)
    opts.output.with_suffix('.bin').write_bytes(bytes(blob))
    opts.output.with_suffix('.json').write_text(json.dumps({
        'boot': {'instructions': fx.instructions, 'ready_level': 4, 'eeprom_sha256': hashlib.sha256(fx.eeprom_original).hexdigest(),
                 'firmware_sha256': hashlib.sha256(fx.image).hexdigest()},
        'pages': index, 'blob_bytes': len(blob), 'blob_sha256': hashlib.sha256(bytes(blob)).hexdigest()}, indent=1))
    stored = sum(1 for e in index if not e.get('zero'))
    print(f'{len(pages)} pages selected, {zero_pages} zero, {stored} nonzero runs ({len(blob)} bytes); wrote {opts.output}.bin/.json')


if __name__ == '__main__':
    main()
