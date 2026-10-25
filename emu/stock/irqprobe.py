"""Forced single-interrupt injection after stock boot reaches idle (black-box).

The scheduler model delivers a chosen VIC source once, bypassing the enable
masks (its documented discovery mode), at a chosen tick after the ordinary boot
has reached idle. The unmodified stock vector, reader, handler and RTOS run.
The report records, for that injection: the handler address from the stock ISR
table, the MMIO reads and writes the handler and the woken tasks performed
before the next idle, RTOS event posts, mailbox receives, tasks seen, new code
ranges and any CPU fault. Sweeping all sources shows which handler touches a
given peripheral (for example the USB core at 0xc0780000) and what it posts,
without reading any stock code. A forced source is not evidence that hardware
raises it; it is evidence of what the handler does when it is raised.
"""
from collections import Counter

from .boot import BootTrace
from .scheduler import TickScheduler, ISR_TABLE
from . import startup as E
from unicorn.arm_const import UC_ARM_REG_PC


class IrqProbe(BootTrace):
    def __init__(self, probe_source=0, probe_tick=300, preset=None, **inputs):
        super().__init__(**inputs)
        self.probe_source, self.probe_tick = probe_source, probe_tick
        self.preset = dict(preset or {})          # register values applied just before the injection
        self.probed = False
        self.probe_reads = []
        self.marks = None

    def read(self, cpu, offset, size, user):
        value = super().read(cpu, offset, size, user)
        if self.probed and self.ticks < self.probe_tick + 3:
            self.probe_reads.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)), 'address': hex(E.PERI + offset),
                                     'value': hex(value), 'task': self.current_task, 'tick': self.ticks})
        return value

    def inject(self):
        if self.ticks == self.probe_tick and not self.probed:
            self.probed = True
            self.marks = (set(self.boot_pcs), len(self.mmio_writes), len(self.boot_points), len(self.restores))
            saved = (self.source, self.force)
            self.source, self.force = self.probe_source, True
            self.registers.update(self.preset)
            self.log('probe injection', source=self.probe_source, preset={hex(a): hex(v) for a, v in self.preset.items()})
            TickScheduler.inject(self)
            self.timer_pending = False
            self.source, self.force = saved
            return
        super().inject()

    def run_probe(self, ticks=320, budget=60000000):
        result = self.run_boot(ticks=ticks, budget=budget)
        pcs0, w0, p0, r0 = self.marks or (set(), 0, 0, 0)
        after_w = self.mmio_writes[w0:]
        points = self.boot_points[p0:]
        new = set(self.boot_pcs) - pcs0
        result['probe'] = {
            'source': self.probe_source, 'group': self.group_for(self.probe_source),
            'handler': hex(self.word(ISR_TABLE + 8 * self.probe_source)), 'tick': self.probe_tick,
            'injected': self.probed, 'faults': self.faults[:5], 'preset': {hex(a): hex(v) for a, v in self.preset.items()},
            'reads_by_handler': sorted(Counter((r['address']) for r in self.probe_reads if r['task'] is None).items()),
            'reads_after': sorted(Counter((r['address']) for r in self.probe_reads).items())[:60],
            'writes_after': sorted(Counter((w['address'], w['value']) for w in after_w if w['tick'] <= self.probe_tick + 2).items())[:60],
            'event_posts': sorted(Counter((p['r0_r3'][0], p['r0_r3'][1]) for p in points if p['pc'] == '0x106bf0').items()),
            'mailbox_receives': sorted(Counter((p['task'], p['r0_r3'][0]) for p in points if p['pc'] == '0x107286').items()),
            'restores_after': [(r['task'], r['target']) for r in self.restores[r0:]][:20],
            'new_pc_count': len(new), 'new_pc_min_max': [hex(min(new)), hex(max(new))] if new else None,
            'stop_reason': result['result']['stop_reason'],
        }
        return result
