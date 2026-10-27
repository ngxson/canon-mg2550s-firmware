"""Project emitted lane rasters onto paper, independently of the band scheduler.

The input uses only pure K/C/M/Y and white, so the expected dot map does not
depend on the firmware's halftone implementation. This checks both carriage
segments, every row, the colour/black lead, trailing blanks and duplicate ink.
It models commanded feed geometry, not physical nozzle or motor behaviour.
"""
import struct
import time
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1

WIDTH, HEIGHT = 2480, 124
PALETTE = ((0, 0, 0), (0, 255, 255), (255, 0, 255), (255, 255, 0))


def pixel(x, y):
    return (255, 255, 255) if (x + 3 * y) % 29 < 3 else PALETTE[(x // 193 + y // 7) % 4]


def input_rows(mono=False):
    if mono:
        return [bytes(255 if (x + 3 * y) % 29 < 3 else 0 for x in range(WIDTH)) for y in range(200)]
    return [bytes(v for x in range(WIDTH) for v in pixel(x, y)) for y in range(HEIGHT)]


class Capture:
    def __init__(self, e, manifest, mono=False):
        self.e = e
        self.mono = mono
        self.paper = None
        self.advances = []
        self.passes = []
        self.height = 2 * ((200 if mono else HEIGHT) - 38)
        self.landed = [bytearray(528 * self.height) for _ in range(4)]
        self.symbols = {k: int(v, 16) for k, v in manifest['symbols']['application'].items()}
        self.last_report = 0
        start = int(manifest['symbols']['application']['hybrid_run'], 16)
        e.uc.hook_add(UC_HOOK_CODE, self.request, begin=start, end=start)
        e.uc.hook_add(UC_HOOK_CODE, self.capture, begin=0xe6aea, end=0xe6aea)
        poll = self.symbols['print_engine_poll']
        e.uc.hook_add(UC_HOOK_CODE, self.progress, begin=poll, end=poll)
        convert = self.symbols['convert_row']
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
                      for k in ('rows_in', 'band_next', 'seg_index', 'nozzle_phase')}
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
        assert x in ((0,) if self.mono else (0, 2112)), ('horizontal segment placement', x)
        assert self.paper is not None
        counts = []
        for plane, lane in enumerate((0,) if self.mono else (0, 2, 5, 7)):
            base = struct.unpack_from('<I', q, 0x11c + 4 * lane)[0]
            stride = struct.unpack_from('<I', q, 0x16c + 4 * lane)[0]
            tiles = 264 if self.mono else 132
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
        self.passes.append({'paper_row': self.paper, 'x': x, 'dots': counts})
        print('quality emitted', self.passes[-1], flush=True)

    def verify(self):
        assert self.advances == ([650, 32, 576, 32] if self.mono else [394, 32, 272, 32]), self.advances
        positions = [(0, 0), (16, 0), (304, 0), (320, 0)] if self.mono else [
            (0, 0), (0, 2112), (16, 0), (16, 2112),
            (152, 0), (152, 2112), (168, 0), (168, 2112)]
        assert [(p['paper_row'], p['x']) for p in self.passes] == positions, self.passes
        expected = [bytearray(528 * self.height) for _ in range(4)]
        for y in range(self.height):
            for x in range(4224):
                sx, sy = 183 + x * 125 // 256, 38 + y // 2
                rgb = ((255, 255, 255) if (sx + 3 * sy) % 29 < 3 else (0, 0, 0)) if self.mono else pixel(sx, sy)
                if rgb == (255, 255, 255):
                    continue
                plane = PALETTE.index(rgb)
                expected[plane][y * 528 + x // 8] |= 1 << (x % 8)
        for plane in range(4):
            bad = [i for i, (a, b) in enumerate(zip(self.landed[plane], expected[plane])) if a != b]
            assert not bad, ('paper raster mismatch', plane, len(bad), bad[:12])
        print('PASS quality raster:', 'mono' if self.mono else 'colour',
              'all paper dots exact, no duplicates or stray rows; all segments, band seam and final partial band; feed pitch unchanged')
