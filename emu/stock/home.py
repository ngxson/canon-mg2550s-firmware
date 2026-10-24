"""Accepted scanner message, stock home dispatch and stationary feedback.

Extends boot tracing with an explicitly recorded scanner command input.
The sole application input mutation substitutes +0x2c in the first stock-owned
0xc028 message BEFORE the real mailbox send to 0x27. Queue links, allocation,
receive results, gate byte, calls, and flash are never patched. This is an
internal command experiment, not a decoded physical copy button/USB scan job.
"""
from collections import Counter
import struct

from .boot import BootTrace, verify as verify_boot
from . import startup as E
from .scheduler import TickScheduler, ISR_TABLE
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
    UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
    UC_ARM_REG_PC, UC_ARM_REG_LR, UC_ARM_REG_SP, UC_ARM_REG_CPSR)

# Include the requested endpoints, plus P+0x404 status for completion evidence.
# Timer span intentionally includes channel 5, unlike boot tracing's narrower WATCH.
CAPTURE = [('C', 0xc0c56000, 0xc0c56404),
           ('P', 0xc0c57000, 0xc0c57408),
           ('serial', 0xc0c50200, 0xc0c50224),
           ('routing', 0xc0c41000, 0xc0c41504),
           ('timer4-5', 0xc0140400, 0xc0140604)]
REGS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
        UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7)
POINTS = {
    0xf5958: 'mode predicate', 0xf59fc: 'stock home gate store of one',
    0xf5a5e: 'sequencer dispatch', 0xf5b52: 'c028 dispatch case',
    0xf5a4e: 'gate getter', 0xf5b72: 'gate result',
    0xf5b88: 'home branch', 0xf9d38: 'home wrapper',
    0xf9d4a: 'wrapper state precondition', 0xf9d52: 'wrapper error precondition',
    0xf9dae: 'initialization result', 0xf9dc0: 'home precondition',
    0xf9dc4: 'home call', 0xf9dc8: 'home return',
    0xf9f26: 'no-home stub', 0xf4d68: 'home thunk',
    0xf4d18: 'home preparation', 0xf4c56: 'home seek',
    0xf6430: 'motion facade request', 0xf6dfe: 'manager move',
    0xf6aaa: 'signed distance move', 0xf6ac2: 'direction decision',
    0xf74fa: 'motion submission', 0xf8280: 'planner enqueue',
    0xf7e5e: 'queue task', 0xf7d6c: 'queue dispatch',
    0xf83c4: 'planner execute', 0xf8992: 'motor direction store',
    0xf8a5c: 'hardware arming', 0xf899e: 'driver enable',
    0xf2350: 'driver control', 0xf2378: 'timer4 start',
    0xf6366: 'timer4 ISR', 0xf87da: 'motor ISR callback',
    0xf873a: 'servo tick', 0xf926c: 'feedback update',
    0xf91bc: 'encoder direction read', 0xf91d6: 'speed direction mismatch',
    0xf966c: 'trajectory profile', 0xf8682: 'servo correction',
    0xf8638: 'drive computation', 0xf865c: 'periodic drive store',
    0xf936a: 'progress detector', 0xf93f0: 'progress failure branch',
    0xf93fe: 'motor failure', 0xf9412: 'failure neutral drive',
    0xf9430: 'failure flag store', 0xf9442: 'failure event call',
    0xf9060: 'motor stop', 0xf4c6c: 'first seek returned',
    0xf4c74: 'home failure', 0xf07ea: 'scanner error distribution',
    0xf833a: 'planner wait', 0xf6b54: 'move wait',
}


def watched(address):
    return next((name for name, lo, hi in CAPTURE if lo <= address < hi), None)


