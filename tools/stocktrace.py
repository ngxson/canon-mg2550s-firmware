#!/usr/bin/env python3
"""Read bounded software-state snapshots from the experimental stock telemetry app.

No configuration changes, interface detach, reset, write request or MMIO reads.
The printer must run an app built with --telemetry. This does not install it.
"""
import argparse
import datetime
import json
import struct
import time

MAGIC = 0x314c5453
ABI = 3
REGIONS = ((0x1804099c, 4), (0x1803fcc4, 8),
           (0x1803fccc, 1024), (0x180400cc, 1024),
           (0x180404cc, 2), (0x180404ce, 512), (0x180406ce, 512),
           (0x1803a790, 0x128), (0x1806c974, 28), (0x180c6f54, 1),
           (0x180d5790, 4), (0x1879b98c, 5))
# DJS formatting per RE brief 12: category nibble 7 selects the print-job
# substates; anything else is NO except substate 20; CC override wins.
DJS_CATEGORY7 = {4: 'WP', 25: 'WP', 5: 'LD', 6: 'PR', 7: 'EJ', 0: 'ID', 1: 'ID', 2: 'ID', 3: 'ID', 11: 'ID'}


def decode_engine(status, geometry, asf, lf, edge):
    """Decode the regions 7-11 bytes; raw hex is kept beside every field."""
    category = status[0x0f] & 0x0f
    substate = (int.from_bytes(status[0x10:0x12], 'little') >> 4) & 0x1f
    cc = status[0x124]
    if cc == 1: djs = 'CC'
    elif category == 7: djs = DJS_CATEGORY7.get(substate, f'?{substate}')
    else: djs = 'S1' if substate == 20 else 'NO'
    # Brief 12 names 24 bytes from 0x1806c974; the region's last 4 bytes are kept raw.
    g = struct.unpack('<hhHHHHhhHHHH', geometry[:24])
    names = ('active_x', 'active_y', 'active_w', 'active_h', 'paper_w', 'paper_h',
             'req_x', 'req_y', 'req_w', 'req_h', 'req_paper_w', 'req_paper_h')
    lf_position = int.from_bytes(lf, 'little', signed=True)
    edge_coordinate = int.from_bytes(edge[:4], 'little', signed=True)
    # RE brief 13: LF word and edge coordinate are signed 'Ulf' units
    # (1 Ulf = 6 controller counts; 1/1200 inch is inferred, not calibrated);
    # edge state 1 = not latched, 2 = trailing edge captured; eject finishes
    # at saved_edge + 3596 Ulf. PID byte 0: 0 idle, 2 preparing/loading,
    # 1 printing, 3 finishing (inferred); bytes 1-2 are copied page values.
    return {'djs': djs, 'category': category, 'substate': substate, 'cc_override': cc,
            'pid': list(status[0x13:0x16]), 'session_available': status[0x127], 'status_raw_hex': status.hex(),
            'geometry_600dpi': dict(zip(names, g)), 'geometry_tail_hex': geometry[24:].hex(), 'asf_state': asf[0],
            'lf_position_ulf': lf_position, 'edge_coordinate_ulf': edge_coordinate, 'edge_state': edge[4],
            'eject_remaining_ulf': (3596 - (lf_position - edge_coordinate)) if edge[4] == 2 else None,
            'limit': 'Reported-status cache and feed accounting, not raw sensors; fields per RE briefs 12/13; '
                     'geometry in 1/600 inch, LF/edge in Ulf (6 controller counts, 1/1200 inch inferred).'}
# Stock's message recorder excludes these IDs from the filtered ring.
HOUSEKEEPING = frozenset([0xf002, 0xf006, 0xf00c, 0xf00f, 0xf010,
                          0xf01f, 0xf020, 0xf021, 0xf022, 0xf023,
                          0xf04f, 0xf050, 0xf051, 0xf052])


