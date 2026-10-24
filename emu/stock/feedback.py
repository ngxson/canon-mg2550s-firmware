"""Stock home control-flow experiment; synthetic feedback, NOT a motor model.

The first seek travels 2048 coordinate units then
stalls; positive backoff advances 64 units per harness tick. These chosen
inputs exercise branches and are not a torque, timing or encoder calibration.
Stock bytes, functions, return values and OS events are never patched.
"""

from .home import HomeCapture
from .scheduler import TickScheduler, ISR_TABLE

P, C = 0xc0c57000, 0xc0c56000
EXTRA_POINTS = {
    0xf4c94: 'backoff returned', 0xf4caa: 'backoff displacement comparison',
    0xf4cf6: 'adequate backoff branch', 0xf4cfa: 'optical stage call',
    0xf4c2c: 'optical stage entry', 0xf502c: 'optical reference search',
    0xf4bec: 'second optical stage', 0xf4d14: 'seek return',
    0xf7aae: 'companion event dispatch', 0xf87fe: 'motion result callback',
    0xf6aaa: 'signed distance request', 0xf4d5e: 'final position move return',
}


class Recheck(HomeCapture):
    def __init__(self, feedback='backoff', completion=True, **inputs):
        self.feedback = feedback
        self.motor_completion = completion
        self.model_tick = -1
        self.model_events = []
        self.checkpoints = []
        self.moves = []
        self.active_move = None
        self.completion_irqs = 0
        super().__init__(**inputs)

    def code(self, cpu, address, size, user):
        super().code(cpu, address, size, user)
        if address == 0xf8a5c:
            point = self.record(address)
            self.active_move = dict(point, start=self.registers.get(P+0x100, 0),
                                    target=int(point['r0_r7'][1], 16))
            self.active_move['direction'] = int(point['r0_r7'][2], 16)
            self.active_move['samples'] = 0
            self.moves.append(self.active_move)
        if address in EXTRA_POINTS:
            self.checkpoints.append(dict(self.record(address), name=EXTRA_POINTS[address]))
        if address == 0xf4c2c:
            self.stop_reason = 'optical reference stage reached; pixels not modeled'
            self.request_irq = False
            cpu.emu_stop()

    def inject(self):
        if self.model_tick != self.ticks:
            self.model_tick = self.ticks
            m = self.active_move
            if (m and self.feedback != 'stationary'
                    and self.registers.get(C, 0) == 0x102):
                x = self.registers[P+0x100]
                if m['direction']:
                    y = max(m['start']-2048, x-64)
                else:
                    y = min(m['target'], x+64)
                if y != x:
                    self.registers[P+0x100] = y
                    self.registers[P+0x304] = 4 if y < x else 0
                    # Keep periods at their stationary sentinel: intentionally
                    # isolate position/completion from the speed estimator.
                    self.model_events.append(dict(tick=self.ticks,
                        event='synthetic position', before=x, after=y,
                        direction=self.registers[P+0x304]))
                if (not m['direction'] and y == m['target'] and self.motor_completion
                        and self.registers.get(C+0x100, 0) & 1
                        and not m.get('completed')):
                    self.registers[C+0x104] = self.registers.get(C+0x104, 0) | 1
                    m['completed'] = True
                    self.model_events.append(dict(tick=self.ticks,
                        event='synthetic companion completion bit', position=y))
                m['samples'] += 1
        if (self.registers.get(C+0x104, 0) & 1
                and self.registers.get(C+0x100, 0) & 1
                and self.enabled[self.group_for(27)] & self.mask_for(27)):
            self.poll_counts.clear()
            self.source = 27
            self.completion_irqs += 1
            TickScheduler.inject(self)
            self.timer_pending = False
            return
        super().inject()


def describe(emu, result):
    """Record every modeled feedback input alongside the stock observations."""
    result['assumptions'] = [a for a in result['assumptions']
                             if a['mechanism'] != 'Stationary encoder']
    result['assumptions'].append(dict(mechanism='Scripted position / completion',
        enabled=True, values=__doc__, completion_enabled=emu.motor_completion,
        periods='Unchanged sentinel 0xffffff; no speed model',
        completion='C+104 bit0 at positive target, IRQ27 if stock enables it; '
                   'comparator electrical semantics are an assumption'))
    result.update(checkpoints=emu.checkpoints, modeled_inputs=emu.model_events,
        moves=emu.moves, completion_irqs=emu.completion_irqs,
        completion_handler=hex(emu.word(ISR_TABLE + 8*27)))


def verify(emu, result):
    assert result['result']['error'] is None and not result['faults']
    assert result['result']['firmware_bytes_unchanged'] and not result['flash_writes']
    assert all(result['input_files_unchanged'].values())
    if emu.motor_completion:
        assert [p['selection'] for p in result['home_points'] if p['pc'] == '0xf6430'] == [1, 25]
        assert any(p['pc'] == '0xf4c6c' and p['r0_r7'][0] == '0x2' for p in result['home_points'])
        assert any(p['pc'] == '0xf4c94' and p['r0_r7'][0] == '0x1' for p in emu.checkpoints)
        assert any(p['pc'] == '0xf4caa' and int(p['r0_r7'][0], 16) == 10582 for p in emu.checkpoints)
        assert emu.completion_irqs == 1
        assert result['result']['stop_reason'].startswith('optical reference stage')
        assert emu.registers[C] == 0 and emu.registers[C+8] == 2000
    else:
        assert result['ticks'] == result['requested_ticks']
        assert result['result']['stop_reason'] == 'tick limit at idle'
        assert [m['direction'] for m in emu.moves] == [1, 0]
        assert emu.registers[P+0x100] == emu.moves[-1]['target']
        assert emu.completion_irqs == 0
        assert not any(e['event'] == 'synthetic companion completion bit' for e in emu.model_events)
        assert not any(p['pc'] == '0xf4c2c' for p in emu.checkpoints)
    result['feedback_assertions_passed'] = True