class HomeCapture(BootTrace):
    def __init__(self, mode=0x0200000f, timer4=True, serial_irq=True, **inputs):
        super().__init__(ready_level=4, **inputs)
        self.mode = mode
        self.timer4 = timer4
        self.serial_irq = serial_irq
        self.serial_launch_tick = 0
        self.serial_irq_transfers = []
        if timer4:
            self.extra_timers[4] = 28  # f2378 -> 154056(0x1c), ISR f6366
        self.input_injections = []
        self.home_points = []
        self.mailbox_events = []
        self.capture_writes = []
        self.feedback_reads = []
        self.send_context = None

    def record(self, pc):
        cpu = self.uc
        return {'pc': hex(pc), 'instruction': self.instructions,
                'tick': self.ticks, 'task': self.current_task,
                'irq_source': self.vic_active,
                'r0_r7': [hex(cpu.reg_read(r)) for r in REGS],
                'lr': hex(cpu.reg_read(UC_ARM_REG_LR)),
                'sp': hex(cpu.reg_read(UC_ARM_REG_SP)),
                'cpsr': hex(cpu.reg_read(UC_ARM_REG_CPSR))}

    def message(self, ptr):
        assert 0x18000000 <= ptr < 0x18800000
        length = int.from_bytes(self.uc.mem_read(ptr + 8, 2), 'little')
        assert 0x28 <= length <= 0x1000, (hex(ptr), length)
        return {'message': hex(ptr), 'length': length,
                'bytes': bytes(self.uc.mem_read(ptr, length)).hex(),
                'command_flags': hex(self.word(ptr + 0x24)),
                'mode': hex(self.word(ptr + 0x2c)) if length >= 0x30 else None}

    def code(self, cpu, address, size, user):
        # Payload substitution at the send boundary, before any kernel work.
        if address == 0x107170:
            mailbox, ptr = cpu.reg_read(UC_ARM_REG_R0), cpu.reg_read(UC_ARM_REG_R1)
            self.send_context = None
            if mailbox in (0x23, 0x24, 0x26, 0x27):
                before = self.message(ptr)
                if (mailbox == 0x27 and self.word(ptr + 0x24) & 0xffff == 0xc028
                        and self.mode and not self.input_injections):
                    assert before['length'] >= 0x30
                    assert self.word(ptr + 0x2c) == 0
                    cpu.mem_write(ptr + 0x2c, struct.pack('<I', self.mode))
                    self.input_injections.append({**self.record(address),
                        'method': 'substitute mode in stock-owned message before native send',
                        'mailbox': hex(mailbox), 'address': hex(ptr + 0x2c),
                        'before': before, 'after': self.message(ptr)})
                event = {**self.record(address), 'event': 'native send',
                         'mailbox': hex(mailbox), **self.message(ptr)}
                self.mailbox_events.append(event)
                self.send_context = event
        # Prove whether the actual kernel woke a receiver or linked a queue.
        if address in (0x1071ba, 0x107278, 0x10727e) and self.send_context:
            self.mailbox_events.append({**self.record(address),
                'event': 'direct receiver delivery' if address == 0x1071ba else 'queue link',
                'mailbox': self.send_context['mailbox'],
                'message': self.send_context['message']})
        super().code(cpu, address, size, user)
        if address in (0xef526, 0xf5626, 0xf5cca):
            ptr = self.word(cpu.reg_read(UC_ARM_REG_SP))
            self.mailbox_events.append({**self.record(address),
                'event': 'native receive returned',
                'mailbox': hex({0xef526: 0x27, 0xf5626: 0x23, 0xf5cca: 0x24}[address]),
                **self.message(ptr)})
        if address in POINTS:
            point = {**self.record(address), 'name': POINTS[address]}
            if address == 0xf6430:
                raw = cpu.reg_read(UC_ARM_REG_R2)
                point.update(selection=cpu.reg_read(UC_ARM_REG_R0),
                             signed_distance=raw - (1 << 32) if raw >> 31 else raw)
            if address in (0xf8a5c, 0xf8638, 0xf93fe, 0xf936a):
                motor = cpu.reg_read(UC_ARM_REG_R0)
                point['motor_state'] = bytes(cpu.mem_read(motor, 0x170)).hex()
            if address == 0xf8638:
                ctrl = cpu.reg_read(UC_ARM_REG_R1)
                point['direction'] = cpu.mem_read(ctrl, 1)[0]
                point['u_signed16'] = struct.unpack('<h', cpu.mem_read(ctrl + 0xc4, 2))[0]
            self.home_points.append(point)

    def write(self, cpu, offset, size, value, user):
        super().write(cpu, offset, size, value, user)
        if E.PERI + offset == 0xc0120104 and value & 1:
            self.serial_launch_tick = self.ticks
        group = watched(E.PERI + offset)
        if group:
            self.capture_writes.append({**self.mmio_writes[-1], 'range': group,
                'ordinal': len(self.capture_writes) + 1,
                'irq_source': self.vic_active,
                'lr': hex(cpu.reg_read(UC_ARM_REG_LR))})

    def read(self, cpu, offset, size, user):
        value = super().read(cpu, offset, size, user)
        address = E.PERI + offset
        if address in (0xc0c57100, 0xc0c57304, 0xc0c57200,
                       0xc0c57204, 0xc0c57208, 0xc0c5720c):
            self.feedback_reads.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
                'address': hex(address), 'value': hex(value), 'size': size,
                'tick': self.ticks, 'instruction': self.instructions,
                'irq_source': self.vic_active})
        return value

    def inject(self):
        # The same serial port used by the inherited polled identification now
        # has an IRQ-driven caller. Complete only a launched transfer, one tick
        # later, with the stock source enabled; real ISR signals event 0x29.
        if (self.serial_irq and self.pending is not None
                and not self.pending.get('irq_delivered')
                and self.ticks > self.serial_launch_tick
                and self.enabled[self.group_for(29)] & self.mask_for(29)):
            self.poll_counts.clear()
            self.pending['polls'] = max(2, self.pending['polls'])
            self.pending['irq_delivered'] = True
            self.serial_irq_transfers.append({'tick': self.ticks,
                'launch_tick': self.serial_launch_tick,
                'tx': self.pending['tx'], 'control': self.pending['control'],
                'handler': hex(self.word(ISR_TABLE + 8*29))})
            self.source = 29
            TickScheduler.inject(self)
            self.timer_pending = False
            return
        super().inject()

    def run_home(self, ticks=3500, budget=40000000):
        result = self.run_boot(ticks, budget)
        result.update(mode=hex(self.mode), timer4_enabled=self.timer4,
            serial_irq_enabled=self.serial_irq,
            serial_irq_transfers=self.serial_irq_transfers,
            serial_irq_handler=hex(self.word(ISR_TABLE + 8*29)),
            input_injections=self.input_injections, home_points=self.home_points,
            mailbox_events=self.mailbox_events, capture_writes=self.capture_writes,
            feedback_reads=self.feedback_reads,
            capture_ranges=[{'name': n, 'start': hex(a), 'end_exclusive': hex(b)}
                            for n, a, b in CAPTURE],
            home_pc_hits={hex(p): self.pcs[p] for p in POINTS},
            motor_final=bytes(self.uc.mem_read(0xe00001bc, 0x170)).hex(),
            registers_final={hex(a): hex(v) for a, v in self.registers.items()
                             if watched(a)})
        result['assumptions'] += [
            {'mechanism': 'Internal scanner command input', 'enabled': bool(self.mode),
             'values': f'First stock c028 send to mailbox 0x27: +0x2c = {self.mode:#x}. '
                       'All other bytes and real send/receive processing retained. '
                       'No direct gate-byte write; not a decoded panel/USB job.',
             'evidence': '107170, ef526, ef3f8..ef424, f5626..f5634, f5958'},
            {'mechanism': 'Timer4 interrupt', 'enabled': self.timer4,
             'values': 'Channel4 -> source28/group3/mask4; at most one per later '
                       'scheduler tick while timer control bit0 and VIC enable set. '
                       'Uses boot timer reset and coarse IRQ mechanism unchanged.',
             'evidence': 'f2378..f2398, f6366, f87da, f23cc; stock VIC tables'},
            {'mechanism': 'Stationary encoder', 'enabled': True,
             'values': 'No feedback overrides: P+100, P+304, P+200/204/208/20c '
                       'retain last stock write or zero. No comparator/complete IRQ.',
             'evidence': 'feedback_reads and mmio_writes retained in full'},
            {'mechanism': 'AFE serial completion IRQ', 'enabled': self.serial_irq,
             'values': 'Started c0120104 transfer completes one scheduler tick '
                       'later if source29 is enabled. Advance inherited poll '
                       'model to completion (c0120110 bit0=1); deliver real '
                       'source29 ISR, which acknowledges status and sets event29. '
                       'Serial response remains inherited 0x40.',
             'evidence': 'f3258, f3288, f31d2, f31e8, f3564; serial_irq_transfers'}]
        return result


