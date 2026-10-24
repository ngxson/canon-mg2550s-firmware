#!/usr/bin/env python3
"""Read the cartridge fuse ROM (IDs, ranks, level field) through the open app.

Sends USB vendor request 0x62 (read-only fuse sampling, see src/cartridge.c),
waits for completion through 0x63, drains the firmware log and decodes the
fields the stock firmware labels PK*ID, PK*DEST, PK*RANK*, PK*INKLVL. INKLVL
is a seven-bit fuse field, not an analog ink measurement.
"""
import argparse
import json
import struct
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device, get_info  # noqa: E402

HEADS = {'black': 1, 'colour': 2, 'both': 3}


def decode(head, low, high):
    return {'id': (low >> 7) & 15,
            'dest': (low >> 25) & 63 if head else (low >> 22) & 63,
            'rank1': (low >> 11) & 31 if head else (low >> 11) & 63,
            'rank2': (low >> 16) & 15 if head else None,
            'inklvl': low & 127,
            'inklvl_bits': format(low & 127, '07b'),
            'level_index': bin(low & 127).count('1') - 1,   # stock programs 1,3,7,..,127 for index 0..6; -1 = none
            'level_note': 'thermometer code: 7 of 7 fuses cut is the last stock level step' if (low & 127) == 127 else '',
            'high': high}


def interpret(low, repeat, high, sense_idle):
    notes = []
    if low != repeat:
        notes.append('UNSTABLE: repeat read differs')
    if low == 0 and high == 0:
        notes.append('all samples high (complemented zero): sense line stuck or head unpowered')
    if low == 0xffffffff:
        notes.append('all samples low (complemented ones): sense line stuck low')
    notes.append(f'idle sense bit0={sense_idle & 1}')
    return notes


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--heads', choices=sorted(HEADS), default='both')
    p.add_argument('--wait-us', type=int, default=0,
                   help='per-sample wait in microseconds, 10..2000 (0 = firmware default 300, stock nominal)')
    p.add_argument('--raw', action='store_true',
                   help='skip the stock head-interface power sequence (reproduces the stuck-low first attempt)')
    p.add_argument('--diag', action='store_true',
                   help='log head-block read-backs, bank/alias words and a sense-line pulse capture')
    p.add_argument('--no-headinit', action='store_true',
                   help='skip the stock head-block configuration (experiment: reproduces the no-response state on a cold board)')
    p.add_argument('--keep-powered', action='store_true',
                   help='leave the head interface in the stock ready state after the read')
    p.add_argument('--output', type=Path, help='write JSON report here')
    args = p.parse_args()
    if args.wait_us % 10 or args.wait_us > 2000:
        p.error('--wait-us must be a multiple of 10 and at most 2000')
    import usb.util
    dev, info = wait_device('application')
    log = bytearray()
    try:
        def drain():
            for _ in range(16):
                data = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
                if not data:
                    break
                log.extend(data)

        def status():
            raw = bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000))
            words = struct.unpack('<16I', raw)
            assert words[0] == 0x5443474d and words[1] in (1, 2), [hex(w) for w in words]
            return words

        drain()
        before = status()
        value = HEADS[args.heads] | (0x100 if args.raw else 0) | (0x200 if args.keep_powered else 0) | (0x400 if args.diag else 0) | (0x800 if args.no_headinit else 0)
        dev.ctrl_transfer(0x40, 0x62, value, args.wait_us // 10, b'', timeout=1000)
        deadline = time.monotonic() + 15
        while True:
            s = status()
            if s[2] == 2 and s[5] != before[5]:
                break
            if time.monotonic() > deadline:
                raise TimeoutError('firmware did not finish the fuse read')
            drain()                                   # keep the ring from overflowing with IPP chatter
            time.sleep(0.05)
        drain()
        conn = {0: 'untested', 1: 'ok', 2: 'FAILED'}
        result = {'management': get_info(dev), 'heads_requested': args.heads, 'raw': args.raw,
                  'wait_ticks': s[4], 'sequence': s[5],
                  'connection': {'black': conn.get(s[6] & 0xff, s[6] & 0xff), 'colour': conn.get(s[6] >> 8, s[6] >> 8)},
                  'options': f'{s[7]:#x}', 'left_prepared': bool(s[7] & 0x10000),
                  'head_block_initialized': bool(s[7] & 0x20000), 'heads': {}}
        for head, name in ((0, 'black'), (1, 'colour')):
            if not s[3] & (1 << head):
                continue
            low, repeat, high, sense = s[8 + 4 * head: 12 + 4 * head]
            result['heads'][name] = {'raw_low': f'{low:08x}', 'raw_low_repeat': f'{repeat:08x}',
                                     'raw_high': f'{high:04x}', 'sense_idle': sense & 1,
                                     'fields': decode(head, low, high),
                                     'notes': interpret(low, repeat, high, sense)}
        result['firmware_log'] = log.decode(errors='replace')
        print('\n'.join(l for l in result['firmware_log'].splitlines() if l.startswith('[cart]')))
        print(f"connection: black={result['connection']['black']} colour={result['connection']['colour']}  "
              f"left_prepared={result['left_prepared']}")
        for name, h in result['heads'].items():
            f = h['fields']
            print(f"{name:7s} low={h['raw_low']} repeat={h['raw_low_repeat']} high={h['raw_high']} "
                  f"id={f['id']} dest={f['dest']} rank1={f['rank1']} rank2={f['rank2']} "
                  f"inklvl={f['inklvl']} ({f['inklvl_bits']}, level index {f['level_index']}/6)  {'; '.join(h['notes'])}")
        if args.output:
            args.output.write_text(json.dumps(result, indent=2) + '\n')
    finally:
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
