"""Project emitted lane rasters onto paper, independently of the band scheduler.

The input uses only pure K/C/M/Y and white, so the expected dot map does not
depend on the firmware's halftone implementation. This checks both carriage
segments, every row, the colour/black lead, trailing blanks and duplicate ink.
It models commanded feed geometry, not physical nozzle or motor behaviour.

The sweep plan is re-derived here from the expected paper image: for every
band and nozzle phase, the occupied 16-column tiles are covered greedily by
windows of at most one segment (2112 colour / 4224 mono columns), each cropped
to its last occupied tile at 32-column granularity; an empty phase has no
sweep. The firmware's sweeps (placement, width, order) must match exactly, so
skipped phases and cropped sweeps are checked, not just a sweep count. The
`sparse` fixture has a blank first phase, two narrow marks far apart in one
phase, a mark crossing the old 2112 seam, and a blank trailing phase.
"""
import struct
import time
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1

WIDTH, HEIGHT = 2480, 124
PALETTE = ((0, 0, 0), (0, 255, 255), (255, 0, 255), (255, 255, 0))


def pixel(x, y):
    return (255, 255, 255) if (x + 3 * y) % 29 < 3 else PALETTE[(x // 193 + y // 7) % 4]


def pixel_sparse(x, y):
    if 46 <= y < 54 and 300 <= x < 400: return PALETTE[0]        # K, band 0 second phase, left
    if 46 <= y < 54 and 1900 <= x < 1960: return PALETTE[2]      # M, same phase, far right: a second cropped sweep
    if 114 <= y < 122 and 1200 <= x < 1300: return PALETTE[3]    # Y, band 1 first phase, across the old 2112-column seam
    return (255, 255, 255)


DEEP = 300   # `deep`: the sparse marks 300 input rows lower, behind four empty bands (feeds merge into 1200 + 16 Ulf)


def pixel_deep(x, y):
    return pixel_sparse(x, y - DEEP)


def input_rows(mono=False, sparse=False, deep=False):
    if mono:
        return [bytes(255 if (x + 3 * y) % 29 < 3 else 0 for x in range(WIDTH)) for y in range(200)]
    f = pixel_deep if deep else pixel_sparse if sparse else pixel
    return [bytes(v for x in range(WIDTH) for v in f(x, y)) for y in range(HEIGHT + (DEEP if deep else 0))]


class Capture:
    def __init__(self, e, manifest, mono=False, sparse=False, deep=False):
        self.e = e
        self.mono = mono
        self.sparse = sparse
        self.deep = deep
        self.paper = None
        self.advances = []
        self.passes = []
        self.height = 2 * ((200 if mono else HEIGHT + (DEEP if deep else 0)) - 38)
        self.landed = [bytearray(528 * self.height) for _ in range(4)]
        self.symbols = {k: int(v, 16) for k, v in manifest['symbols']['application'].items()}
        self.last_report = 0
        start = int(manifest['symbols']['application']['hybrid_run'], 16)
        e.uc.hook_add(UC_HOOK_CODE, self.request, begin=start, end=start)
        e.uc.hook_add(UC_HOOK_CODE, self.capture, begin=0xe6aea, end=0xe6aea)
        poll = self.symbols['print_engine_poll']
        e.uc.hook_add(UC_HOOK_CODE, self.progress, begin=poll, end=poll)
        convert = self.symbols['convert_pair']
        e.uc.hook_add(UC_HOOK_CODE, self.conversion, begin=convert, end=convert)

    def conversion(self, cpu, address, size, user):
        # Stock LF/CR servos are polled in the shim's blocking calls. A row
        # conversion yields only to the scanner, so it must not run there.
        running = cpu.mem_read(self.symbols['running'], 1)[0]
        pending = int.from_bytes(cpu.mem_read(self.symbols['routine_pending'], 2), 'little')
        assert not running and not pending, ('raster conversion starves stock motor polling', running, pending)

    def progress(self, cpu, address, size, user):
        if time.monotonic() - self.last_report >= 20:
            self.last_report = time.monotonic()
            values = {k: int.from_bytes(cpu.mem_read(self.symbols[k], 4), 'little')
                      for k in ('rows_in', 'band_next', 'plan_i', 'plan_n', 'nozzle_phase')}
            values.update({k: int.from_bytes(cpu.mem_read(self.symbols[k], 1), 'little')
                           for k in ('state', 'step', 'waiting', 'page_open', 'dry', 'running')})
            values['routine_pending'] = int.from_bytes(cpu.mem_read(self.symbols['routine_pending'], 2), 'little')
            print('quality progress', values, 'captured sweeps', len(self.passes), flush=True)

    def request(self, cpu, address, size, user):
        routine, arg = cpu.reg_read(UC_ARM_REG_R0), cpu.reg_read(UC_ARM_REG_R1)
        if routine & 0xff == 10:
            self.advances.append(arg)
            if self.paper is None:
                assert arg == (650 if self.mono else 394), ('first-line advance', arg)
                self.paper = 0
            else:
                assert arg % 2 == 0
                self.paper += arg // 2

    def capture(self, cpu, address, size, user):
        q = bytes(cpu.mem_read(cpu.reg_read(UC_ARM_REG_R0), 0x500))
        place = struct.unpack_from('<h', q, 0x3a)[0]
        x = (4 * place + 34272 - (35008 + 2688)) // 16
        N = struct.unpack_from('<h', q, 0x6a)[0] // 4               # lane 0 extent = 4 x band columns
        assert x % 16 == 0 and N % 32 == 0 and 32 <= N <= (4224 if self.mono else 2112) and 0 <= x and x + N <= 4224, ('sweep placement', x, N)
        tiles = N // 16
        assert self.paper is not None
        counts = []
        for plane, lane in enumerate((0,) if self.mono else (0, 2, 5, 7)):
            base = struct.unpack_from('<I', q, 0x11c + 4 * lane)[0]
            stride = struct.unpack_from('<I', q, 0x16c + 4 * lane)[0]
            data = bytes(cpu.mem_read(base, tiles * stride))
            dots = 0
            for r in range(304 if plane == 0 else 160):
                y = self.paper + r - (128 if plane == 0 and not self.mono else 0)
                for tile in range(tiles):
                    pos = tile * stride + 2 * r
                    for byte in range(2):
                        bits = data[pos + byte]
                        if not bits:
                            continue
                        assert r % 32 < 16, ('weak nozzle rows used', plane, r)
                        assert 0 <= y < self.height, ('ink outside page data', plane, r, y)
                        dst = y * 528 + x // 8 + tile * 2 + byte
                        assert not self.landed[plane][dst] & bits, ('duplicate paper dots', plane, y, dst % 528)
                        self.landed[plane][dst] |= bits
                        dots += bits.bit_count()
            counts.append(dots)
        self.passes.append({'paper_row': self.paper, 'x': x, 'N': N, 'dots': counts})
        print('quality emitted', self.passes[-1], flush=True)

    def expected_image(self):
        expected = [bytearray(528 * self.height) for _ in range(4)]
        f = pixel_deep if self.deep else pixel_sparse if self.sparse else pixel
        for y in range(self.height):
            for x in range(4224):
                sx, sy = 183 + x * 125 // 256, 38 + y // 2
                rgb = ((255, 255, 255) if (sx + 3 * sy) % 29 < 3 else (0, 0, 0)) if self.mono else f(sx, sy)
                if rgb == (255, 255, 255):
                    continue
                plane = PALETTE.index(rgb)
                expected[plane][y * 528 + x // 8] |= 1 << (x % 8)
        return expected

    def expected_plan(self, expected=None):
        """[(paper_row, x, N)] per sweep, the number of empty phases and the feed list (first-line advance,
        then each pass's feed; consecutive skipped passes merge into moves of at most 1200 Ulf before the
        next printed pass, and feeds pending at the eject are dropped), from the expected image alone."""
        expected = expected or self.expected_image()
        band_rows, lead, seg = (304, 0, 4224) if self.mono else (152, 128, 2112)
        bands = max(1, -(-(self.height + lead) // band_rows))
        plan, skipped, feeds, pending = [], 0, [650 if self.mono else 394], 0
        for b in range(bands):
            for phase in (0, 16):
                feed = 32 if phase == 0 else 2 * band_rows - 32
                occupied = set()
                for plane in range(1 if self.mono else 4):
                    row0 = b * band_rows - (lead if plane == 0 else 0)
                    for r in range(band_rows):
                        d = row0 + r + phase
                        if r % 32 < 16 and r + phase < band_rows and 0 <= d < self.height:
                            occupied.update(i // 2 for i in range(528) if expected[plane][d * 528 + i])
                if not occupied:
                    skipped += 1
                    pending += feed
                    continue
                while pending:
                    feeds.append(min(pending, 1200)); pending -= feeds[-1]
                if b + 1 < bands or phase == 0:
                    feeds.append(feed)
                t = min(occupied)
                while t < 264:
                    window = [u for u in occupied if t <= u < t + seg // 16]
                    cols = -(-(max(window) - t + 1) * 16 // 32) * 32
                    x = min(t * 16, 4224 - cols)
                    plan.append((b * band_rows + phase, x, cols))
                    later = [u for u in occupied if u >= t + seg // 16]
                    t = min(later) if later else 264
        return plan, skipped, feeds

    def verify(self):
        expected = self.expected_image()
        plan, skipped, feeds = self.expected_plan(expected)
        assert self.advances == feeds, (self.advances, feeds)
        assert [(p['paper_row'], p['x'], p['N']) for p in self.passes] == plan, (self.passes, plan)
        for plane in range(4):
            bad = [i for i, (a, b) in enumerate(zip(self.landed[plane], expected[plane])) if a != b]
            assert not bad, ('paper raster mismatch', plane, len(bad), bad[:12])
        print('PASS quality raster:', 'mono' if self.mono else 'deep' if self.deep else 'sparse' if self.sparse else 'colour',
              f'all paper dots exact, no duplicates or stray rows; {len(plan)} sweeps as planned from the expected image ({skipped} empty phases skipped); feeds {feeds} as expected')
