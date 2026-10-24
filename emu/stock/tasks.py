"""EEPROM transactions and task tracing on the stock scheduler.

The caller supplies a 1024-byte 24C08 image. Stock EEPROM writes change only
an in-memory copy; no input file or physical device is written.
"""
from collections import Counter, deque
import hashlib
import struct

from .scheduler import TickScheduler, IDLE, ISR_TABLE, E
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_CPSR, UC_ARM_REG_SP,
                               UC_ARM_REG_LR, UC_ARM_REG_R0, UC_ARM_REG_R1,
                               UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R12)

from .inputs import load_eeprom
I2C = 0xc0b20100
WATCH = [('motor', 0xc0c56000, 0xc0c57404),
         ('serial', 0xc0c50200, 0xc0c50224),
         ('routing', 0xc0c41000, 0xc0c41504),
         ('timer4', 0xc0140400, 0xc0140500)]
POINTS = {0x11bb08: 'root task entry', 0x11146e: 'EEPROM read entry',
          0x1114c8: 'event 51 wait', 0x1114cc: 'event wait returned',
          0x11150c: 'read success branch', 0x11154c: 'read timeout/failure',
          0x111548: 'whole read successful', 0x1110ca: 'read wrapper returned',
          0x106d16: 'event wait API', 0x106bf0: 'event set API',
          0x1085be: 'task delay API', 0xf4c56: 'home seek entry',
          0xf8a5c: 'motor arm entry', 0xf8638: 'servo drive entry',
          0x11ba8e: 'component phase 1', 0x11baa8: 'component phase 1b',
          0x11babe: 'component phase 2', 0x11bad4: 'component phase 2b',
          0x49a60: 'USB hardware initialization'}


def ranges(pcs, sizes):
    result = []
    for pc in sorted(pcs):
        end = pc + sizes[pc]
        if result and result[-1][1] == pc:
            result[-1][1] = end
        else:
            result.append([pc, end])
    return [[hex(a), hex(b)] for a, b in result]


