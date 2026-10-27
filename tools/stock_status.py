#!/usr/bin/env python3
"""Print one stock GetStatusResponse (state, detail, support code) through the print back-channel.

Use before stock_scan.py: a StartJob is refused with DeviceBusy while the status is
notready (2026-10-27: DoorOpen / support code 1200 with the cartridge cover open)."""
import sys, time
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from stockusb.transport import USB, Trace
from stockusb.printing import parse_status
with USB('print', Trace(None)) as io:
    pending = bytearray(); t0 = time.monotonic(); status = None
    while time.monotonic() - t0 < 5 and status is None:
        try: data = io.read(512, 300)
        except TimeoutError: continue
        pending.extend(data or b'')
        if b'</cmd>' in pending:
            status = parse_status(bytes(pending[:pending.index(b'</cmd>') + 6]))
    print('status', status if status else 'none (%d bytes)' % len(pending))
