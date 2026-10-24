"""Scanner capture-engine model for emulation.

Extends the bootloader's ARM Model with a faithful-enough SCNIO + LDMAC capture:
when the firmware issues the acquisition request (SCNIO +0x0c nonzero while the
engine is enabled), the modeled LDMAC DMAs a synthetic line into the per-channel
destination buffers and raises the completion signals — matching the behavior we
observed on hardware (flat 0x8180 when scanner power is off; real per-pixel data
when on). This lets the app's scanner_full_capture run off-hardware.
"""
import struct
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'bootloader' / 'tests'))
from model import Model, checked  # noqa: E402

SEG_U16 = 0xc00 // 2  # 16-bit samples per LDMAC channel segment (width 0xc00 bytes)


class ScannerModel(Model):
    def __init__(self, *a, scan_image=None, **k):
        super().__init__(*a, **k)
        self.scan_image = scan_image      # fn(sample_index)->u16, or None for a default ramp
        self.scan_captures = 0

    @checked
    def read(self, cpu, offset, size, user):
        address = 0xc0000000 + offset
        if address == 0xc0120110:         # AFE serial "transfer complete" so afe_word() won't spin
            return 1
        return super().read(cpu, offset, size, user)

    @checked
    def write(self, cpu, offset, size, value, user):
        r = super().write(cpu, offset, size, value, user)
        address = 0xc0000000 + offset
        # SCNIO acquisition request: +0x0c nonzero while the engine is enabled (+0 == 1).
        if address == 0xc0d6000c and value and self.regs.get(0xc0d60000) == 1:
            self._scan_dma(cpu)
        return r

    def _scan_dma(self, cpu):
        powered = self.regs.get(0xc0c85614, 0) & 1
        for ch, dest_reg in ((0, 0xc0d50014), (1, 0xc0d50114)):
            dest = self.regs.get(dest_reg, 0)
            if not dest:
                continue
            out = bytearray()
            segment = (self.regs.get(dest_reg + 0xc, 0) & 0xffff) // 2
            for i in range(segment):
                sample = ch * segment + i
                if not powered:
                    v = 0x8180
                elif self.scan_image:
                    v = self.scan_image(sample) & 0xffff
                else:
                    v = (0x6400 + (sample & 0xff)) & 0xffff
                out += struct.pack('<H', v)
            cpu.mem_write(dest, bytes(out))
        # completion signals the firmware reads back
        self.regs[0xc0d50010] = self.regs.get(0xc0d50010, 0) | 4
        self.regs[0xc0d50110] = self.regs.get(0xc0d50110, 0) | 4
        self.regs[0xc0d60010] = self.regs.get(0xc0d60010, 0) + 1
        self.scan_captures += 1