class Reader:
    """Transport accepts (request, value, index, length), returning bytes.

    An ESP32 bridge can implement the same contract through its serialized
    ctrl_request(). No arbitrary USB command forwarding is needed.
    """
    def __init__(self, transfer):
        self.transfer = transfer
        expected = struct.pack(f'<{4+2*len(REGIONS)}I', MAGIC, ABI, 64, len(REGIONS),
                               *(v for r in REGIONS for v in r))
        raw = bytearray(transfer(0xd0, 0, 0, 64))
        if len(raw) == 64 and len(expected) > 64:
            raw += bytes(transfer(0xd0, 64, 0, 64))
        if bytes(raw) != expected:
            raise ValueError('Unrecognized telemetry ABI/region map; refusing reads')

    def read(self, region, offset=0, length=None):
        if not 0 <= region < len(REGIONS):
            raise ValueError('Unknown region')
        size = REGIONS[region][1]
        length = size-offset if length is None else length
        if offset < 0 or length <= 0 or offset >= size or length > size-offset:
            raise ValueError('Read exceeds the advertised region')
        data = bytearray()
        while len(data) < length:
            n = min(64, length-len(data))
            part = bytes(self.transfer(0xd1, offset+len(data), region, n))
            if len(part) != n:
                raise ValueError(f'Short telemetry read: {len(part)} != {n}')
            data.extend(part)
        return bytes(data)

    def snapshot(self, history=False, messages=False, engine=False):
        start = time.monotonic_ns()
        state = self.read(0)
        errors = self.read(1)
        result = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'source': 'physical USB software-state snapshot',
                  'usb_state': dict(zip(('state','mode','initialized','attached'), state)),
                  'error_and_indices_hex': errors.hex(), 'atomic': False}
        if history:
            low, high = self.read(2), self.read(3)
            after = self.read(1)
            result['setup_history'] = {
                'first_words_hex': low.hex(), 'last_words_hex': high.hex(),
                'indices_before': [errors[3], errors[4]],
                'indices_after': [after[3], after[4]],
                'limit': 'Split rings are not atomic; these telemetry requests also enter stock history. '
                         'Indices wrap modulo 256; unchanged indices do not prove consistency.'}
        if messages:
            filtered_index = self.read(4)
            all_ids, filtered_ids = self.read(5), self.read(6)
            after = self.read(1)
            result['messages'] = {
                'all_index_before': errors[1], 'all_processed_before': errors[2],
                'all_index_after': after[1], 'all_processed_after': after[2],
                'filtered_index_before': filtered_index[0], 'filtered_processed_before': filtered_index[1],
                'all_ids_hex': all_ids.hex(), 'filtered_ids_hex': filtered_ids.hex(),
                'limit': 'Rings of 256 u16 IDs written by stock at 0x50a76-0x50bca; not atomic. '
                         'Each telemetry control request itself produces four messages.'}
        if engine:
            result['engine'] = decode_engine(self.read(7), self.read(8), self.read(9),
                                             self.read(10), self.read(11))
        result['host_elapsed_ns'] = time.monotonic_ns()-start
        return result


def connect():
    import usb.core
    import usb.util
    devices = list(usb.core.find(find_all=True, idVendor=0x04a9, idProduct=0x176d))
    if len(devices) != 1:
        raise RuntimeError(f'Expected one directly attached MG2500, found {len(devices)}')
    dev = devices[0]
    if usb.util.get_string(dev, dev.iProduct) != 'MG2500 series':
        raise RuntimeError('Device is not the running stock runtime')
    try:
        reader = Reader(lambda request, value, index, length:
            dev.ctrl_transfer(0xc0, request, value, index, length, timeout=2000))
    except BaseException:
        usb.util.dispose_resources(dev)
        raise
    return dev, reader


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--count', type=int, default=1)
    p.add_argument('--interval', type=float, default=1.0)
    p.add_argument('--setup-history', action='store_true', help='Include the non-atomic SETUP rings (32 extra reads)')
    p.add_argument('--messages', action='store_true', help='Include the stock message-ID rings (18 extra reads)')
    p.add_argument('--engine', action='store_true', help='Include status cache, print geometry, ASF/LF/edge state (9 extra reads)')
    p.add_argument('--output', help='New JSONL evidence file; existing files are refused')
    args = p.parse_args()
    if args.count < 1 or args.interval < 0.1:
        p.error('count must be positive and interval at least 0.1 seconds')
    dev, reader = connect()
    import usb.util
    output = None
    try:
        if args.output: output = open(args.output, 'x', encoding='utf8')
        for i in range(args.count):
            result = reader.snapshot(args.setup_history, args.messages, args.engine)
            result['usb_identity'] = {'vid': hex(dev.idVendor), 'pid': hex(dev.idProduct),
                                      'bcd_device': hex(dev.bcdDevice), 'product': 'MG2500 series'}
            line = json.dumps(result)
            print(line, flush=True)
            if output: output.write(line+'\n'); output.flush()
            if i+1 < args.count: time.sleep(args.interval)
    finally:
        if output: output.close()
        usb.util.dispose_resources(dev)


if __name__ == '__main__': main()
