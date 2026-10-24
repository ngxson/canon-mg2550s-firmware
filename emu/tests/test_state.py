"""Regression for stable architectural snapshots across Unicorn context saves."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock.startup import Startup, state_digest
from unicorn.arm_const import UC_ARM_REG_CPSR, UC_ARM_REG_SP


class StateDigestTests(unittest.TestCase):
    def test_repeatable_and_preserves_live_cpu(self):
        emu = Startup()
        before = (emu.uc.reg_read(UC_ARM_REG_CPSR), emu.uc.reg_read(UC_ARM_REG_SP))
        first = state_digest(emu)
        self.assertEqual(first, state_digest(emu))
        self.assertEqual(before, (emu.uc.reg_read(UC_ARM_REG_CPSR), emu.uc.reg_read(UC_ARM_REG_SP)))

    def test_detects_memory_and_banked_register_changes(self):
        emu = Startup()
        initial = state_digest(emu)
        emu.uc.mem_write(0x18000000, b'\x01')
        self.assertNotEqual(initial, state_digest(emu))
        emu.uc.mem_write(0x18000000, b'\0')
        self.assertEqual(initial, state_digest(emu))
        cpsr = emu.uc.reg_read(UC_ARM_REG_CPSR)
        emu.uc.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x1f) | 0x12)
        emu.uc.reg_write(UC_ARM_REG_SP, 0x1234)
        emu.uc.reg_write(UC_ARM_REG_CPSR, cpsr)
        self.assertNotEqual(initial, state_digest(emu))


if __name__ == '__main__':
    unittest.main()