class TaskTrace(TickScheduler):
    def __init__(self, completion=True, *, eeprom, **inputs):
        self.eeprom_path, self.eeprom_original = load_eeprom(eeprom)
        super().__init__(**inputs)
        self.eeprom = bytearray(self.eeprom_original)
        self.completion = completion
        self.tx = []
        self.rx = deque()
        self.transfer_pending = False
        self.transfers = []
        self.motor_writes = []
        self.engine_writes = []
        self.points = []
        self.boot_switches = []
        self.restores = []
        self.expected_restore = None
        self.current_task = None
        self.task_pcs = Counter()
        self.task_sizes = {}
        self.edges = Counter()
        self.previous_pc = None
        self.previous_size = 0
        self.ticks = 0
        self.completions = 0
        self.max_ticks = 400
        self.max_completions = 256
        self.phy_polls = 0
        self.next_wall = None

    def read(self, cpu, offset, size, user):
        address = E.PERI + offset
        if address == I2C:
            self.reads[address] += 1
            if self.phase != 'boot':
                self.post_reads[address] += 1
            # Bit 0 completion; bit 6 RX FIFO empty (111764..111768).
            return self.registers.get(address, 0) | (0 if self.rx else 0x40)
        if address == I2C + 0x1c:
            self.reads[address] += 1
            if self.phase != 'boot':
                self.post_reads[address] += 1
            assert self.rx, 'stock read an empty modeled I2C FIFO'
            value = self.rx.popleft()
            self.transfers[-1]['rx_consumed'] += 1
            return value
        return super().read(cpu, offset, size, user)

    def write(self, cpu, offset, size, value, user):
        address = E.PERI + offset
        pc = cpu.reg_read(UC_ARM_REG_PC)
        super().write(cpu, offset, size, value, user)
        record = {'pc': hex(pc), 'address': hex(address), 'value': hex(value),
                  'size': size, 'instruction': self.instructions,
                  'phase': self.phase, 'task': self.current_task}
        for name, lo, hi in WATCH:
            if lo <= address < hi:
                self.motor_writes.append({**record, 'range': name})
        if 0xc0b20000 <= address < 0xc0b20200:
            self.engine_writes.append(record)
        if address == 0xc0b2001c and value & 1:
            self.tx = []
            self.rx.clear()
            self.transfer_pending = False
        if address == I2C + 0x20:
            self.tx.append(value & 255)
        if address == I2C + 0x18 and value & 1:
            mode = self.registers.get(I2C + 0x14, 0)
            if mode not in (0, 1):
                return  # inherited polled bus reset/recovery micro-commands
            slave = self.registers[I2C + 0xc]
            assert 0x50 <= slave <= 0x53, ('new I2C slave', hex(slave))
            assert self.tx and not self.transfer_pending
            start = 256 * (slave - 0x50) + self.tx[0]
            count = self.registers[I2C + 0x10] + 1 if mode == 1 else len(self.tx) - 1
            assert 0 <= start < start + count <= len(self.eeprom)
            if mode == 1:
                data = bytes(self.eeprom[start:start + count])
                self.rx = deque(data)
            else:
                data = bytes(self.tx[1:])
                self.eeprom[start:start + count] = data
            self.transfers.append({**record, 'operation': 'read' if mode else 'write',
                                   'slave': hex(slave), 'offset': hex(start),
                                   'count': count, 'data': data.hex(),
                                   'rx_consumed': 0, 'irq_delivered': False,
                                   'launch_tick': self.ticks})
            self.transfer_pending = True

    def code(self, cpu, address, size, user):
        if self.expected_restore is not None:
            assert address == self.expected_restore
            self.restores[-1]['target_executed'] = True
            self.expected_restore = None
        if address in (0x150694, 0x150520):
            self.current_task = cpu.mem_read(self.word(0x150950), 1)[0]
            target = self.word(cpu.reg_read(UC_ARM_REG_R12) + 4)
            self.restores.append({'pc': hex(address), 'task': self.current_task,
                'target': hex(target), 'ticks': self.ticks, 'irqs': self.irqs,
                'phase': self.phase, 'target_executed': False})
            self.expected_restore = target & ~1
            if self.phase == 'boot':
                self.boot_switches.append({'task': self.current_task,
                    'resume_pc': hex(self.word(cpu.reg_read(UC_ARM_REG_R12) + 4))})
        if self.current_task == 1:
            self.task_pcs[address] += 1
            self.task_sizes[address] = size
        if self.phase != 'boot' and self.previous_pc is not None:
            if address != self.previous_pc + self.previous_size:
                self.edges[(self.previous_pc, address, self.current_task)] += 1
        self.previous_pc, self.previous_size = address, size
        if address in POINTS:
            regs = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1,
                    UC_ARM_REG_R2, UC_ARM_REG_R3)]
            point = {'name': POINTS[address], 'pc': hex(address),
                     'task': self.current_task, 'tick': self.ticks,
                     'phase': self.phase, 'r0_r3': [hex(v) for v in regs],
                     'lr': hex(cpu.reg_read(UC_ARM_REG_LR)),
                     'sp': hex(cpu.reg_read(UC_ARM_REG_SP))}
            if address in (0x1114c8, 0x1114cc):
                point['stack_words'] = [hex(v) for v in struct.unpack('<11I',
                    cpu.mem_read(cpu.reg_read(UC_ARM_REG_SP), 44))]
            self.points.append(point)
        super().code(cpu, address, size, user)
        if address == 0x49aa0:
            self.phy_polls += 1
            if self.phy_polls == 65:
                self.next_wall = {'pc': hex(address), 'task': self.current_task,
                    'register': '0xc0c88024', 'required_mask': '0x100',
                    'modeled_value': hex(self.registers.get(0xc0c88024, 0)),
                    'completed_polls': self.phy_polls - 1,
                    'cpsr': hex(cpu.reg_read(UC_ARM_REG_CPSR))}
                self.stop_reason = 'distinct peripheral wall: USB PHY ready bit 8'
                cpu.emu_stop()
        if address == IDLE and self.phase != 'boot' and self.request_irq:
            # Parent handles idle-flag consumption, IRQ masks and safe IRQ entry.
            next_completion = self.completion_ready()
            if ((next_completion and self.completions >= self.max_completions) or
                    (not next_completion and self.ticks >= self.max_ticks)):
                self.request_irq = False
                self.stop_reason = 'completion limit' if next_completion else 'tick limit at idle'

    def completion_ready(self):
        # Synthetic one-tick transfer latency, safely before the 200-tick timeout.
        return (self.completion and self.transfer_pending and
                self.ticks > self.transfers[-1]['launch_tick'])

    def inject(self):
        self.source = 52 if self.completion_ready() else 56
        if self.source == 52:
            assert self.enabled[6] & self.mask_for(52)
            self.registers[I2C] = 1
            self.transfer_pending = False
            self.transfers[-1]['irq_delivered'] = True
            self.transfers[-1]['completion_tick'] = self.ticks
            self.completions += 1
        else:
            self.ticks += 1
        super().inject()
        if self.source == 52:
            self.timer_pending = False  # only source 56 belongs to timer 5

    def run_tasks(self, ticks=400, completions=256, budget=2000000):
        self.max_ticks, self.max_completions = ticks, completions
        self.exercise(ticks=2**31, budget=budget)
        result = self.report()
        # scheduler uses one IRQ source, so its "tick" is the IRQ count. With two
        # sources retain the old observations but label that field accurately.
        for switch in result['task_switches']:
            switch['irq_count'] = switch.pop('tick')
        result.update({'ticks': self.ticks, 'completions': self.completions,
            'completion_enabled': self.completion,
            'eeprom_input': str(self.eeprom_path),
            'eeprom_sha256': hashlib.sha256(self.eeprom_original).hexdigest(),
            'eeprom_model_unchanged': self.eeprom == self.eeprom_original,
            'input_files_unchanged': {'flash': self.firmware_path.read_bytes() == self.image,
                                      'eeprom': self.eeprom_path.read_bytes() == self.eeprom_original},
            'transfers': self.transfers, 'motor_writes': self.motor_writes,
            'engine_writes': self.engine_writes, 'points': self.points,
            'boot_switches': self.boot_switches,
            'context_restores': self.restores, 'next_wall': self.next_wall,
            'task1_pc_ranges_end_exclusive': ranges(self.task_pcs, self.task_sizes),
            'task1_hot_pcs': [[hex(pc), n] for pc, n in self.task_pcs.most_common(30)],
            'edges': [[hex(a), hex(b), task, n] for (a, b, task), n in self.edges.items()],
            'enabled_groups_final': [hex(v) for v in self.enabled],
            'firmware_sha256': hashlib.sha256(self.image).hexdigest()})
        return result


