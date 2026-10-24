#!/usr/bin/env python3
"""eSCL backed by the scanner page sequencer, with scripted motion and DMA.

Runs the flashed (scanner-source) app behind the real bootloader. An IPP-USB
host creates eSCL jobs; the page sequencer homes, steps and captures; the
scan source turns each synthetic 2592-sample line into pixels. Checks exact
pixels (PNG), positions including a hopped start offset, ownership against
the vendor page protocol, return home, cancellation and back-to-back jobs.
Motor physics and optics are not modelled: this verifies software contracts.
"""
import hashlib
import json
import struct
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests'))
sys.path.insert(0, str(APP / 'tests'))
from model import setup  # noqa: E402
from validate_page import PageModel  # noqa: E402
from model_scanner import ScannerModel  # noqa: E402
from unicorn import UC_HOOK_BLOCK, UC_HOOK_CODE  # noqa: E402

# The motor controller runs a 1 ms loop and stops a move when more than 4 ms
# pass between scanner_motor_poll() calls. Measured on the board, the CPU
# executes ~3.8 M instructions/s (caches off), so 4 ms is ~15 k instructions.
# Protocol work during motion must leave a clear margin below that. Measured
# from each poll's return to the next poll's entry: the driver's own move
# set-up (logging, deliberate delays) is existing, physically proven code.
MOTION_GAP_LIMIT = 8000
import validate_escl as v  # noqa: E402

SAMPLES, SPAN, BLACK, WHITE = 2592, 2550, 26000, 65000


class ScanHost(v.IppUsbHost, PageModel):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.lines = []           # samples of every capture, in order
        self.positions = []       # encoder position at each capture
        self.instructions = 0
        self.last_poll = None
        self.motion_gaps = []     # instructions between motor polls while driving
        self.uc.hook_add(UC_HOOK_BLOCK, self._count)
        poll = int(self.meta['symbols']['application']['scanner_motor_poll'], 16)
        self.uc.hook_add(UC_HOOK_CODE, self._motor_poll, begin=poll, end=poll)
        listing = (Path(self.meta['build']) / 'application.dis').read_text()
        for line in listing.splitlines():
            # app_yield tail-calls the poll, so its callers are return sites too.
            if line.rstrip().endswith(('<scanner_motor_poll>', '<app_yield>')) and '\tbl\t' in line:
                site = int(line.split(':')[0], 16) + 4
                self.uc.hook_add(UC_HOOK_CODE, self._motor_poll_return, begin=site, end=site)

    def _count(self, uc, address, size, user):
        self.instructions += size // 4

    def _motor_poll(self, uc, address, size, user):
        if self.drive is not None and self.last_poll is not None:
            self.motion_gaps.append(self.instructions - self.last_poll)
        self.last_poll = None

    def _motor_poll_return(self, uc, address, size, user):
        self.last_poll = self.instructions

    def _scan_dma(self, cpu):
        index = len(self.lines)
        line = [25000 + (s * 17 + index * 331) % 40536 for s in range(SAMPLES)]
        self.lines.append(line)
        self.positions.append(self.regs.get(0xc0c57100, 0))
        self.scan_image = lambda s: line[s]
        assert self.drive is None, 'capture while motor powered'
        ScannerModel._scan_dma(self, cpu)   # PageModel's own ramp would replace ours


def fetch_while_polling(e, c, path):
    """GET on interface c sent at once (as macOS does, i.e. during homing) while
    the other interface polls ScannerStatus, like Image Capture."""
    other = 1 - c
    v.send(e, c, f'GET {path} HTTP/1.1\r\nHost: {v.HOST}\r\n\r\n'.encode())
    polls = 0
    while True:
        buf = bytes(e.from_device[c][e.cursor[c]:])
        last = e.transfers[c][-1] if e.transfers[c] else None
        if v.parse_response(buf) and last is not None and (last == 0 or last % e.max_packet):
            break
        v.scanner_status(e, other)
        polls += 1
    status, headers, body, _ = v.receive(e, c)
    return status, headers, body, polls


