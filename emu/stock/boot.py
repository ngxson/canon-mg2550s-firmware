"""Continue stock boot after EEPROM task tracing with explicit readiness inputs.

No stock instructions, application calls, or RAM flags are patched. Readiness
models are explicit and individually selectable for negative controls. Every
motor/serial/routing/timer4 write is inherited from TaskTrace, without sampling.
"""
from collections import Counter, deque

from .tasks import TaskTrace, ranges, E, WATCH
from .scheduler import TickScheduler
from unicorn import UC_HOOK_MEM_WRITE
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_LR, UC_ARM_REG_SP,
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_CPSR,
    UC_ARM_REG_R12)

POINTS = {
    0x49aac: 'PHY wait passed', 0x49aca: 'USB core ready wait passed',
    0x49ad4: 'USB hardware init return',
    0x11bae0: 'root component init return',
    0x11bae2: 'root component phase 3 entry',
    0x11baf0: 'component phase 3 call',
    0x11bafc: 'root component phase 3 return',
    0x11bb52: 'root task exit call',
    0xf6430: 'motion facade request',
    0x107286: 'mailbox receive API', 0x106d16: 'event wait API',
    0xf5cca: 'scanner sequencer mailbox return',
    0xf5626: 'scanner entry mailbox return',
    0xef526: 'scanner master mailbox return',
    0x89780: 'scanner synchronous mailbox return',
    0xd9b88: 'linefeed completion wait',
    0xf5958: 'scanner mode selection',
    0xf5b72: 'scanner home permission result',
    0xf9f26: 'scanner init no-home return',
    0xf9d38: 'scanner init with home',
}

ASSUMPTIONS = [
    {'level': 0, 'mechanism': 'Inherited startup',
     'values': 'Post-bootloader stacks; scanner response 0x40 after 3 polls; '
               '0xc0c90010 bit 6 set; timer1 early power delays expire immediately; '
               'timer5 reset bit31 clear; 0xc0b2001c and 0xc0b20118 busy bit0 clear.',
     'evidence': 'startup.py, scheduler.py; tasks.py'},
    {'level': 0, 'mechanism': 'Inherited EEPROM/VIC/tick',
     'values': 'Caller-supplied 24C08 image FIFO; status bit0 complete, bit6 FIFO empty; '
               'source52 after one idle tick; source56 idle-boundary timer ticks; '
               'single source arbitration, group pending and enable set/clear aliases.',
     'evidence': 'tasks.py, 0x1114b2..0x11154a, 0x11188a..0x1118d6'},
    {'level': 0, 'mechanism': 'Unmodeled peripherals',
     'values': 'Last write or zero. No panel key, USB packet/enumeration, encoder '
               'movement, or motor-completion input is supplied. Modeled EEPROM '
               'writes are retained in RAM only; input files are never written.',
     'evidence': 'All MMIO reads/writes and EEPROM transactions recorded.'},
    {'level': 1, 'mechanism': 'USB PHY ready',
     'values': '0xc0c88024 reads OR 0x100',
     'evidence': '0x49aa0..0x49aa4: shift bit8 into sign, BMI exits poll.'},
    {'level': 2, 'mechanism': 'USB core ready',
     'values': '0xc0780010 reads OR 0x80000000',
     'evidence': '0x49abe..0x49ac2: negative read exits second readiness poll.'},
    {'level': 3, 'mechanism': 'Timer reset completion',
     'values': '0xc0140000 + 0x100*channel, channel0..7: read bit31 clear. '
               'Newly exercised channels2/7; inherited channels1/5 already clear.',
     'evidence': 'Generic channel map 0x109dbc..0x109df4; reset polls '
                 '0x109eb4..0x109ebc and 0x109ef8..0x109f00.'},
    {'level': 4, 'mechanism': 'Additional timer IRQs',
     'values': 'Channel1 -> source1/group0/mask0x1 (bit0); channel7 -> '
               'source96/group11/mask0x40. One IRQ per later scheduler tick '
               'while control bit0 and stock VIC enable are set. Source latch '
               'bit0x10 clears through inherited model. Coarse, not calibrated.',
     'evidence': '0xc2f98..0xc2fca; 0xdbf0c..0xdbf44; '
                 '0xe2448..0xe2492, table0x1872a0, 0xe24de..0xe254e.'},
    {'level': 4, 'mechanism': 'Timer1 control read refinement',
     'values': '0xc0140100: generic timer API reads last control with bit31 clear; '
               'early power delay callers retain inherited immediate expiry.',
     'evidence': '0x109dfe, 0x109e10, 0x109ec8 versus 0x115abe..0x115bb2.'},
]