def verify(result):
    assert result['requested_ticks'] >= 1000, 'verification requires >=1000 ticks'
    assert result['result']['error'] is None and not result['faults']
    assert result['result']['firmware_bytes_unchanged'] and not result['flash_writes']
    assert all(result['input_files_unchanged'].values())
    assert result['tick_counter_delta'] == result['ticks']
    assert result['reader_results'] == result['dispatches']
    expected = Counter({'0x1541dc': result['ticks'], '0x11188b': result['completions']})
    if result['serial_irq_transfers']:
        expected[result['serial_irq_handler']] += len(result['serial_irq_transfers'])
    for source, count in result['extra_timer_irqs'].items():
        expected[{1: '0xdbf0d', 96: '0xe24df', 28: '0xf6367'}[int(source)]] += count
    assert Counter(result['dispatches']) == expected
    assert all(r['target_executed'] for r in result['context_restores'])
    assert result['vic_pending'] is None and result['vic_active'] is None
    assert not result['timer_pending']
    assert result['ticks'] == result['requested_ticks'] and result['next_wall'] is None
    assert result['result']['stop_reason'] == 'tick limit at idle'
    assert all(t['irq_delivered'] and (t['operation'] == 'write' or
               t['rx_consumed'] == t['count']) for t in result['transfers'])
    all_writes = [w for w in result['mmio_writes'] if watched(int(w['address'], 16))]
    assert len(all_writes) == len(result['capture_writes'])
    for full, captured in zip(all_writes, result['capture_writes']):
        assert all(full[k] == captured[k] for k in full)
    if result['mode'] == '0x0':
        verify_boot(result)
        assert not result['input_injections']
    else:
        assert len(result['input_injections']) == 1
        injection = result['input_injections'][0]
        before, after = (bytes.fromhex(injection[k]['bytes']) for k in ('before', 'after'))
        assert before[:0x2c] == after[:0x2c] and before[0x30:] == after[0x30:]
        assert before[0x2c:0x30] == bytes(4)
        assert after[0x2c:0x30] == struct.pack('<I', int(result['mode'], 16))
        deliveries = [p for p in result['mailbox_events']
                      if p['event'] == 'native receive returned']
        for mailbox in ('0x27', '0x23', '0x24'):
            assert any(p['mailbox'] == mailbox and p['mode'] == result['mode']
                       and int(p['command_flags'], 16) & 0xffff == 0xc028
                       for p in deliveries)
        assert any(w['pc'] == '0xf59fc' and w['address'] == '0x180d8631'
                   and w['value'] == '0x1' for w in result['scanner_gate_writes'])
        assert any(p['pc'] == '0xf5b72' and p['r0_r7'][0] == '0x1'
                   for p in result['home_points'])
        hits = result['home_pc_hits']
        assert hits['0xf9d38'] == 1 and hits['0xf9f26'] == 0
        if not result['serial_irq_enabled']:
            assert not result['serial_irq_transfers'] and not hits['0xf8a5c']
            assert result['last_observed_wait_by_task'].get(
                36, result['last_observed_wait_by_task'].get('36'))['lr'] == '0xf329d'
        else:
            assert result['serial_irq_handler'] == '0xf54bd'
            assert len(result['serial_irq_transfers']) >= 11
            assert all(t['tick'] > t['launch_tick'] for t in result['serial_irq_transfers'])
            assert hits['0xf8a5c'] >= 1
            moves = [p for p in result['home_points'] if p['pc'] == '0xf6430']
            arms = [p for p in result['home_points'] if p['pc'] == '0xf8a5c']
            assert len(arms) == len(moves)
            assert moves[0]['selection'] == 1 and moves[0]['signed_distance'] == -529134
            # Each arm must contain the entire contiguous register signature.
            for move, arm in zip(moves, arms):
                distance = move['signed_distance']
                target = 0x700000 + distance
                direction = int(distance < 0)
                state = bytes.fromhex(arm['motor_state'])
                assert state[0x1c] == direction
                assert struct.unpack_from('<H', state, 0xec)[0] == 0  # residue
                assert struct.unpack_from('<h', state, 0x118)[0] == 0  # offset
                signature = [
                    (0xc0c57200, 0xffffff), (0xc0c57204, 0xffffff),
                    (0xc0c57208, 0xffffff), (0xc0c5720c, 0xffffff),
                    (0xc0c56008, 2000), (0xc0c57200, 0xffffff),
                    (0xc0c57160, 1), (0xc0c57164, 0x700000),
                    (0xc0c57124, 0), (0xc0c5711c, 0), (0xc0c57120, target),
                    (0xc0c56010, 2000), (0xc0c57128, target + (1 if not direction else -1)),
                    (0xc0c56028, 0), (0xc0c5601c, 0), (0xc0c57124, 1),
                    (0xc0c56018, 0), (0xc0c5600c, 0), (0xc0c5711c, 1),
                    (0xc0c57130, 0), (0xc0c5712c, 0), (0xc0c56100, 0),
                    (0xc0c56104, 0), (0xc0c56100, 1), (0xc0c56000, 0x102)]
                writes = [w for w in result['capture_writes']
                          if w['instruction'] > arm['instruction']][:len(signature)]
                assert [(int(w['address'], 16), int(w['value'], 16))
                        for w in writes] == signature
            assert all(x['value'] == '0x700000' for x in result['feedback_reads']
                       if x['address'] == '0xc0c57100')
            assert all(x['value'] == '0x0' for x in result['feedback_reads']
                       if x['address'] == '0xc0c57304')
            if not result['timer4_enabled']:
                assert not hits['0xf865c'] and not hits['0xf93fe']
            else:
                assert hits['0xf865c'] and hits['0xf93fe']
                # Every emitted periodic value must match the observed dir/u.
                computed = None
                for p in result['home_points']:
                    if p['pc'] == '0xf8638':
                        u = p['u_signed16']
                        computed = int(2 * (u if p['direction'] else 10000-u) / 5)
                    elif p['pc'] == '0xf865c':
                        assert int(p['r0_r7'][0], 16) == computed
                assert any(p['pc'] == '0x106bf0' and p['lr'] == '0xf9447'
                           and p['r0_r3'][:2] == ['0x28', '0x200000']
                           for p in result['points'])
                if result['requested_ticks'] >= 3000:
                    assert [p['selection'] for p in moves] == [1, 25, 24, 1, 25]
                    assert hits['0xf93fe'] == 5 and hits['0xf9dc8'] == 1
                    assert any(p['pc'] == '0xf9dc8' and p['r0_r7'][0] == '0x2'
                               for p in result['home_points'])
                    assert any(p['pc'] == '0xf07ea' and p['r0_r7'][0] == '0x5'
                               for p in result['home_points'])
                    assert result['registers_final']['0xc0c56000'] == '0x0'
                    assert result['registers_final']['0xc0c56008'] == '0x7d0'
    result['home_assertions_passed'] = True