def expected_row(line, x, step, pixels):
    row = bytearray()
    for i in range(pixels):
        u = x + i * step
        s0, s1 = u * SAMPLES // SPAN, (u + step) * SAMPLES // SPAN
        s0 = min(s0, SAMPLES - 1); s1 = min(s1, SAMPLES)
        s1 = max(s1, s0 + 1); s1 = min(s1, s0 + 8)
        n = s1 - s0
        level = sum(line[s0:s1]) * ((65536 + n - 1) // n) >> 16       # firmware fixed point
        value = 0 if level <= BLACK else (level - BLACK) * (255 * 65536 // (WHITE - BLACK)) >> 16
        row.append(min(value, 255))
    return bytes(row)


def main():
    factory, app, metadata = v.factory_and_metadata(APP / 'build')
    metadata['build'] = str(APP / 'build')
    symbols = metadata['symbols']['application']
    report = {'assumptions': __doc__, 'app_sha256': hashlib.sha256(app).hexdigest(), 'cases': {}}

    def record(name, **details):
        report['cases'][name] = details
        print('PASS', name, json.dumps(details) if details else '', flush=True)

    def phase(e):
        return e.uc.mem_read(int(symbols['page_phase'], 16), 1)[0]

    def word(e, name):
        return struct.unpack('<I', e.uc.mem_read(int(symbols[name], 16), 4))[0]

    def idle_hardware(e):
        assert e.drive is None and e.regs[0xc0c56000] == 0 and e.regs[0xc0c85614] == 0
        assert not e.erases and not e.programs

    def booted():
        e = ScanHost(image=factory, metadata=metadata)
        e.run(v.app_mode)
        return e

    e = booted()
    status, _, body, _ = v.http(e, 1, 'GET', '/eSCL/ScannerCapabilities')
    caps = v.ET.fromstring(body)
    modes = [m.text for m in caps.iterfind('.//scan:ColorMode', v.NS)]
    resolutions = [int(r.text) for r in caps.iterfind('.//scan:XResolution', v.NS)]
    assert modes == ['BlackAndWhite1', 'Grayscale8'] and resolutions == [75, 150], (modes, resolutions)
    assert v.create_job(e, 0, v.settings(mode='RGB24'))[0] == 409
    record('capabilities_are_the_scanners', modes=modes, resolutions=resolutions)

    # A region below the home position: 40/300 in = 5120 counts, i.e. hops.
    x, y, w, h, res = 300, 40, 1500, 120, 75
    status, location, _ = v.create_job(e, 0, v.settings(mode='Grayscale8', res=res, region=(x, y, w, h)))
    assert status == 201
    job = '/eSCL/' + location.split('/eSCL/', 1)[1]
    status, headers, body, polls = fetch_while_polling(e, 0, job + '/NextDocument')
    assert status == 200 and headers['content-type'] == 'image/png'
    pixels, lines = w * res // 300, h * res // 300
    width, height, ctype, rows, phys, depth = v.decode_png(body)
    assert (width, height, ctype, depth) == (pixels, lines, 0, 8)
    assert len(e.lines) == lines
    for j in range(lines):
        assert rows[j] == expected_row(e.lines[j], x, 300 // res, pixels), f'row {j} differs'
    home = word(e, 'home_position')
    offsets = [p - home for p in e.positions]
    assert all(abs(o - (y * 128 + j * 512)) <= 48 for j, o in enumerate(offsets)), offsets[:4]
    e.run(lambda q: phase(q) in (7, 8, 9), resume=True, budget=400_000_000)
    assert phase(e) == 7
    idle_hardware(e)
    record('region_with_offset_exact_pixels_and_positions', status_polls=polls, pixels=[pixels, lines],
           first_offset=offsets[0], pitch_counts=offsets[1] - offsets[0])

    # Back-to-back: a job posted while the last page returns home adopts it.
    status, location, _ = v.create_job(e, 0, v.settings(mode='Grayscale8', res=75, region=(0, 0, 2550, 32)))
    assert status == 201
    status, headers, body, _ = v.http(e, 0, 'GET', '/eSCL/' + location.split('/eSCL/', 1)[1] + '/NextDocument')
    assert status == 200
    e.run(lambda q: phase(q) == 6, resume=True, budget=400_000_000)        # returning home
    status, location, _ = v.create_job(e, 1, v.settings(mode='BlackAndWhite1', res=150, region=(0, 0, 600, 16),
                                                       fmt='image/jpeg'))
    assert status == 201, 'second job refused while returning home'
    status, headers, body, _ = fetch_while_polling(e, 1, '/eSCL/' + location.split('/eSCL/', 1)[1] + '/NextDocument')
    assert status == 200 and headers['content-type'] == 'image/jpeg' and body[:2] == b'\xff\xd8'
    e.run(lambda q: phase(q) in (7, 8, 9), resume=True, budget=400_000_000)
    assert phase(e) == 7
    idle_hardware(e)
    record('job_adopts_return_home', jpeg_bytes=len(body))

    # Cancel while the page is running: motor and lamp off, page cancelled.
    status, location, _ = v.create_job(e, 0, v.settings(mode='Grayscale8', res=75, region=(0, 0, 2550, 400)))
    assert status == 201
    job = '/eSCL/' + location.split('/eSCL/', 1)[1]
    e.hold_in.add(0)
    v.send(e, 0, f'GET {job}/NextDocument HTTP/1.1\r\nHost: {v.HOST}\r\n\r\n'.encode())
    e.run(lambda q: phase(q) == 4, resume=True, budget=400_000_000)
    # The vendor page protocol cannot read or acknowledge an eSCL-owned row.
    stalls = len(e.stalls)
    e.requests.appendleft(('vendor_data', setup(0xc0, 0x79, 0, 0, 256)))
    e.requests.appendleft(('vendor_ack', setup(0x40, 0x7a, 0)))
    e.run(lambda q: len(q.stalls) >= stalls + 2, resume=True)
    assert phase(e) == 4
    assert v.http(e, 1, 'DELETE', job)[0] == 200
    e.run(lambda q: phase(q) == 9, resume=True)
    e.hold_in.discard(0)
    status, _, body, _ = v.receive(e, 0)
    assert status == 200
    idle_hardware(e)
    assert v.scanner_status(e, 1)[1]['JobState'] == 'Canceled'
    record('delete_cancels_page_vendor_rows_refused')

    worst = max(e.motion_gaps)
    assert worst <= MOTION_GAP_LIMIT, f'motor poll gap {worst} instructions while driving'
    record('motor_serviced_during_escl_work', worst_gap_instructions=worst, samples=len(e.motion_gaps))

    report['all_assertions_passed'] = True
    (APP / 'build/validation_escl_scan.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL ESCL SCAN ASSERTIONS PASSED', flush=True)


if __name__ == '__main__':
    main()
