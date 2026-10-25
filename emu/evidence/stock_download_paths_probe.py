#!/usr/bin/env python3
"""Offline stock download-entry experiments. Never accesses a physical device.

The message experiment calls stock's real message producer from an idle-task
context. It is an explicit input fixture, NOT a USB command delivery test.
All flash instructions remain unchanged. Peripheral assumptions are inherited
from stock.boot.BootTrace and are not evidence of physical entry.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock.boot import BootTrace
from stock.scheduler import IDLE
from unicorn import UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ
from unicorn.arm_const import (
    UC_ARM_REG_PC, UC_ARM_REG_LR, UC_ARM_REG_SP, UC_ARM_REG_R0,
    UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
)

REGS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
POINTS = {0x126b0: 'application request producer',
          0x114eb0: 'power request', 0x114618: 'copy power parameters',
          0x113ed6: 'reset decision', 0x154146: 'latch1 reset',
          0x154162: 'latch2 reset', 0x1818: 'write latch1',
          0x1810: 'write latch2', 0x110: 'warm reset',
          0x113f16: 'reset veto check', 0x126a2: 'queue application request',
          0x126a6: 'application request producer return',
          0x14a1c: 'application request state gate',
          0x13792: 'decode requested reset reason',
          0x23740: 'XML VendorCmd dispatcher'}

XML_REQUEST = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<cmd xmlns:ivec="http://www.canon.com/ns/cmd/2008/07/common/" '
    'xmlns:vcn="http://www.canon.com/ns/cmd/2008/07/canon/">'
    '<ivec:contents><ivec:operation>VendorCmd</ivec:operation>'
    '<ivec:param_set servicetype="device">'
    '<vcn:ijoperation>FRomUpMode</vcn:ijoperation>'
    '</ivec:param_set></ivec:contents></cmd>'
).encode()


class MessageProbe(BootTrace):
    def __init__(self, event, kind='message', inject_tick=200, power=False, **kw):
        super().__init__(**kw)
        self.event = event
        self.kind = kind
        self.inject_tick, self.power = inject_tick, power
        self.payload = (b'BJLSTART\n@FROMUPMODE\nBJLEND\n' if kind == 'bjl' else b'')
        if kind == 'xml': self.payload = XML_REQUEST
        self.fifo = deque(self.payload)
        self.fixture_context = None
        self.fixture_called = False
        self.fixture_returned = False
        self.audit = []
        self.baseline = set()
        self.param_writes = []
        self.message_reads = []
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self.parameters,
                         begin=0x1879048c + 0x1c, end=0x1879048c + 0x20)

    def read(self, cpu, offset, size, user):
        if offset + 0xc0000000 == 0xc0c8d028 and self.power:
            return 8 | (0x40 if 150 <= self.ticks < 190 else 0)
        if self.fixture_called and self.kind in ('bjl', 'xml'):
            if offset + 0xc0000000 == 0xc0c23020:
                return 4 if self.fifo else 0
            if offset + 0xc0000000 == 0xc0c23014:
                assert self.fifo, 'FIFO read without available byte'
                return self.fifo.popleft()
        return super().read(cpu, offset, size, user)

    def parameters(self, cpu, access, address, size, value, user):
        self.param_writes.append(dict(pc=hex(cpu.reg_read(UC_ARM_REG_PC)),
            address=hex(address), size=size, value=hex(value), tick=self.ticks))

    def message_read(self, cpu, access, address, size, value, user):
        if len(self.message_reads) < 80:
            self.message_reads.append(dict(pc=hex(cpu.reg_read(UC_ARM_REG_PC)),
                address=hex(address), value=bytes(cpu.mem_read(address,size)).hex(),
                tick=self.ticks, lr=hex(cpu.reg_read(UC_ARM_REG_LR))))

    def code(self, cpu, address, size, user):
        if address == 0x100ff000 and self.fixture_context is not None:
            self.audit.append(dict(name='fixture return', result=hex(cpu.reg_read(UC_ARM_REG_R0))))
            cpu.context_restore(self.fixture_context)
            self.fixture_context = None
            self.fixture_returned = True
            # End this emulation slice: this Unicorn version does not redirect
            # the active translation block solely through context_restore().
            self.request_irq = True
            cpu.emu_stop()
            return
        super().code(cpu, address, size, user)
        if self.fixture_called and address == 0x1fe8c:
            self.audit.append(dict(pc=hex(address), name='BJL parsed command',
                                  command=hex(int.from_bytes(cpu.mem_read(0x100f002c,2),'little'))))
        if self.fixture_called and address in POINTS:
            values = [cpu.reg_read(r) for r in REGS]
            rec = dict(pc=hex(address), name=POINTS[address],
                       registers=[hex(v) for v in values], tick=self.ticks,
                       task=self.current_task)
            if address == 0x114618:
                rec['parameters'] = bytes(cpu.mem_read(values[1], 5)).hex()
            if address == 0x14a1c:
                rec['state_object'] = hex(values[0])
                rec['state_byte_8'] = cpu.mem_read(values[0]+8,1)[0]
            if address == 0x23740:
                rec['parsed_command'] = bytes(cpu.mem_read(values[1],0x88)).hex()
            if address == 0x126a2:
                rec['message'] = bytes(cpu.mem_read(values[1],0x34)).hex()
                cpu.hook_add(UC_HOOK_MEM_READ, self.message_read,
                             begin=values[1]+0x28, end=values[1]+0x2b)
            self.audit.append(rec)
            if address in (0x154146, 0x154162):
                self.stop_reason = POINTS[address] + ' reached before execution'
                self.request_irq = False
                cpu.emu_stop()

    def inject(self):
        if not self.fixture_called and self.ticks >= self.inject_tick:
            assert self.uc.reg_read(UC_ARM_REG_PC) == IDLE
            self.fixture_called = True
            self.baseline = set(self.pcs)
            self.fixture_context = self.uc.context_save()
            self.uc.reg_write(UC_ARM_REG_R0, 0)
            self.uc.reg_write(UC_ARM_REG_R1, self.event)
            self.uc.reg_write(UC_ARM_REG_LR, 0x100ff001)
            # Real helper used by the XML handler, with its real registry lookup.
            self.uc.reg_write(UC_ARM_REG_PC, 0x23477)
            if self.kind in ('bjl','xml'):
                # Synthetic BJL component object, zero state; the actual method
                # resolves its dependencies from the stock-constructed registry.
                # Its input adapter is modeled FIFO channel 1, already routed.
                self.uc.mem_write(0x100f0000, bytes(0x100))
                self.uc.mem_write(self.uc.reg_read(UC_ARM_REG_SP), bytes(16))
                for reg, value in zip(REGS, (0x100f0000, 1, 0, 0)):
                    self.uc.reg_write(reg, value)
                self.uc.reg_write(UC_ARM_REG_PC, 0x1fbe9 if self.kind == 'bjl' else 0x2365d)
            self.request_irq = False
            return
        super().inject()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--eeprom', type=Path, required=True)
    p.add_argument('--event', type=lambda s: int(s, 0), default=0x30007)
    p.add_argument('--kind', choices=['message', 'bjl', 'xml'], default='message')
    p.add_argument('--ticks', type=int, default=400)
    p.add_argument('--inject-tick', type=int, default=200)
    p.add_argument('--power', action='store_true')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    e = MessageProbe(args.event, args.kind, args.inject_tick, args.power, eeprom=args.eeprom).boot()
    r = e.run_boot(ticks=args.ticks, budget=20000000)
    out = dict(firmware_sha256=hashlib.sha256(e.image).hexdigest(),
        fixture=('Call stock 0x23476(event) at idle tick 200; no USB delivery'
                 if args.kind == 'message' else
                 f'Call stock {args.kind} component at idle tick 200 with synthetic object and modeled input FIFO; no USB delivery'),
        payload=e.payload.decode(), input_bytes_consumed=len(e.payload)-len(e.fifo),
        inject_tick=args.inject_tick, power_pulse_ticks=[150,190] if args.power else None,
        final_request_gate=bytes(e.uc.mem_read(0x18032e58,12)).hex(),
        event=hex(args.event), fixture_returned=e.fixture_returned,
        error=e.error, stop_reason=e.stop_reason, pc=hex(e.uc.reg_read(UC_ARM_REG_PC)),
        instructions=e.instructions, ticks=e.ticks, audit=e.audit,
        power_parameters=bytes(e.uc.mem_read(0x1879048c+0x1c,5)).hex(),
        parameter_writes=e.param_writes, next_wall=e.next_wall,
        message_reads=e.message_reads,
        new_pcs=[hex(p) for p in sorted(set(e.pcs)-e.baseline)],
        flash_unchanged=bytes(e.uc.mem_read(0,len(e.image)))==e.image,
        assumptions=r['assumptions'])
    output = json.dumps(out, indent=2)+'\n'
    if args.output: args.output.write_text(output)
    print(output, end='')


if __name__ == '__main__': main()
