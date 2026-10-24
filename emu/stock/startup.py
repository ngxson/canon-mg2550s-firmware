"""Execute C runtime and application startup with explicit synthetic MMIO.

This is a firmware experiment, not a model of an electrically unplugged cable.
Starts after bootloader/CPU/DRAM setup; executes real scatter loading, constructors,
kernel code, and SVC handlers. No firmware bytes or functions are patched/skipped.
"""
from collections import Counter, deque
import hashlib
import struct

from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_INTR
from unicorn.arm_const import (
    UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_SP, UC_ARM_REG_LR,
    UC_ARM_REG_PC, UC_CPU_ARM_946,
)
from unicorn import arm_const

from .inputs import DEFAULT_FIRMWARE, load_firmware
PERI = 0xc0000000
MARKERS = {
    0x11960: 'C runtime', 0x14f8b8: 'static constructors',
    0x1096aa: 'application main', 0x104318: 'platform initialization',
    0x116c5c: 'earlier peripheral handshake', 0xef868: 'scanner probe',
    0xfa342: 'scanner identification', 0x1168e6: 'scanner returned to caller',
    0x1096ca: 'platform initialization returned',
    0x104d50: 'scheduler startup', 0x11bb08: 'root task',
    0x135768: 'panel component startup', 0x12c782: 'panel output routine',
}


