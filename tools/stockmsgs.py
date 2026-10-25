#!/usr/bin/env python3
"""Decode stock message-ID rings from stocktrace --messages captures.

Consecutive snapshots are turned into the IDs written between them, using the
all-messages write index (region 1 byte 1) and the filtered write index
(region 4 byte 0). Both indices wrap modulo 256, so gaps larger than 255 IDs
between snapshots are reported as overflow, never silently reconstructed.
Each telemetry control request itself produces four messages
(f01f f022 f023 f021 on the pinned image); they are flagged, not removed.
"""
import argparse
import collections
import datetime
import json

OWN = (0xf01f, 0xf022, 0xf023, 0xf021)
HOUSEKEEPING = frozenset([0xf002, 0xf006, 0xf00c, 0xf00f, 0xf010, 0xf01f, 0xf020, 0xf021,
                          0xf022, 0xf023, 0xf04f, 0xf050, 0xf051, 0xf052])


def ids(hexstring):
    raw = bytes.fromhex(hexstring)
    return [int.from_bytes(raw[2*k:2*k+2], 'little') for k in range(len(raw)//2)]


def decode(rows):
    """Yield (row, new_all_ids, new_filtered_ids, overflow) per snapshot after the first."""
    prev = None
    for row in rows:
        m = row.get('messages')
        if m is None: continue
        if prev is not None:
            all_ids, fil_ids = ids(m['all_ids_hex']), ids(m['filtered_ids_hex'])
            n_all = (m['all_index_before'] - prev['all_index_before']) % 256
            n_fil = (m['filtered_index_before'] - prev['filtered_index_before']) % 256
            start = prev['all_index_before']
            new_all = [all_ids[(start + k) % 256] for k in range(n_all)]
            fstart = prev['filtered_index_before']
            new_fil = [fil_ids[(fstart + k) % 256] for k in range(n_fil)]
            requests = m.get('requests_since_previous')
            # Own traffic: at least the reads of the previous snapshot (2 + 18 or more).
            overflow = n_all == 0 and m['all_ids_hex'] != prev['all_ids_hex']
            yield row, new_all, new_fil, overflow
        prev = m


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('capture')
    p.add_argument('--hide-own', action='store_true', help='Hide the telemetry request pattern itself')
    p.add_argument('--since', type=float, default=0, help='Seconds after the first snapshot')
    args = p.parse_args()
    rows = [json.loads(l) for l in open(args.capture)]
    t0 = datetime.datetime.fromisoformat(rows[0]['utc']).timestamp()
    hist = collections.Counter(); fhist = collections.Counter()
    for row, new_all, new_fil, overflow in decode(rows):
        t = datetime.datetime.fromisoformat(row['utc']).timestamp() - t0
        hist.update(new_all); fhist.update(new_fil)
        if t < args.since: continue
        shown = new_all
        if args.hide_own:
            shown = []
            i = 0
            while i < len(new_all):
                if tuple(new_all[i:i+4]) == OWN: i += 4
                else: shown.append(new_all[i]); i += 1
        if shown or new_fil or overflow:
            print(f'{t:7.2f}s all+{len(new_all):3d} fil+{len(new_fil):3d}'
                  + (' OVERFLOW' if overflow else '')
                  + ' | ' + ' '.join(f'{x:04x}' for x in shown)
                  + (' || filtered: ' + ' '.join(f'{x:04x}' for x in new_fil) if new_fil else ''))
    print('\nall IDs histogram:', ' '.join(f'{k:04x}:{v}' for k, v in sorted(hist.items())))
    print('filtered histogram:', ' '.join(f'{k:04x}:{v}' for k, v in sorted(fhist.items())))


if __name__ == '__main__': main()
