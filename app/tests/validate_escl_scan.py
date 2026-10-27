#!/usr/bin/env python3
"""eSCL backed by the scanner page sequencer, with scripted motion and DMA.

75 dpi pages use continuous travel with moving line acquisition; 150 dpi
pages use stop-and-capture. IRQ19, the acquisition-position latch, the lead
comparator and the 0x1440 row stride are modelled from the static analysis
of the stock scanner path (see validate_burst.py); they are not hardware facts.

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
from model import checked, setup  # noqa: E402
from validate_page import PageModel  # noqa: E402
from model_scanner import ScannerModel  # noqa: E402
from unicorn import UC_HOOK_BLOCK, UC_HOOK_CODE  # noqa: E402

# The motor controller runs a 1 ms loop and stops a move when more than 4 ms
# pass between scanner_motor_poll() calls. Measured on the board, the CPU
# executes ~3.8 M instructions/s (caches off), so 4 ms is ~15 k instructions.
# Protocol work during motion must leave a clear margin below that. Measured
# from each poll's return to the next poll's entry: the driver's own move
# set-up (logging, deliberate delays) is existing, physically proven code.
MOTION_GAP_LIMIT = 16000   # IPP-USB request dispatch (ippusb_poll) runs ~15k instructions without a motor service
import validate_escl as v  # noqa: E402
from unicorn.arm_const import UC_ARM_REG_LR  # noqa: E402

SAMPLES, SPAN, BLACK, WHITE = 2592, 2550, 26000, 65000
DARK = 1200
# Optical geometry, independent of the firmware's chosen calibration window
# (1/300 inch from mechanical start). The strip has a central dark notch
# through row 31, measured in scanner_stripe/reference_rows_0_171.bin.
STRIP, DARKBAR, DOC_ORIGIN, SAMPLE_ORIGIN = (20, 90), (132, 148), 156, 4


def document_lines(e, start, y, count):
    """The first `count` moving captures at or past the document origin of region row y."""
    sel = [(l, p_) for l, m, u, p_ in zip(e.lines[start:], e.moving[start:], e.units[start:], e.positions[start:]) if m and u >= DOC_ORIGIN + y]
    assert len(sel) >= count, (len(sel), count)
    return [l for l, _ in sel[:count]], [p_ for _, p_ in sel[:count]]


def white_line():
    # a uniform-enough strip: the firmware accepts a reference only when max <= 1.5 x min
    return [40000 + (s * 23) % 8000 for s in range(SAMPLES)]


MP = 0xc0c57000


class ScanHost(v.IppUsbHost, PageModel):
    def __init__(self, **kw):
        self.irq_polls = 0; self.irq_pending = False; self.line_index = 0
        super().__init__(**kw)
        self.lines = []           # samples of every capture, in order
        self.moving = []          # True for a scan line (moving, lit); False for a calibration capture
        self.positions = []       # encoder position at each capture
        self.units = []           # 1/300 inch from the mechanical start (run-in geometry), -1 when stationary
        self.page_first = None    # moving captures so far in the current page (None between pages)
        self.instructions = 0
        self.last_poll = None
        self.motion_gaps = []     # instructions between motor polls while driving
        self.worst_gap = (0, '', '', 0, None)   # (instructions, caller lr, previous poll return site, captures so far, code at the limit)
        self.last_site = 0; self.gap_site = None
        self.uc.hook_add(UC_HOOK_BLOCK, self._count)
        poll = int(self.meta['symbols']['application']['scanner_motor_poll'], 16)
        self.uc.hook_add(UC_HOOK_CODE, self._motor_poll, begin=poll, end=poll)
        listing = (Path(self.meta['build']) / 'application.dis').read_text()
        for line in listing.splitlines():
            # app_yield tail-calls the poll, so its callers are return sites too.
            if line.rstrip().endswith(('<scanner_motor_poll>', '<app_yield>')) and '\tbl\t' in line:
                site = int(line.split(':')[0], 16) + 4
                self.uc.hook_add(UC_HOOK_CODE, self._motor_poll_return, begin=site, end=site)

    @checked
    def read(self, cpu, offset, size, user):
        address = 0xc0000000 + offset
        if address == 0xc0130020:                      # IRQ19 group pending
            if self.regs.get(0xc0130028, 0) & 2:
                self.irq_polls += 1
                if not self.irq_pending and self.irq_polls % 60 == 0:
                    self.irq_pending = True
            return 2 if self.irq_pending else 0
        if address == MP + 0x14c:                     # acquisition-position latch
            return super().read(cpu, (MP + 0x100) - 0xc0000000, size, user)
        if address == MP + 0x404:                     # one-shot lead comparator
            here = super().read(cpu, (MP + 0x100) - 0xc0000000, size, user)
            if self.regs.get(MP + 0x400, 0) & 2 and here >= self.regs.get(MP + 0x108, 0):
                self.regs[MP + 0x404] = self.regs.get(MP + 0x404, 0) | 2
            return self.regs.get(MP + 0x404, 0)
        return super().read(cpu, offset, size, user)

    @checked
    def write(self, cpu, offset, size, value, user):
        address = 0xc0000000 + offset
        if address == 0xc0d50014:                     # a new LDMAC setup starts a new page's line count
            self.page_first = None
        if address == 0xc0133104 and value & 0x10:
            self.irq_pending = False
        if address == 0xc0130028 and value & 2 and not self.regs.get(0xc0130028, 0) & 2:
            self.line_index = 0
        result = super().write(cpu, offset, size, value, user)
        if address == 0xc013002c and value & 2:
            self.regs[0xc0130028] = self.regs.get(0xc0130028, 0) & ~2
            self.irq_pending = False
        return result

    def _count(self, uc, address, size, user):
        self.instructions += size // 4
        if self.drive is not None and self.last_poll is not None and self.gap_site is None \
                and self.instructions - self.last_poll > MOTION_GAP_LIMIT:
            self.gap_site = hex(address)              # code running when a motion gap passes the limit

    def _motor_poll(self, uc, address, size, user):
        if self.drive is not None and self.last_poll is not None:
            gap = self.instructions - self.last_poll
            self.motion_gaps.append(gap)
            if gap > self.worst_gap[0]:
                self.worst_gap = (gap, hex(uc.reg_read(UC_ARM_REG_LR)), hex(self.last_site), len(self.lines), self.gap_site)
        self.last_poll = None; self.gap_site = None

    def _motor_poll_return(self, uc, address, size, user):
        self.last_poll = self.instructions; self.last_site = address

    def _scan_dma(self, cpu):
        index = len(self.lines)
        moving = bool(self.regs.get(0xc0d6011c, 0) & 0x10000)
        dark = self.regs.get(0xc0d60268, 0) & 0xfffff == self.regs.get(0xc0d60264, 0) & 0xfffff   # windows closed
        pos = self.regs.get(0xc0c57100, 0)
        u = -1
        if moving:
            # Line k of a page lies k pitches past the first: the firmware indexes its run-in the same way.
            if self.page_first is None: self.page_first = 0
            pitch = struct.unpack('<I', self.uc.mem_read(int(self.meta['symbols']['application']['pitch'], 16), 4))[0]
            u = self.page_first * pitch // 128; self.page_first += 1
        else:
            self.page_first = None
        if dark:
            line = [DARK] * SAMPLES
        elif moving and STRIP[0] <= u < STRIP[1]:
            line = white_line()                              # the lid's white strip during the run-in
            if u < 32:
                line[1180:1416] = [DARK] * (1416 - 1180)
        elif moving and DARKBAR[0] <= u < DARKBAR[1]:
            line = [DARK] * SAMPLES                          # the platen-edge dark bar
        elif moving:
            # Midtones expose a wrong reference gain; saturated white would
            # hide it. Enabled by the focused central-notch colour case.
            line = ([18000 + (s % 97) * 41 for s in range(SAMPLES)]
                    if getattr(self, 'reference_notch_probe', False)
                    else [25000 + (s * 17 + index * 331) % 40536 for s in range(SAMPLES)])
        else:
            line = white_line()                              # stationary reference: the same every capture
        self.lines.append(line)
        self.moving.append(moving and not dark)
        self.units.append(u)
        self.positions.append(pos)
        if not moving:
            assert self.drive is None, 'stationary capture while motor powered'
        k = self.line_index; self.line_index += 1
        mask = self.regs.get(0xc0d6000c, 0)
        components = 3 if mask == 7 else 1
        for comp in range(components):
            t = k * components + comp
            for ch in (0, 1):
                base = 0xc0d50000 + ch * 0x100
                lower, upper = self.regs.get(base + 0x18, 0), self.regs.get(base + 0x1c, 0)
                skip2 = self.regs.get(base + 0x34, 0xa20)
                dest = self.regs.get(base + 0x14, 0) + t * (0xa20 + skip2)
                dest = lower + (dest - lower) % (upper - lower)          # ring wrap (modelled, see BRIEF_scan_modes answer §1)
                assert dest + 0xa20 <= upper, 'row outside window'
                shade = [max(v - comp * 2000, 0) for v in line[ch * 1296:(ch + 1) * 1296]]
                cpu.mem_write(dest, struct.pack('<1296H', *shade))
        self.regs[0xc0d50010] = self.regs.get(0xc0d50010, 0) | 4
        self.regs[0xc0d50110] = self.regs.get(0xc0d50110, 0) | 4
        self.regs[0xc0d60010] = self.regs.get(0xc0d60010, 0) + 1     # SCNIO read count
        self.scan_captures += 1


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


GAMMA = [round(255 * (i / 255) ** (1 / 2.2)) for i in range(256)]   # scan_source_scanner.c gamma_lut


def expected_row(line, x, step, pixels, black=BLACK, white=WHITE, shade=True, plane=0):
    row = bytearray()
    wl = [max(w - 2000 * plane, 0) for w in white_line()]       # the model shades component k by -2000k
    dark = max(DARK - 2000 * plane, 0)
    for i in range(pixels):
        u = x + i * step + SAMPLE_ORIGIN
        s0, s1 = u * SAMPLES // SPAN, (u + step) * SAMPLES // SPAN
        s0 = min(s0, SAMPLES - 1); s1 = min(s1, SAMPLES)
        s1 = max(s1, s0 + 1); s1 = min(s1, s0 + 8)
        n = s1 - s0
        if shade:
            span = (230, 230, 217)[plane]                       # stock-match spans and black floor (scan_source_scanner.c)
            scale = [(span * 65536) // (wl[s] - dark) if wl[s] > dark + 1024 else 0 for s in range(s0, s1)]
            if scale[0]:
                acc = sum((max(line[s] - dark, 0) * g) >> 16 for s, g in zip(range(s0, s1), scale))
                value = 14 + (acc * ((65536 + n - 1) // n) >> 16)
                row.append(GAMMA[min(value, 255)])
                continue
        level = sum(line[s0:s1]) * ((65536 + n - 1) // n) >> 16       # firmware fixed point
        value = 0 if level <= black else (level - black) * (255 * 65536 // (white - black)) >> 16
        row.append(GAMMA[min(value, 255)])
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
    assert modes == ['BlackAndWhite1', 'Grayscale8', 'RGB24'] and resolutions == [75, 150, 300], (modes, resolutions)
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
    # Continuous mode without the optional stationary probe: every capture is a scan line.
    scan_lines, scan_positions = document_lines(e, 0, y, lines)
    for j in range(lines):
        exp = expected_row(scan_lines[j], x, 300 // res, pixels)
        if rows[j] != exp:
            k = next(i for i in range(len(exp)) if rows[j][i] != exp[i])
            u = x + k * (300 // res) + SAMPLE_ORIGIN; s0 = u * SAMPLES // SPAN
            raise AssertionError(f'row {j} differs at pixel {k}: got {list(rows[j][k:k + 4])} expected {list(exp[k:k + 4])} raw {scan_lines[j][s0:s0 + 4]} white {white_line()[s0:s0 + 4]}')
    home = word(e, 'home_position')
    offsets = [p - home for p in scan_positions]
    first = 4096 + y * 128                         # continuous mode: CONT_START past home (the model's capture positions lag the gate)
    assert all(o >= first for o in offsets), offsets[:4]
    assert all(b > a for a, b in zip(offsets, offsets[1:])), "positions not increasing"
    e.run(lambda q: phase(q) in (7, 8, 9), resume=True, budget=400_000_000)
    assert phase(e) == 7
    idle_hardware(e)
    # Parked at a fresh reference: the next continuous page skips homing.
    homes_before = sum(1 for d in e.drives if d['direction'])
    status, location, _ = v.create_job(e, 1, v.settings(mode='Grayscale8', res=75, region=(0, 0, 2480, 16)))
    assert status == 201
    e.run(lambda q: phase(q) in (1, 10), resume=True)
    assert phase(e) == 10, f'expected direct scan, phase {phase(e)}'
    status, _, _, _ = v.http(e, 1, 'GET', '/eSCL/' + location.split('/eSCL/', 1)[1] + '/NextDocument')
    assert status == 200
    e.run(lambda q: phase(q) in (7, 8, 9), resume=True, budget=400_000_000)
    assert phase(e) == 7
    record('continuous_page_skips_homing_when_parked', reverse_moves_before=homes_before)

    record('continuous_region_exact_pixels_gated_positions', status_polls=polls, pixels=[pixels, lines],
           first_offset=offsets[0], requests=len(scan_lines))

    # Colour: three component rows per line from the planar ring (component k of
    # line i in row (2N + i + k(2N+1)) mod (3N+1), the modelled stock C=3 layout),
    # request mask 7, 150 dpi continuous; the model shades component k by -2000k.
    x, y, w, h, res = 120, 0, 1200, 64, 150
    captures = len(e.lines)
    status, location, _ = v.create_job(e, 0, v.settings(mode='RGB24', res=res, region=(x, y, w, h)))
    assert status == 201
    status, headers, body, _ = fetch_while_polling(e, 0, '/eSCL/' + location.split('/eSCL/', 1)[1] + '/NextDocument')
    assert status == 200
    pixels, lines = w * res // 300, h * res // 300
    width, height, ctype, rows, phys, depth = v.decode_png(body)
    assert (width, height, ctype, depth) == (pixels, lines, 2, 8), (width, height, ctype, depth)
    colour_lines, _ = document_lines(e, captures, y, lines)
    for j in range(lines):
        planes = [expected_row([max(v_ - 2000 * k, 0) for v_ in colour_lines[j]], x, 300 // res, pixels, plane=k) for k in range(3)]
        expected = bytes(b for i in range(pixels) for b in (planes[0][i], planes[1][i], planes[2][i]))
        if rows[j] != expected:
            log = b''
            for _ in range(60):
                e.requests.appendleft(('log', setup(0xc0, 0x50, 0, 0, 256))); before = len(e.responses)
                e.run(lambda q: len(q.responses) > before, resume=True)
                part = e.responses[-1][2]
                if not part: break
                log += bytes(part)
            print('FIRMWARE LOG:', [l for l in log.decode('latin1').splitlines() if '[cal]' in l][-6:])
            k = next(i for i in range(len(expected)) if rows[j][i] != expected[i])
            raise AssertionError(f'colour row {j} differs at byte {k} (pixel {k // 3} comp {k % 3}): got {list(rows[j][k:k + 6])} expected {list(expected[k:k + 6])}')
    e.run(lambda q: phase(q) in (7, 8, 9), resume=True, budget=400_000_000)
    assert phase(e) == 7
    idle_hardware(e)
    record('colour_rgb24_planar_ring_rows', pixels=[pixels, lines])

    # Back-to-back: a job posted while the last page returns home adopts it.
    status, location, _ = v.create_job(e, 0, v.settings(mode='Grayscale8', res=150, region=(0, 0, 2480, 96)))
    assert status == 201
    status, headers, body, _ = v.http(e, 0, 'GET', '/eSCL/' + location.split('/eSCL/', 1)[1] + '/NextDocument')
    assert status == 200
    # The ring engine delivers the last line before the travel's run-out ends.
    e.run(lambda q: phase(q) in (6, 7, 8, 9), resume=True, budget=400_000_000)
    assert phase(e) == 6, f'expected the page to be returning home, phase {phase(e)}'
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
    status, location, _ = v.create_job(e, 0, v.settings(mode='Grayscale8', res=150, region=(0, 0, 2480, 400)))
    assert status == 201
    job = '/eSCL/' + location.split('/eSCL/', 1)[1]
    e.hold_in.add(0)
    v.send(e, 0, f'GET {job}/NextDocument HTTP/1.1\r\nHost: {v.HOST}\r\n\r\n'.encode())
    e.run(lambda q: phase(q) == 10 and q.drive is not None, resume=True, budget=400_000_000)
    # The vendor page protocol cannot read or acknowledge an eSCL-owned row.
    stalls = len(e.stalls)
    e.requests.appendleft(('vendor_data', setup(0xc0, 0x79, 0, 0, 256)))
    e.requests.appendleft(('vendor_ack', setup(0x40, 0x7a, 0)))
    e.run(lambda q: len(q.stalls) >= stalls + 2, resume=True)
    assert phase(e) == 10
    assert v.http(e, 1, 'DELETE', job)[0] == 200
    e.run(lambda q: phase(q) == 9, resume=True)
    e.hold_in.discard(0)
    status, _, body, _ = v.receive(e, 0)
    assert status == 200
    idle_hardware(e)
    assert v.scanner_status(e, 1)[1]['JobState'] == 'Canceled'
    record('delete_cancels_page_vendor_rows_refused')

    # Continuous page cancelled while travelling: motor and lamp off.
    status, location, _ = v.create_job(e, 0, v.settings(mode='Grayscale8', res=75, region=(0, 0, 2480, 800)))
    assert status == 201
    job = '/eSCL/' + location.split('/eSCL/', 1)[1]
    e.run(lambda q: q.drive is not None and phase(q) == 10, resume=True, budget=400_000_000)
    assert v.http(e, 1, 'DELETE', job)[0] == 200
    e.run(lambda q: phase(q) == 9, resume=True)
    idle_hardware(e)
    record('delete_cancels_continuous_travel')

    worst = max(e.motion_gaps)
    assert worst <= MOTION_GAP_LIMIT, f'motor poll gap {worst} instructions while driving: {e.worst_gap}'
    record('motor_serviced_during_escl_work', worst_gap_instructions=worst, samples=len(e.motion_gaps))

    report['all_assertions_passed'] = True
    (APP / 'build/validation_escl_scan.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL ESCL SCAN ASSERTIONS PASSED', flush=True)


if __name__ == '__main__':
    main()