class BootTrace(TaskTrace):
    def __init__(self, ready_level=4, **inputs):
        super().__init__(**inputs)
        self.ready_level = ready_level
        self.mmio_reads = Counter()
        self.mmio_tail = deque(maxlen=64)
        self.mmio_writes = []
        self.boot_points = []
        self.usb_baseline = None
        self.boot_pcs = Counter()
        self.boot_sizes = {}
        self.poll_counts = Counter()
        self.extra_timers = {1: 1, 7: 96}  # channel -> VIC source, verified in stock
        self.timer_last_tick = {}
        self.extra_timer_irqs = Counter()
        self.scanner_gate_writes = []
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self.scanner_gate_write,
                         begin=0x180d8630, end=0x180d863b)

    def scanner_gate_write(self, cpu, access, address, size, value, user):
        self.scanner_gate_writes.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
            'address': hex(address), 'size': size, 'value': hex(value),
            'task': self.current_task, 'tick': self.ticks})

    def read(self, cpu, offset, size, user):
        address = E.PERI + offset
        pc = cpu.reg_read(UC_ARM_REG_PC)
        value = super().read(cpu, offset, size, user)
        if self.ready_level >= 1 and address == 0xc0c88024:
            value |= 0x100
        if self.ready_level >= 2 and address == 0xc0780010:
            value |= 0x80000000
        if (self.ready_level >= 3 and 0xc0140000 <= address <= 0xc0140700
                and address & 255 == 0):
            # Same generic timer reset function as channel 5: 109ef8..109f00.
            value &= ~0x80000000
        if (self.ready_level >= 4 and address == 0xc0140100
                and not 0x115a00 <= pc < 0x115c00):
            # Keep inherited early power-delay expiry, but expose the later
            # generic timer API's control state faithfully while it is armed.
            value = self.registers.get(address, 0) & ~0x80000000
        key = (pc, address, value, self.current_task)
        self.mmio_reads[key] += 1
        self.mmio_tail.append({'pc': hex(pc), 'address': hex(address),
            'value': hex(value), 'size': size, 'task': self.current_task})
        if self.usb_baseline is not None:
            self.poll_counts[(pc, address)] += 1
            if self.poll_counts[(pc, address)] == 512:
                self.next_wall = {'pc': hex(pc), 'task': self.current_task,
                    'register': hex(address), 'modeled_value': hex(value),
                    'reads_since_last_irq': 512,
                    'lr': hex(cpu.reg_read(UC_ARM_REG_LR)),
                    'cpsr': hex(cpu.reg_read(UC_ARM_REG_CPSR))}
                self.stop_reason = 'repeated MMIO read; inspect next peripheral wall'
                cpu.emu_stop()
        return value

    def write(self, cpu, offset, size, value, user):
        super().write(cpu, offset, size, value, user)
        self.mmio_writes.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
            'address': hex(E.PERI + offset), 'value': hex(value), 'size': size,
            'task': self.current_task, 'tick': self.ticks,
            'instruction': self.instructions, 'phase': self.phase})
        for channel in self.extra_timers:
            if E.PERI + offset == 0xc0140000 + channel*256:
                self.timer_last_tick[channel] = self.ticks

    def code(self, cpu, address, size, user):
        extra_restore = None
        if address == 0x1507c0:
            # Third real restore: dispatch after a task exits (RootTask -> USB).
            self.current_task = cpu.mem_read(self.word(0x150950), 1)[0]
            target = self.word(cpu.reg_read(UC_ARM_REG_R12) + 4)
            self.restores.append({'pc': hex(address), 'task': self.current_task,
                'target': hex(target), 'ticks': self.ticks, 'irqs': self.irqs,
                'phase': self.phase, 'target_executed': False})
            # Parent checks expected_restore before bookkeeping, on next code hook.
            extra_restore = target & ~1
        if address == 0x49aa0 and self.usb_baseline is None:
            self.usb_baseline = set(self.pcs)
        # Disable only EEPROM task tracing's deliberate stopping threshold for modeled PHY.
        if address == 0x49aa0 and self.ready_level >= 1:
            self.phy_polls = 0
        super().code(cpu, address, size, user)
        if extra_restore is not None:
            self.expected_restore = extra_restore
        if self.usb_baseline is not None:
            self.boot_pcs[address] += 1
            self.boot_sizes[address] = size
        if address in POINTS:
            self.boot_points.append({'pc': hex(address), 'name': POINTS[address],
                'task': self.current_task, 'tick': self.ticks,
                'r0_r3': [hex(cpu.reg_read(r)) for r in
                    (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)],
                'lr': hex(cpu.reg_read(UC_ARM_REG_LR)),
                'sp': hex(cpu.reg_read(UC_ARM_REG_SP)),
                'stack_words': [hex(self.word(cpu.reg_read(UC_ARM_REG_SP) + 4*i))
                                for i in range(16)]})
            if address in (0xf5cca, 0xf5626, 0xef526):
                message = self.word(cpu.reg_read(UC_ARM_REG_SP))
                if 0x18000000 <= message < 0x18800000:
                    self.boot_points[-1]['message'] = hex(message)
                    self.boot_points[-1]['message_words'] = [
                        hex(self.word(message + 4*i)) for i in range(20)]

    def inject(self):
        self.poll_counts.clear()
        if self.ready_level >= 4:
            for channel, source in self.extra_timers.items():
                control = self.registers.get(0xc0140000 + channel*256, 0)
                if (control & 1 and not control & 0x80000000
                        and self.ticks > self.timer_last_tick.get(channel, self.ticks)
                        and self.enabled[self.group_for(source)] & self.mask_for(source)):
                    self.source = source
                    self.timer_last_tick[channel] = self.ticks
                    self.extra_timer_irqs[source] += 1
                    TickScheduler.inject(self)
                    self.timer_pending = False  # this field is timer 5 only
                    return
        super().inject()

    def run_boot(self, ticks=2000, budget=30000000):
        result = self.run_tasks(ticks=ticks, completions=1024, budget=budget)
        new = set(self.boot_pcs) - (self.usb_baseline or set())
        result.update({'ready_level': self.ready_level,
            'boot_points': self.boot_points,
            'boot_new_pc_count': len(new),
            'boot_new_pc_ranges_end_exclusive': ranges(new, self.boot_sizes),
            'boot_hot_pcs': [[hex(p), n] for p, n in self.boot_pcs.most_common(30)],
            'mmio_read_counts': [{'pc': hex(p), 'address': hex(a), 'value': hex(v),
                'task': t, 'count': n} for (p, a, v, t), n in self.mmio_reads.most_common()],
            'mmio_read_tail': list(self.mmio_tail), 'mmio_writes': self.mmio_writes})
        result.update({'extra_timer_irqs': dict(self.extra_timer_irqs),
            'scanner_gate_writes': self.scanner_gate_writes,
            'scanner_gate_final': bytes(self.uc.mem_read(0x180d8630, 12)).hex(),
            'assumptions': [{**a, 'enabled': a['level'] <= self.ready_level}
                            for a in ASSUMPTIONS],
            'eeprom_private_byte_changes': [{'offset': hex(i), 'before': hex(a),
                'after': hex(b)} for i, (a, b) in enumerate(
                    zip(self.eeprom_original, self.eeprom)) if a != b],
            'signature_pc_hits': {hex(p): self.pcs[p] for p in
                (0xf4c56, 0xf6430, 0xf8a5c, 0xf8638, 0xf865c, 0xf9d38)},
            'requested_ticks': ticks})
        last_waits = {}
        for point in self.boot_points:
            if point['pc'] in ('0x107286', '0x106d16'):
                last_waits[point['task']] = point
        result['last_observed_wait_by_task'] = last_waits
        return result


