"""Deliver grouped VIC IRQs to the unmodified stock scheduler.

All code addresses are flash offsets (XIP at zero). This inherits Startup's
synthetic startup peripherals and polled engine completions. No stock
function, RAM flag, or flash instruction is patched. IRQ entry alone changes
CPU state; the real vector, reader, ISR, SVC and scheduler execute.

This is a single-pending-source, event-driven model, not a cycle-accurate VIC.
Group +0 is pending; +8/+12 are modeled as enable-set/enable-clear aliases.
The low 16 status bits are an acknowledge token; this harness uses the global
source number as that token. Stock actually selects the handler from group +0.
The source-56 CLI run honors the enables stock programmed. The Python API's
force option can bypass masks for IRQ-source discovery experiments.
"""
from collections import Counter
import struct

from . import startup as E
from unicorn import UC_HOOK_MEM_WRITE, UC_HOOK_MEM_INVALID
from unicorn.arm_const import (
    UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_PC, UC_ARM_REG_LR,
    UC_ARM_REG_R0, UC_ARM_REG_R5, UC_ARM_REG_R12,
)

IDLE = 0x104da2
ISR_TABLE = 0x1811bc30
GROUP_TABLE = 0x1903d8
MASK_TABLE = 0x190418
VIC = 0xc0130000
STATUS = VIC + 0x1080
ACK = VIC + 0x1088
TIMER = 0xc0140500
MARKERS = {
    0x11810: 'IRQ vector handler (ARM)',
    0x153ff0: 'grouped VIC reader',
    0x1541dc: 'timer tick ISR (ARM)',
    0x109460: 'scheduler tick notification',
    0x104da8: 'idle loop exited on tick flag',
    0x104c4a: 'scheduler timeout processing',
    0x1046b2: 'scheduler work at 0x1046b2',
    0x104604: 'scheduler work at 0x104604',
    0x110c96: 'resumed task transfer-return path',
    0x1110ca: 'task transfer wrapper returned',
}


