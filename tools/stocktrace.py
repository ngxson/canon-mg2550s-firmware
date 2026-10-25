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
ABI = 2
REGIONS = ((0x1804099c, 4), (0x1803fcc4, 8),
           (0x1803fccc, 1024), (0x180400cc, 1024),
           (0x180404cc, 2), (0x180404ce, 512), (0x180406ce, 512))
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

    def snapshot(self, history=False, messages=False):
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
            result = reader.snapshot(args.setup_history, args.messages)
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
