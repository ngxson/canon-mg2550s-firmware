#!/usr/bin/env python3
"""Offline evidence probe; requires the historical USB updater harness.

This is a companion to STOCK_PANEL_UPDATE.md, not a printer host utility.
Run in canonemu with --harness pointing to fw_dump/gpt in the outer workspace.
No physical USB or serial devices are accessed. No firmware function is replaced.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def model_class(harness):
    """Return the cold-reset model, shared by the full-image evidence run."""
    sys.path.insert(0, str(Path(harness).resolve()))
    from emulate_usb_controller import USBUpdater
    from emulate_usb_update import FW, MODE_OBJECT, STOP
    from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_LR

    class ColdPanel(USBUpdater):
        def __init__(self, samples):
            self.samples, self.panel_reads = samples, 0
            self.early, self.counter = True, 0
            self.indicator_pcs = []
            super().__init__(latch=0)
            self.eeprom[9] = 0

        def code(self, cpu, address, size, user):
            # Parent treats 0x18c as a post-update recovery landing. During a
            # cold boot it is also an ordinary instruction before wrapper entry.
            if address == 0x18c and self.early:
                return
            if address == 0x1838:
                self.early = False
            super().code(cpu, address, size, user)

        def read(self, cpu, offset, size, user):
            address = 0xc0000000 + offset
            if address == 0xc0c8d028:
                i = self.panel_reads
                self.panel_reads += 1
                # Live idle input on this board was 08; boot checks mask it out.
                return 8 | (self.samples[i] if i < len(self.samples) else 0)
            if self.early:
                if address == 0xc0aa0014:
                    return 2  # early SPI command completion
                if address == 0xc0100000:
                    return self.registers.get(address, 0) | 0x80008000
                if address == 0xc0c82404:
                    return 1  # bus/DRAM ready assumption
                if address == 0xc0c82008:
                    self.counter += 0x100000
                    return self.counter  # synthetic delay counter
            return super().read(cpu, offset, size, user)

        def write(self, cpu, offset, size, value, user):
            address = 0xc0000000 + offset
            if self.early and address == 0xc0c82008:
                self.counter = value
            if (self.early and address == 0xc0aa0054 and value == 1
                    and self.registers.get(0xc0aa0040) == 1):
                # Cold boot's NOR status-register write enables quad reads;
                # it is not an array program job in the updater's SPI model.
                self.registers[address] = value
                return
            if address == 0xc0c8d140 and value == 0x20:
                self.indicator_pcs.append(hex(cpu.reg_read(UC_ARM_REG_PC)))
            super().write(cpu, offset, size, value, user)

        def run_cold(self, budget=12000000):
            self.uc.reg_write(UC_ARM_REG_LR, STOP)
            self.uc.emu_start(0, STOP, count=budget)

    return ColdPanel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    ColdPanel = model_class(args.harness)
    from emulate_usb_update import FW, MODE_OBJECT

    cases = (
        ('cancel_held_at_reset_then_release', [0x20]*128 + [0]*4, 3),
        ('cancel_then_power_press_release_then_cancel_release',
         [0x20]*128 + [0x60]*16 + [0x20]*16 + [0]*4, 4),
        ('both_held_at_initial_check', [0x60]*128 + [0]*4, 1),
        ('power_only_at_initial_check', [0x40]*128 + [0]*4, 1),
        ('no_buttons', [], 1),
    )
    results = []
    for name, samples, expected in cases:
        emu = ColdPanel(samples)
        emu.run_cold()
        report = emu.report()
        mode = emu.word(MODE_OBJECT + 12)
        assert mode == expected, (name, mode, report['stop_pc'])
        assert emu.stopped_at == 0x1ac, (name, report['stop_pc'])
        if expected != 1:
            assert emu.last_status == 'C0' and emu.final_status_acknowledged
            assert bytes(emu.uc.mem_read(0x10000, len(emu.payload))) == emu.payload
            assert report['updater_instructions_and_constants_unchanged']
            assert '0x20001e10' in emu.indicator_pcs
        else:
            assert not emu.erases and not emu.programs and not emu.eeprom_writes
            assert bytes(emu.uc.mem_read(0, len(FW))) == FW
        results.append(dict(case=name, mode=mode, stop_pc=report['stop_pc'],
                            panel_reads=emu.panel_reads,
                            channel0_indicator_pcs=emu.indicator_pcs,
                            status=emu.last_status, erase_jobs=len(emu.erases),
                            program_jobs=len(emu.programs),
                            final_marker=hex(emu.eeprom[9]),
                            instructions=emu.instructions))
    result = dict(firmware_sha256=hashlib.sha256(FW).hexdigest(),
                  initial_marker='00', initial_latch=0, baseline_input='08',
                  start_pc='0x0', assertions_passed=True, cases=results)
    output = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.write_text(output)
    print(output, end='')


if __name__ == '__main__':
    main()
