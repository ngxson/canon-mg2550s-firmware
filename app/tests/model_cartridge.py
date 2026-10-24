"""Cartridge fuse-ROM and head-interface model for emulation.

Models the ASIC side of the one-bit-at-a-time fuse read used by src/cartridge.c:
a selector word (0xc0c29540) names one fuse of one head, the trigger (0xc0c29544)
samples it while the block is enabled (0xc0c2954c), and GPIO 0xc0c84618 bit 0
returns the fuse state active low. The sense line only follows the fuses while
the head interface controls that stock raises are all set (0xc0c84664 resource,
0xc0c84654 cold preparation, 0xc0c84604 acquire) and the bank bits acquire
sets are in place AND the full fixed head configuration matches. The former
model incorrectly allowed reads with only the control sequence. The updated
contract reproduces the failed subset/full-configuration distinction in
app/evidence/CARTRIDGE.md. It treats the register group conservatively; the
minimum subset and electrical field meanings are not established.
The connection-response inputs 0xc0c84638/48 mirror the complement of the
0xc0c85e04/14 outputs while the resource is held, matching the stock
expectation 1,1 -> 0,0 -> 1,1. Fuse contents are synthetic test words; this is
not an electrical model.

The model also records every write to the fuse-cut GPIO 0xc0c84624 and every
trigger with a non-zero pair word (0xc0c29550), so tests can prove the firmware
never enters the programming path, and keeps an ordered log of head-interface
control writes.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'bootloader' / 'tests'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from model import checked            # noqa: E402
from model_printer import PrinterModel  # noqa: E402

# One empirical contract for the stock harness and replacement-firmware tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'emu'))
from stock.head_interface import (HeadInterface, ENABLE, SELECT, PAIR, TRIGGER,
    SENSE, FUSECUT, CTL_ACQ, CTL_COLD, CTL_RES, BANK_ROUTE, BANK_CFG,
    CONN_IN, HEAD_CONTROLS, HEAD_CONFIG, PROFILE_CONFIG, SELECTOR_MAP, selector)


class CartridgeModel(PrinterModel):
    def __init__(self, *a, fuse=None, powered=True, heads_connected=(True, True), **k):
        super().__init__(*a, **k)
        self.fuse = {0: 0, 1: 0} if fuse is None else dict(fuse)       # per-head fuse words, bit n = fuse n
        self.powered = powered                 # legacy name: injected response fault, not voltage
        self.heads_connected = heads_connected  # per head: connection loop answers
        self.fuse_triggers = []                # (head, bit) per sampled trigger
        self.fusecut_writes = []               # must stay empty
        self.bad_triggers = []                 # trigger while disabled / pair != 0
        self.control_writes = []               # (addr, value) for head-interface controls
        self.unpowered_triggers = 0            # samples taken while the interface was not ready
        self.regs[BANK_CFG] = 0x2e5            # bootloader board init values
        self.head = HeadInterface(self.regs, self.fuse, response_enabled=powered,
                                  heads_connected=heads_connected)
        self.unconfigured_triggers = 0

    def interface_ready(self):
        return self.head.controls_match() and not self.head.missing_config()

    @checked
    def write(self, cpu, offset, size, value, user):
        r = super().write(cpu, offset, size, value, user)
        addr = 0xc0000000 + offset
        if addr == FUSECUT:
            self.fusecut_writes.append(value)
        if addr in HEAD_CONTROLS and self.stage == 'application':
            self.control_writes.append((addr, value))
        self.head.response_enabled = self.powered
        event = (self.head.write(addr, size, value)
                 if addr in HEAD_CONFIG or addr in PROFILE_CONFIG or addr == TRIGGER
                 else None)
        if event and (event['mode'] != 1 or event['pair'] or
                      (event['selector'] and event['selected'] is None)):
            self.bad_triggers.append((event['selector'], event['pair'], event['mode']))
        if event and event['selector']:
            if event['selected']:
                self.fuse_triggers.append(event['selected'])
            if 'interface_controls' in event['blocked_by'] or 'injected_response_fault' in event['blocked_by']:
                self.unpowered_triggers += 1
            if 'head_configuration' in event['blocked_by']:
                self.unconfigured_triggers += 1
        return r

    @checked
    def read(self, cpu, offset, size, user):
        addr = 0xc0000000 + offset
        if addr in CONN_IN or addr in (SENSE, TRIGGER, 0xc0c291d0):
            return self.head.read(addr)
        if addr == 0xc0c90010:                       # ADC block control: ready bit 6 once initialised with 0x80
            v = self.regs.get(addr, 0)
            return v | 0x40 if v & 0x80 else v
        return super().read(cpu, offset, size, user)