def verify(result):
    assert result['result']['error'] is None and not result['faults']
    assert result['result']['firmware_bytes_unchanged'] and not result['flash_writes']
    assert all(result['input_files_unchanged'].values())
    assert result['tick_counter_delta'] == result['ticks']
    assert result['reader_results'] == result['dispatches']
    assert Counter(result['dispatches']) == Counter({
        '0x1541dc': result['ticks'], '0x11188b': result['completions']})
    assert all(r['target_executed'] for r in result['context_restores'])
    assert result['vic_pending'] is None and result['vic_active'] is None
    assert not result['timer_pending']
    returned = [p for p in result['points'] if p['pc'] == '0x1114cc']
    if result['completion_enabled']:
        assert result['next_wall'] and result['next_wall']['task'] == 1
        assert result['completions'] == 64 and len(returned) == 64
        assert all(p['r0_r3'][0] == '0x0' and p['stack_words'][8] == '0x1'
                   and p['task'] == 1 for p in returned)
        assert all(t['irq_delivered'] and t['operation'] == 'read' and
                   t['rx_consumed'] == t['count'] for t in result['transfers'])
        assert sum(t['count'] for t in result['transfers']) == 938
        assert sum(p['name'] == 'whole read successful' for p in result['points']) == 12
        assert not any(p['pc'] == '0x11154c' for p in result['points'])
        assert not any(w['phase'] != 'boot' for w in result['motor_writes'])
    else:
        assert result['ticks'] == 400 and result['completions'] == 0
        assert len(returned) == 2
        assert all(p['r0_r3'][0] == '0xffffffce' for p in returned)
        assert not any(p['name'] == 'read success branch' for p in result['points'])
    assert result['eeprom_model_unchanged']
    result['assertions_passed'] = True