class TickScheduler(E.Startup):
    def __init__(self, **inputs):
        super().__init__(**inputs)
        self.groups = [struct.unpack_from('<HH', self.image, GROUP_TABLE + 4*g)
                       for g in range(15)]
        self.enabled = [0] * 15
        self.vic_pending = None
        self.vic_active = None
        self.timer_pending = False
        self.phase = 'boot'
        self.idle_visits = 0
        self.idle_flag = None
        self.baseline_pcs = set()
        self.post_pcs = Counter()
        self.pc_sizes = {}
        self.post_reads = Counter()
        self.trace = []
        self.irqs = 0
        self.idle_exits = 0
        self.flag_writes = []
        self.reader_results = []
        self.dispatches = []
        self.task_switches = []
        self.expected_task_pc = None
        self.faults = []
        self.flash_writes = []
        self.request_irq = False
        self.source = None
        self.tick_limit = 1
        self.force = False
        self.spurious = False
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self.memory_write,
                         begin=0x1811c629, end=0x1811c629)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self.flash_write,
                         begin=0, end=len(self.image) - 1)
        self.uc.hook_add(UC_HOOK_MEM_INVALID, self.invalid_memory)

    def word(self, address):
        return struct.unpack('<I', self.uc.mem_read(address, 4))[0]

    def group_for(self, source):
        return next(g for g, (start, count) in enumerate(self.groups)
                    if start <= source < start + count)

    def mask_for(self, source):
        return struct.unpack_from('<I', self.image, MASK_TABLE + 4*source)[0]

    def source_register(self, source):
        group = self.group_for(source)
        start, _ = self.groups[group]
        slot = source - start if group else source - 1
        # Stock's 0x109cac..0x109cf0 computes this address, NOT 0x1100+4*source.
        return VIC + 0x3000 + 0x80*group + 4*slot

    def log(self, event, **data):
        self.trace.append({'event': event, 'pc': hex(self.uc.reg_read(UC_ARM_REG_PC)),
                           'instruction': self.instructions, **data})

    def read(self, cpu, offset, size, user):
        address = E.PERI + offset
        if self.phase != 'boot':
            self.post_reads[address] += 1
        if address in (0xc0b2001c, 0xc0b20118):
            self.reads[address] += 1
            return self.registers.get(address, 0) & ~1
        if address == STATUS:
            self.reads[address] += 1
            return (self.group_for(self.vic_pending) << 16 | self.vic_pending
                    if self.vic_pending is not None else 0)
        if VIC <= address < VIC + 15*16:
            group, reg = divmod(address - VIC, 16)
            self.reads[address] += 1
            if reg == 0:
                # The reader ACKs STATUS before scanning this pending bitmap.
                active = self.vic_active
                return (self.mask_for(active) if active is not None and
                        self.group_for(active) == group else 0)
            if reg == 8:
                return self.enabled[group]
            if reg == 12:
                return 0  # write-one-clear alias: don't replay old clears
        if address == TIMER + 0xc:
            self.reads[address] += 1
            return int(self.timer_pending)
        return super().read(cpu, offset, size, user)

    def write(self, cpu, offset, size, value, user):
        address = E.PERI + offset
        super().write(cpu, offset, size, value, user)
        if VIC <= address < VIC + 15*16:
            group, reg = divmod(address - VIC, 16)
            if reg == 8:
                self.enabled[group] |= value
            elif reg == 12:
                self.enabled[group] &= ~value
        if address == ACK and self.phase != 'boot':
            if self.vic_pending is not None:
                assert value == self.vic_pending
                self.vic_active = self.vic_pending
                self.vic_pending = None
                self.log('VIC acknowledge', value=hex(value))
            else:
                self.log('VIC end of interrupt', value=hex(value))
                self.vic_active = None
        if address == TIMER + 0x10 and value & 1:
            self.timer_pending = False
        if self.phase != 'boot' and self.source is not None:
            if address == self.source_register(self.source) and value & 0x10:
                # Tick ISR's tail call at 0x154210 sets the source's clear bit.
                self.timer_pending = False
                self.registers[address] &= ~0x10
                self.log('source latch clear', register=hex(address), value=hex(value))

    def memory_write(self, cpu, access, address, size, value, user):
        if self.phase != 'boot':
            self.flag_writes.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
                                     'address': hex(address), 'value': value})

    def flash_write(self, cpu, access, address, size, value, user):
        self.flash_writes.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
                                  'address': hex(address), 'size': size})

    def invalid_memory(self, cpu, access, address, size, value, user):
        self.faults.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
                            'address': hex(address), 'size': size, 'access': access})
        return False  # preserve the fault; never paper over it by mapping RAM

    def code(self, cpu, address, size, user):
        # Same bookkeeping as scheduler: do not stop at panel startup.
        self.instructions += 1
        self.pcs[address] += 1
        self.last_pcs.append(address)
        if address in E.MARKERS and address not in self.markers_seen:
            self.markers_seen.add(address)
            self.events.append({'event': E.MARKERS[address], 'pc': hex(address),
                                'instruction': self.instructions})
        if self.phase != 'boot':
            self.post_pcs[address] += 1
            self.pc_sizes[address] = size
            if self.expected_task_pc is not None:
                assert address == self.expected_task_pc
                self.task_switches[-1]['target_executed'] = True
                self.expected_task_pc = None
            if address in MARKERS and self.post_pcs[address] == 1:
                self.log(MARKERS[address])
            if address == 0x11834:
                self.reader_results.append(hex(cpu.reg_read(UC_ARM_REG_R0)))
            if address == 0x1184c:
                self.dispatches.append(hex(cpu.reg_read(UC_ARM_REG_R0)))
            if address == 0x104da8:
                self.idle_exits += 1
            if address == 0x150694:
                # LDM ip,{ip,pc}^: PC is the second word at the saved frame.
                target = self.word(cpu.reg_read(UC_ARM_REG_R12) + 4)
                task = cpu.mem_read(self.word(0x150950), 1)[0]
                self.task_switches.append({'tick': self.irqs, 'pc': hex(address),
                                           'task': task, 'resume_pc': hex(target),
                                           'target_executed': False})
                self.expected_task_pc = target & ~1
        if address != IDLE:
            return
        self.idle_visits += 1
        if self.phase == 'boot':
            if self.idle_visits == 8:
                self.idle_flag = cpu.reg_read(UC_ARM_REG_R5)
                self.baseline_pcs = set(self.pcs)
                self.stop_reason = 'initial idle loop'
                cpu.emu_stop()
            return
        flag = cpu.mem_read(self.idle_flag, 1)[0]
        if self.phase == 'control':
            return
        if flag:
            return  # let stock consume the ISR's flag before another tick
        if self.irqs >= self.tick_limit:
            self.stop_reason = 'returned to idle after requested IRQs'
            cpu.emu_stop()
        elif not cpu.reg_read(UC_ARM_REG_CPSR) & 0x80:
            self.request_irq = True
            cpu.emu_stop()

    def inject(self):
        cpu = self.uc
        old_cpsr = cpu.reg_read(UC_ARM_REG_CPSR)
        pc_ret = cpu.reg_read(UC_ARM_REG_PC)
        assert pc_ret == IDLE and not old_cpsr & 0x80
        assert self.vic_pending is None and self.vic_active is None
        if not self.spurious:
            group = self.group_for(self.source)
            assert self.force or self.enabled[group] & self.mask_for(self.source)
            if self.source == 56:
                assert self.registers[TIMER] & 1 and self.registers[TIMER + 0x14] == 3
                assert not self.timer_pending
            self.vic_pending = self.source
            self.timer_pending = True
        self.log('manual IRQ entry', source=self.source,
                 group=self.group_for(self.source), old_cpsr=hex(old_cpsr))
        # ARM IRQ banks SP/LR/SPSR; retain flags and F, clear Thumb and mode.
        cpu.reg_write(UC_ARM_REG_CPSR, (old_cpsr & ~0x3f) | 0x92)
        cpu.reg_write(UC_ARM_REG_SPSR, old_cpsr)
        cpu.reg_write(UC_ARM_REG_LR, pc_ret + 4)
        cpu.reg_write(UC_ARM_REG_PC, 0x18)
        self.irqs += 1
        self.request_irq = False

    def boot(self):
        self.run(5_000_000)
        assert not self.error and self.stop_reason == 'initial idle loop', self.result()
        assert self.idle_flag == 0x1811c629
        self.boot_instructions = self.instructions
        self.boot_tick_counter = struct.unpack('<Q', self.uc.mem_read(0x1811c020, 8))[0]
        self.idle_config = {
            'cpsr': hex(self.uc.reg_read(UC_ARM_REG_CPSR)),
            'flag_address': hex(self.idle_flag),
            'enabled_groups': [hex(v) for v in self.enabled],
            'timer': {hex(a): hex(v) for a, v in self.registers.items()
                      if TIMER <= a <= TIMER + 0x14},
        }
        return self

    def exercise(self, source=56, ticks=400, budget=500_000,
                 force=False, spurious=False, control=False):
        self.source, self.tick_limit = source, ticks
        self.force, self.spurious = force, spurious
        self.phase = 'control' if control else 'ticks'
        self.stop_reason = None
        limit = self.instructions + budget
        while self.instructions < limit and not self.error:
            self.run(limit - self.instructions)
            if self.request_irq:
                self.inject()
            else:
                break
        if self.stop_reason is None:
            self.stop_reason = 'CPU fault' if self.error else 'instruction budget exhausted'
        return self.report()

    def report(self):
        new = set(self.post_pcs) - self.baseline_pcs
        ranges = []
        for pc in sorted(new):
            end = pc + self.pc_sizes[pc]
            if ranges and ranges[-1][1] == pc:
                ranges[-1][1] = end
            else:
                ranges.append([pc, end])
        tick_counter = struct.unpack('<Q', self.uc.mem_read(0x1811c020, 8))[0]
        return {
            'source': self.source, 'group': self.group_for(self.source),
            'handler': hex(self.word(ISR_TABLE + 8*self.source)),
            'forced_candidate': self.force, 'spurious': self.spurious,
            'idle_config': self.idle_config,
            'instructions_after_idle': self.instructions - self.boot_instructions,
            'irqs': self.irqs, 'idle_exits': self.idle_exits,
            'reader_results': self.reader_results, 'dispatches': self.dispatches,
            'task_switches': self.task_switches,
            'tick_counter_delta': tick_counter - self.boot_tick_counter,
            'faults': self.faults, 'flash_writes': self.flash_writes,
            'flag_writes': self.flag_writes, 'trace': self.trace,
            'new_pc_count': len(new), 'new_pcs': [hex(a) for a in sorted(new)],
            'new_pc_ranges_end_exclusive': [[hex(a), hex(b)] for a, b in ranges],
            'hot_pcs_after_idle': [[hex(a), n] for a, n in self.post_pcs.most_common(12)],
            'reads_after_idle': [[hex(a), n] for a, n in self.post_reads.most_common()],
            'timer_pending': self.timer_pending,
            'vic_pending': self.vic_pending, 'vic_active': self.vic_active,
            'result': self.result(),
        }


