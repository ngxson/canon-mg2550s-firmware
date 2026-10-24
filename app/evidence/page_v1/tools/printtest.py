#!/usr/bin/env python3
"""Send a BJL/BJRaster3 stream to the open app over the printer-class bulk OUT EP.

Real-chip test of the USB print transport: PRINT_BEGIN (vendor 0x61, total bytes),
then the stream on bulk EP 0x01, then drain the debug log to see the firmware's
decode result. Head output is a placeholder on this board (head unconnected), so
success = the firmware received the full stream over bulk, reassembled it, and
decoded it (mandatory=0xff, correct plane/row counts).

Usage:  python3 printtest.py [minimal|page]   (default: both)
"""
import sys
import time

VID = 0x04a9
APP_PID = 0x12fe
REQUEST_LOG = 0x50
REQUEST_PRINT_BEGIN = 0x61
BULK_OUT = 0x01


def esc(cmd, payload=b''):
    return bytes([0x1b, 0x28, ord(cmd), len(payload) & 0xff, len(payload) >> 8]) + payload


def pb_encode(data):
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        run = 1
        while i + run < n and data[i + run] == data[i] and run < 128:
            run += 1
        if run >= 2:
            out += bytes([257 - run, data[i]]); i += run
        else:
            start, lit = i, 0
            while i < n and lit < 128 and not (i + 1 < n and data[i] == data[i + 1]):
                i += 1; lit += 1
            out += bytes([lit - 1]) + data[start:i]
    out.append(0x80)
    return bytes(out)


def minimal_band():
    b = b''.join([
        esc('I', b'\x01'), esc('J', b'\x01'), esc('b', b'\x01'),
        esc('c', b'\x30\x00\x00'), esc('d', b'\x02\x58\x02\x58'), esc('l', b'\x34\x00\x00'),
        esc('p', b'\x00' * 46), esc('t', b'\x00' * 42), esc('L', b'K'),
        esc('F', bytes([0xfc, 0xff, 0xfc, 0x00, 0x80])), b'\x1b\x40',
    ])
    return b, {'planes': 1, 'rowbytes': 10}


def cmyk_page():
    raw = [[bytes((i * 7 + pl * 13) & 0xff for i in range(200)), None] for pl in range(4)]
    raw[0][1] = bytes([0xCC] * 5 + [0x10, 0x20, 0x30] + [0] * 12)
    raw[1][1] = bytes([0x4D, 0x4D] + [0x5A] * 10 + [0x01])
    raw[2][1] = bytes([0xFF] * 7)
    raw[3][1] = bytes([0, 0, 0, 0, 0x7F, 0x80, 0x81, 0xFF, 0xFF, 0xFF, 0xFF])
    s = bytearray()
    s += esc('I', b'\x01') + esc('J', b'\x02') + esc('b', b'\x01')
    s += esc('c', b'\x30\x00\x00') + esc('d', b'\x02\x58\x02\x58') + esc('l', b'\x34\x00\x00')
    s += esc('p', b'\x00' * 46) + esc('t', b'\x00' * 42) + esc('L', b'CMYK')
    for pl in range(4):
        s += esc('F', pb_encode(raw[pl][0]) + pb_encode(raw[pl][1]))
    s += b'\x0c\x1b\x40'
    return bytes(s), {'planes': 4, 'rowbytes': 20}


def drain(dev, seconds=3.0):
    import usb.core
    out, deadline = b'', time.time() + seconds
    while time.time() < deadline:
        try:
            d = dev.ctrl_transfer(0xC0, REQUEST_LOG, 0, 0, 256, timeout=500)
        except usb.core.USBError:
            d = None
        if d:
            out += bytes(d)
        else:
            time.sleep(0.05)
    return out


def send(dev, name, stream, expect):
    import usb.core
    drain(dev, 0.3)                                       # flush any stale log
    n = len(stream)
    dev.ctrl_transfer(0x40, REQUEST_PRINT_BEGIN, n & 0xffff, n >> 16, None, timeout=1000)
    dev.write(BULK_OUT, stream, timeout=4000)
    log = drain(dev, 3.0)
    tail = b'\n'.join(l for l in log.split(b'\n') if b'[print]' in l).decode('latin1')
    ok = (f"planes={expect['planes']}".encode() in log
          and f"rowbytes={expect['rowbytes']}".encode() in log
          and b'mandatory=0x000000ff' in log
          and b'emitted head bytes' in log)
    print(f"[{name}] {n} bytes sent")
    print(tail)
    print(f"[{name}] {'PASS' if ok else 'FAIL'}\n")
    return ok


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else 'both'
    import usb.core, usb.util
    dev = usb.core.find(idVendor=VID, idProduct=APP_PID)
    if dev is None:
        print('MG2550s app (04a9:12fe) not found', file=sys.stderr); sys.exit(1)
    try:
        if dev.is_kernel_driver_active(1):
            dev.detach_kernel_driver(1)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        usb.util.claim_interface(dev, 1)
    except usb.core.USBError as e:
        print(f'# could not claim interface 1 ({e}); trying anyway', file=sys.stderr)

    tests = []
    if which in ('minimal', 'both'):
        tests.append(('minimal', *minimal_band()))
    if which in ('page', 'both'):
        tests.append(('cmyk_page', *cmyk_page()))
    results = [send(dev, name, s, exp) for name, s, exp in tests]
    print('ALL REAL-CHIP PRINT TESTS PASSED' if all(results) else 'SOME TESTS FAILED')
    sys.exit(0 if all(results) else 2)


if __name__ == '__main__':
    main()
