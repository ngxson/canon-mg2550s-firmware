#!/usr/bin/env python3
"""Sensitivity check for stock's reset-time key admission, not a timing model.

Delay the visibility of an already-held Cancel in the synthetic panel register.
The delay unit is MMIO reads, not microseconds or hardware scan frames. This
tests the prior model assumption; it does not establish the physical failure.
"""
import argparse
import hashlib
import json
from pathlib import Path

from stock_panel_update_probe import model_class


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    ColdPanel = model_class(args.harness)
    from emulate_usb_update import FW, MODE_OBJECT
    from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_R3

    class Probe(ColdPanel):
        def __init__(self, delay):
            self.admission_samples = []
            self.accepted_iterations = None
            self.updater_entered = False
            super().__init__([0]*delay + [0x20]*128 + [0]*4)

        def read(self, cpu, offset, size, user):
            value = super().read(cpu, offset, size, user)
            if (0xc0000000 + offset == 0xc0c8d028 and
                    0x20001db4 <= cpu.reg_read(UC_ARM_REG_PC) <= 0x20001dc0):
                self.admission_samples.append(value)
            return value

        def code(self, cpu, address, size, user):
            if address == 0x20001dec:
                self.accepted_iterations = cpu.reg_read(UC_ARM_REG_R3)
            if address == 0x2000351c:
                self.updater_entered = True
            super().code(cpu, address, size, user)

    cases = []
    for delay in (0, 1, 2, 4, 8, 64):
        emu = Probe(delay)
        emu.run_cold()
        expected = 3 if delay < 2 else 1
        mode = emu.word(MODE_OBJECT + 12)
        assert mode == expected and emu.stopped_at == 0x1ac
        assert len(emu.admission_samples) == 64
        assert emu.updater_entered == (expected == 3)
        assert bool(emu.accepted_iterations == 16) == emu.updater_entered
        if expected == 3:
            assert emu.last_status == 'C0' and emu.final_status_acknowledged
        else:
            assert not emu.erases and not emu.programs and not emu.eeprom_writes
            assert bytes(emu.uc.mem_read(0, len(FW))) == FW
            assert emu.panel_reads == 64  # no retry after failed admission
        cases.append(dict(initial_unavailable_reads=delay,
                          first_eight_samples=[hex(v) for v in emu.admission_samples[:8]],
                          accepted_iterations=emu.accepted_iterations,
                          mode=mode, updater_entered=emu.updater_entered,
                          status=emu.last_status, panel_reads=emu.panel_reads))
    result = dict(firmware_sha256=hashlib.sha256(FW).hexdigest(),
                  scope='Synthetic panel-readiness sensitivity, not physical verification',
                  units='MMIO reads, not time', assertions_passed=True, cases=cases)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