def verify(case, ticks, control=False, spurious=False):
    """Check observed stock effects, not just whether emu_start returned."""
    assert case['result']['error'] is None and not case['faults'], case
    assert case['result']['firmware_bytes_unchanged'] and not case['flash_writes']
    assert case['result']['pc'] == hex(IDLE) or control
    if control or spurious:
        assert case['idle_exits'] == 0 and not case['flag_writes']
        assert case['tick_counter_delta'] == 0 and not case['task_switches']
        if spurious:
            assert case['reader_results'] == ['0x0'] * ticks
            assert not case['dispatches']
        else:
            assert case['irqs'] == 0 and not case['reader_results']
    else:
        assert case['irqs'] == case['idle_exits'] == case['tick_counter_delta'] == ticks
        assert case['reader_results'] == case['dispatches'] == ['0x1541dc'] * ticks
        assert case['flag_writes'] == [item for _ in range(ticks) for item in (
            {'pc': '0x109476', 'address': '0x1811c629', 'value': 1},
            {'pc': '0x104de2', 'address': '0x1811c629', 'value': 0})]
        assert sum(e['event'] == 'source latch clear' for e in case['trace']) == ticks
        assert sum(e['event'] == 'VIC acknowledge' for e in case['trace']) == ticks
        assert sum(e['event'] == 'VIC end of interrupt' for e in case['trace']) == ticks
        assert not case['timer_pending'] and case['vic_active'] is None
        assert case['vic_pending'] is None
        if ticks >= 200:
            assert any(s['resume_pc'] != '0x104d94' for s in case['task_switches'])
            assert all(s['target_executed'] for s in case['task_switches'])
            assert '0x110c96' in case['new_pcs']
    case['assertions_passed'] = True