class Startup:
    def __init__(self, scanner_complete=True, response=0x40, handshake=True,
                 stall_transfer=1, *, firmware=DEFAULT_FIRMWARE):
        self.firmware_path, self.image = load_firmware(firmware)
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.uc.ctl_set_cpu_model(UC_CPU_ARM_946)
        for address, size in ((0, 0x200000), (0x10000000, 0x100000),
                              (0x18000000, 0x800000), (0xe0000000, 0x10000)):
            self.uc.mem_map(address, size)
        self.uc.mem_write(0, self.image)
        # Stack state established by 0x11008..0x11064, before C runtime entry.
        for cpsr, sp in ((0xd3, 0xe0000efc), (0xd1, 0x187dbffc),
                         (0x92, 0x187dbefc), (0x10, 0x187dbdfc)):
            self.uc.reg_write(UC_ARM_REG_CPSR, cpsr)
            self.uc.reg_write(UC_ARM_REG_SP, sp)
        self.uc.reg_write(UC_ARM_REG_PC, 0x11960)
        self.scanner_complete = scanner_complete
        self.stall_transfer = stall_transfer
        self.response = response
        self.handshake = handshake
        self.registers = {}
        self.reads, self.writes, self.pcs = Counter(), Counter(), Counter()
        self.uninitialized_reads = Counter()
        self.last_pcs = deque(maxlen=32)
        self.events = []
        self.markers_seen = set()
        self.serial = []
        self.pending = None
        self.panel_writes = []
        self.instructions = 0
        self.svcs = 0
        self.error = None
        self.stop_reason = None
        self.uc.mmio_map(PERI, 0x10000000, self.read, None, self.write, None)
        self.uc.hook_add(UC_HOOK_CODE, self.code)
        self.uc.hook_add(UC_HOOK_INTR, self.interrupt)

    def read(self, cpu, offset, size, user):
        address = PERI + offset
        self.reads[address] += 1
        if address == 0xc0120110:
            if self.pending is None:
                return 0
            self.pending['polls'] += 1
            enabled = self.scanner_complete or len(self.serial) != self.stall_transfer
            return int(enabled and self.pending['polls'] >= 3)
        if address == 0xc012011c:
            return self.response
        if address == 0xc0c90010:
            return (self.registers.get(address, 0) & ~0x40) | (0x40 if self.handshake else 0)
        if address == 0xc0140100:
            # Timer control: synthetic immediate expiry (0x115b20).
            return 0
        if address == 0xc0140500:
            # Timer channel 5 reset completes (wait at 0x109efc).
            return self.registers.get(address, 0) & ~0x80000000
        if address not in self.registers:
            self.uninitialized_reads[address] += 1
        return self.registers.get(address, 0) & ((1 << (size * 8)) - 1)

    def write(self, cpu, offset, size, value, user):
        address = PERI + offset
        self.writes[address] += 1
        self.registers[address] = value
        if address == 0xc0120104 and value & 1:
            self.pending = {'control': hex(self.registers.get(0xc012010c, 0)),
                            'tx': hex(self.registers.get(0xc0120118, 0)),
                            'polls': 0, 'acknowledged': False}
            self.serial.append(self.pending)
        if address == 0xc0120110 and value == 0 and self.pending is not None:
            self.pending['acknowledged'] = True
            self.pending = None
            self.registers[0xc0120104] = 0
        if 0xc0c8d140 <= address <= 0xc0c8d178 or address == 0xc0c8d014:
            self.panel_writes.append({'address': hex(address), 'value': hex(value),
                                      'pc': hex(cpu.reg_read(UC_ARM_REG_PC))})

    def interrupt(self, cpu, number, user):
        if number != 2:
            self.error = f'unexpected CPU exception {number}'
            cpu.emu_stop()
            return
        self.svcs += 1
        old_cpsr = cpu.reg_read(UC_ARM_REG_CPSR)
        next_pc = cpu.reg_read(UC_ARM_REG_PC)
        # Unicorn reports SVC after the instruction; enter the real ARM handler.
        cpu.reg_write(UC_ARM_REG_CPSR, (old_cpsr & ~0x3f) | 0x93)
        cpu.reg_write(UC_ARM_REG_SPSR, old_cpsr)
        cpu.reg_write(UC_ARM_REG_LR, next_pc)
        cpu.reg_write(UC_ARM_REG_PC, 0x8)

    def code(self, cpu, address, size, user):
        self.instructions += 1
        self.pcs[address] += 1
        self.last_pcs.append(address)
        if address in MARKERS and address not in self.markers_seen:
            self.markers_seen.add(address)
            self.events.append({'event': MARKERS[address], 'pc': hex(address),
                                'instruction': self.instructions})
        if address == 0x135768:
            self.stop_reason = 'panel component startup reached'
            cpu.emu_stop()

    def run(self, budget=5000000):
        pc = self.uc.reg_read(UC_ARM_REG_PC)
        thumb = bool(self.uc.reg_read(UC_ARM_REG_CPSR) & 0x20)
        try:
            self.uc.emu_start(pc | int(thumb), 0x200000, count=budget)
        except UcError as exc:
            self.error = str(exc)
        return self.result()

    def result(self):
        return {
            'scanner_completion_enabled': self.scanner_complete,
            'stall_transfer': self.stall_transfer,
            'scanner_response': hex(self.response), 'earlier_handshake_ready': self.handshake,
            'instructions': self.instructions, 'pc': hex(self.uc.reg_read(UC_ARM_REG_PC)),
            'error': self.error, 'stop_reason': self.stop_reason,
            'events': list(self.events), 'SVC_exceptions': self.svcs,
            'scanner_transfers': [dict(t) for t in self.serial],
            'panel_writes': list(self.panel_writes),
            'chip_type': self.uc.mem_read(0x18030ecc, 1)[0],
            'mechSW': self.uc.mem_read(0x180d8630, 1)[0],
            'hot_pcs': [[hex(a), n] for a, n in self.pcs.most_common(8)],
            'top_reads': [[hex(a), n] for a, n in self.reads.most_common(12)],
            'uninitialized_register_reads': [[hex(a), n] for a, n in self.uninitialized_reads.most_common()],
            'last_pcs': [hex(a) for a in self.last_pcs],
            'firmware_bytes_unchanged': bytes(self.uc.mem_read(0, len(self.image))) == self.image,
        }


def verify_reached_panel(result):
    assert result['error'] is None, result
    assert result['firmware_bytes_unchanged']
    assert result['mechSW'] == 1
    pcs = [int(e['pc'], 16) for e in result['events']]
    required = [0x11960, 0x14f8b8, 0x1096aa, 0x104318, 0x116c5c,
                0xef868, 0xfa342, 0x1168e6, 0x1096ca, 0x104d50,
                0x11bb08, 0x135768]
    assert pcs == required, result['events']
    assert result['stop_reason'] == 'panel component startup reached'
    assert len(result['scanner_transfers']) == 5
    assert all(t['acknowledged'] for t in result['scanner_transfers'])
    expected = {0x40: 0, 0x90: 1}.get(int(result['scanner_response'], 16) & 0xf0, 255)
    assert result['chip_type'] == expected


