"""Printer-side emulation scaffolding.

Extends the scanner model with the print mechanism so we can develop and validate
the open print receiver + motor control OFF-HARDWARE (the print head and its
motors are unconnected, so emulation is the only validation path):

- **Motor family `0xc0c50000–0xc0c58000`** (the CR carriage + LF paper-feed motors
  live here; the scanner CIS carriage motor at `0xc0c56000/57000` is the same
  family, a different channel; print-motor handlers `0xd35c4`/`0xd369a`, IRQ src
  `0xc0133000`). Writes are recorded. A motor marked "running" by a start write
  raises its completion bit after a few polls of its done register — so a (future)
  bounded motor-wait completes in emulation without a real motor. Register roles
  are concrete for the scanner motor; the CR/LF enable/done registers are still
  being RE'd, so they are configurable and default to the scanner-style layout.
- **Print-head data block `0xc0c20000–0xc0c26000`** — writes are captured in
  `head_writes` so a test can assert what the firmware sends to the nozzles.
- Print-stream bulk input is inherited from the base model (`bulk=`), to feed a
  BJRaster3 stream to a future print receiver.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'bootloader' / 'tests'))
from model import checked            # noqa: E402
from model_scanner import ScannerModel  # noqa: E402

HEAD_LO, HEAD_HI = 0xc0c20000, 0xc0c26000     # print-head data subsystem
MOTOR_LO, MOTOR_HI = 0xc0c50000, 0xc0c58000   # CR / LF / scanner mechanism family


class PrinterModel(ScannerModel):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.head_writes = []      # (addr, value) to the head-data block
        self.motor_writes = []     # (addr, value) to the motor family
        # name -> dict(enable, start, stop, done, bit, ticks, running, polls)
        self.motors = {
            'scanner_cr': dict(enable=0xc0c56000, start=0x102, stop=0,
                               done=0xc0c56104, bit=0x1, ticks=16, running=False, polls=0),
            # CR/LF print motors: block known (0xc0c51xxx–0xc0c53xxx), exact enable/done
            # registers TODO — placeholders kept inert until the driver+RE pin them down.
            'print_cr': dict(enable=None, start=0, stop=0, done=None, bit=0x1, ticks=16, running=False, polls=0),
            'print_lf': dict(enable=None, start=0, stop=0, done=None, bit=0x1, ticks=16, running=False, polls=0),
        }

    @checked
    def write(self, cpu, offset, size, value, user):
        r = super().write(cpu, offset, size, value, user)
        addr = 0xc0000000 + offset
        if HEAD_LO <= addr < HEAD_HI:
            self.head_writes.append((addr, value))
        elif MOTOR_LO <= addr < MOTOR_HI:
            self.motor_writes.append((addr, value))
            for m in self.motors.values():
                if addr == m['enable']:
                    if value == m['start']:
                        m['running'] = True; m['polls'] = 0
                    elif value == m['stop']:
                        m['running'] = False
        return r

    @checked
    def read(self, cpu, offset, size, user):
        addr = 0xc0000000 + offset
        for m in self.motors.values():
            if m['done'] is not None and addr == m['done'] and m['running']:
                m['polls'] += 1
                if m['polls'] >= m['ticks']:
                    self.regs[addr] = self.regs.get(addr, 0) | m['bit']   # raise completion
                    m['running'] = False
        return super().read(cpu, offset, size, user)

    def complete_motor(self, name):
        """Force a modeled motor to report completion (driver development / tests)."""
        m = self.motors[name]
        if m['done'] is not None:
            self.regs[m['done']] = self.regs.get(m['done'], 0) | m['bit']
        m['running'] = False
