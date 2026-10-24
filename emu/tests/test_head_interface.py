"""Empirical cartridge configuration contract; no ARM or hardware required."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock.head_interface import (HeadInterface, HEAD_CONFIG, FUSE_CONFIG,
    PROFILE_CONFIG, CTL_ACQ, CTL_COLD, CTL_RES, BANK_CFG, BANK_ROUTE,
    ENABLE, SELECT, PAIR, TRIGGER, SENSE, selector)


class HeadInterfaceTests(unittest.TestCase):
    def model(self):
        h = HeadInterface({}, fuse={0: 0x64f12b7f, 1: 0x03efa737853f})
        for a, v in {CTL_ACQ: 1, CTL_COLD: 1, CTL_RES: 1,
                     BANK_CFG: 0x2e5, BANK_ROUTE: 0, ENABLE: 1, PAIR: 0}.items():
            h.write(a, 4, v)
        return h

    def configure(self, h):
        for a, v in HEAD_CONFIG.items():
            h.write(a, 4, 0xfffffffc if a == 0xc0c291d0 else v)

    def sample(self, h, head=0, bit=7):
        h.write(SELECT, 4, selector(head, bit))
        return h.write(TRIGGER, 4, 1)

    def test_controls_and_failed_subset_cannot_return_data(self):
        h = self.model()
        self.assertEqual(self.sample(h)['blocked_by'], ['head_configuration'])
        for a, v in {**FUSE_CONFIG, **PROFILE_CONFIG}.items():
            h.write(a, 4, v)
        self.assertEqual(self.sample(h)['blocked_by'], ['head_configuration'])
        self.assertEqual(h.read(SENSE), 0)
        self.configure(h)
        self.assertEqual(self.sample(h)['blocked_by'], [])
        self.assertEqual(h.read(SENSE), 1)  # captured black bit 7 is zero

    def test_all_captured_bits_and_observed_readbacks(self):
        h = self.model()
        self.configure(h)
        self.assertEqual(h.read(0xc0c291d0), 0x3fc)
        for head, count in ((0, 32), (1, 48)):
            word = 0
            for bit in range(count):
                event = self.sample(h, head, bit)
                self.assertEqual(event['blocked_by'], [])
                word |= (1 - h.read(SENSE)) << bit
                self.assertEqual(h.read(TRIGGER), 0)
            self.assertEqual(word, h.fuse[head])

    def test_retained_config_and_explicit_cold_reset(self):
        h = self.model()
        self.configure(h)
        for a in (CTL_ACQ, CTL_COLD, CTL_RES):
            h.write(a, 4, 0)
        self.assertFalse(h.missing_config())
        self.assertIn('interface_controls', self.sample(h)['blocked_by'])
        for a in (CTL_ACQ, CTL_COLD, CTL_RES):
            h.write(a, 4, 1)
        retained = HeadInterface(h.regs, h.fuse, reset_defaults=False)
        self.assertEqual(self.sample(retained)['blocked_by'], [])
        cold = self.model()
        self.assertEqual(cold.read(0xc0c29130), 0x70707)
        self.assertIn('head_configuration', self.sample(cold)['blocked_by'])

    def test_invalid_transaction_and_injected_fault_block_response(self):
        for address, value, reason in ((PAIR, 1, 'nonzero_pair'),
                (ENABLE, 0, 'transaction_mode'), (BANK_ROUTE, 1, 'interface_controls')):
            with self.subTest(reason=reason):
                h = self.model()
                self.configure(h)
                h.write(address, 4, value)
                self.assertIn(reason, self.sample(h)['blocked_by'])
                self.assertEqual(h.read(SENSE), 0)
        h = self.model()
        self.configure(h)
        h.response_enabled = False
        self.assertEqual(self.sample(h)['blocked_by'], ['injected_response_fault'])
        h.response_enabled = True
        h.heads_connected = (False, True)
        self.assertEqual(self.sample(h)['blocked_by'], ['disconnected_head'])


if __name__ == '__main__':
    unittest.main()