def verify_stalled(result, transfer):
    assert result['error'] is None, result
    assert result['firmware_bytes_unchanged']
    assert result['stop_reason'] is None
    assert result['panel_writes'] == []
    pcs = {int(e['pc'], 16) for e in result['events']}
    assert 0xfa342 in pcs
    assert not pcs & {0x1168e6, 0x1096ca, 0x104d50, 0x11bb08, 0x135768}
    assert len(result['scanner_transfers']) == transfer
    assert all(t['acknowledged'] for t in result['scanner_transfers'][:-1])
    assert not result['scanner_transfers'][-1]['acknowledged']
    assert result['scanner_transfers'][-1]['polls'] > 1000
    assert all(0xf31d2 <= int(pc, 16) < 0xf323a or
               0xf3382 <= int(pc, 16) < 0xf3396 for pc in result['last_pcs'])


def state_digest(emu):
    """Hash memory and architectural registers, not opaque Unicorn internals."""
    digest = hashlib.sha256()
    digest.update(bytes(emu.uc.mem_read(0, 0x200000)))
    for address, size in ((0x10000000, 0x100000), (0x18000000, 0x800000),
                          (0xe0000000, 0x10000)):
        digest.update(bytes(emu.uc.mem_read(address, size)))
    # Serialized context buffers include implementation data/padding that can
    # differ across saves in Unicorn 2.1.4. Read register values from a detached
    # context, including each bank, without changing the running CPU's state.
    context = emu.uc.context_save()
    cpsr = context.reg_read(UC_ARM_REG_CPSR)
    registers = [getattr(arm_const, f'UC_ARM_REG_R{i}') for i in range(13)]
    registers += [UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_CPSR]
    for mode in (0x10, 0x11, 0x12, 0x13, 0x17, 0x1b, 0x1f):
        context.reg_write(UC_ARM_REG_CPSR, (cpsr & ~0x1f) | mode)
        values = [context.reg_read(reg) for reg in registers]
        if mode not in (0x10, 0x1f):
            values.append(context.reg_read(UC_ARM_REG_SPSR))
        digest.update(struct.pack('<' + 'I' * len(values), *values))
    return digest.hexdigest()


def experiment(firmware=DEFAULT_FIRMWARE):
    cases = []
    for response in (0x40, 0x90, 0xff, 0x00):
        emu = Startup(response=response, firmware=firmware)
        result = emu.run(3000000)
        verify_reached_panel(result)
        cases.append({'name': f'completed_transfers_response_{response:02x}',
                      'result': result})
    for transfer in range(1, 6):
        emu = Startup(scanner_complete=False, stall_transfer=transfer, firmware=firmware)
        blocked = emu.run(3000000)
        verify_stalled(blocked, transfer)
        # Keep the same CPU at the blocked instruction; change one peripheral input.
        before = state_digest(emu)
        emu.scanner_complete = True
        assert state_digest(emu) == before, 'Completion input changed CPU registers or mapped memory'
        resumed = emu.run(1000000)
        verify_reached_panel(resumed)
        cases.append({'name': f'stall_then_release_transfer_{transfer}',
                      'blocked': blocked, 'resumed': resumed,
                      'state_unchanged_when_releasing_completion': True,
                      'release_state_sha256': before})
    # Negative control: another early wait can also block scanner/panel startup.
    emu = Startup(handshake=False, firmware=firmware)
    earlier = emu.run(3000000)
    assert earlier['error'] is None and earlier['firmware_bytes_unchanged']
    assert earlier['scanner_transfers'] == [] and earlier['panel_writes'] == []
    assert {int(pc, 16) for pc in earlier['last_pcs']} <= {0x116c62, 0x116c64, 0x116c66}
    cases.append({'name': 'earlier_handshake_not_ready', 'result': earlier})
    return {
        'firmware_sha256': hashlib.sha256(emu.image).hexdigest(),
        'start_pc': '0x11960', 'stop_pc': '0x135768',
        'scope': 'C runtime through first panel component startup entry; not full printer boot or physical LED illumination',
        'assumptions': [
            'Bootloader, memory/clock setup already completed; banks/stacks initialized as in 0x11008..0x11064.',
            'Unknown MMIO reads return last write or zero; every previously unwritten read is logged.',
            'Earlier handshake bit 0x40 at 0xc0c90010 is supplied unless testing that wait.',
            'Timer 0xc0140100 expires immediately; timer reset bit 31 at 0xc0140500 self-clears.',
            'Scanner transfer completes after three polls unless chosen transfer is stalled; receive byte is synthetic.',
            'SVC exception entry is modeled; real vectors/handlers execute. No timer IRQs or watchdog reset are delivered.',
            'No scanner electrical/connector/clock/ACK model; cases are not labeled plugged/unplugged.',
        ],
        'all_assertions_passed': True, 'cases': cases,
    }