def verify(result):
    assert result['result']['error'] is None and not result['faults']
    assert result['result']['firmware_bytes_unchanged'] and not result['flash_writes']
    assert all(result['input_files_unchanged'].values())
    assert result['tick_counter_delta'] == result['ticks']
    assert result['reader_results'] == result['dispatches']
    expected = Counter({'0x1541dc': result['ticks'], '0x11188b': result['completions']})
    handlers = {1: '0xdbf0d', 96: '0xe24df'}
    for source, count in result['extra_timer_irqs'].items():
        expected[handlers[int(source)]] += count
    assert Counter(result['dispatches']) == expected
    assert all(r['target_executed'] for r in result['context_restores'])
    assert result['vic_pending'] is None and result['vic_active'] is None
    assert not result['timer_pending']
    # Cross-check that every requested-range write was captured, in order.
    watched = [w for w in result['mmio_writes'] if any(
        lo <= int(w['address'], 16) < hi for _, lo, hi in WATCH)]
    fields = ('pc', 'address', 'value', 'size', 'instruction', 'phase', 'task')
    assert [[w[k] for k in fields] for w in watched] == [
        [w[k] for k in fields] for w in result['motor_writes']]
    assert not any(result['signature_pc_hits'].values())
    level = result['ready_level']
    points = result['boot_points']
    if level < 3:
        pc, register = {0: ('0x49aa0', '0xc0c88024'),
            1: ('0x49abe', '0xc0780010'),
            2: ('0x109efc', '0xc0140200')}[level]
        assert result['next_wall']['pc'] == pc
        assert result['next_wall']['register'] == register
    else:
        assert result['requested_ticks'] >= 1000, 'full verification needs >=1000 ticks'
        assert result['ticks'] == result['requested_ticks'] and result['next_wall'] is None
        assert result['result']['stop_reason'] == 'tick limit at idle'
        assert any(p['pc'] == '0x11bb52' and p['task'] == 1 for p in points)
        assert any(r['pc'] == '0x1507c0' and r['task'] == 61
                   and r['target'] == '0x515e9' for r in result['context_restores'])
        assert len(result['motor_writes']) == 106
        assert not any(w['address'] == '0xc0c56008' and w['value'] != '0x7d0'
                       or w['address'] == '0xc0c56000' and w['value'] == '0x102'
                       for w in result['motor_writes'])
        assert any(p['pc'] == '0xf5958' and p['r0_r3'][0] == '0x0' for p in points)
        assert any(p['pc'] == '0xf9f26' and p['task'] == 36 for p in points)
        assert result['scanner_gate_final'][2:4] == '00'
        if level == 4:
            assert set(map(int, result['extra_timer_irqs'])) == {1, 96}
            assert any(p['pc'] == '0x106bf0' and p['lr'] == '0xda6ab'
                       and p['r0_r3'][:2] == ['0x1d', '0x200'] for p in result['points'])
    assert all(t['irq_delivered'] and (t['operation'] == 'write' or
               t['rx_consumed'] == t['count']) for t in result['transfers'])
    result['assertions_passed'] = True
