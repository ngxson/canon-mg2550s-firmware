#!/usr/bin/env python3
"""Run one stock routine as a fixture and log what it touches (hybrid shape B).

Boots the unmodified stock image to its idle loop with the public BootTrace
model (readiness level 4, caller-supplied 24C08 image), snapshots RAM and the
modeled MMIO, then performs direct calls into stock functions from a stopped
return sentinel. Between cases the snapshot is restored, so cases are
independent. Nothing is patched in flash; RAM presets are explicit per case.

Two ways of handling the stock services a routine reaches (RE brief 16 §3):

* stop (default): execution stops the moment the routine enters an event
  wait, semaphore wait or mailbox receive, logging the arguments and the
  caller. This is the brief's recommended first check.
* stub: the listed service entries are logged and immediately returned from
  with a chosen r0, without executing them. Mailbox sends log the message
  words, the pool allocator hands out scratch memory. This exposes the order
  of CR/LF/head requests the routine would make; it is a tracing aid, not a
  model of successful completion, and cannot show mechanical success.

Conventions from the brief 16 answer §7: Thumb entry (address | 1), r0..r3
then stack words for further arguments, SP 0x100fe000, CPSR 0xd3, LR
0x100ff001 as the stopped return sentinel, scheduler stepping disabled.
"""
import argparse
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'emu')]

from collections import Counter  # noqa: E402
from unicorn import UcError, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ  # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_LR, UC_ARM_REG_SP,  # noqa: E402
    UC_ARM_REG_CPSR, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)
from stock.boot import BootTrace  # noqa: E402

RAM_REGIONS = ((0x10000000, 0x100000), (0x18000000, 0x800000), (0xe0000000, 0x10000))
STACK_TOP = 0x100fe000
SENTINEL = 0x100ff000
STUB = 0x100ff100           # ARM mode-switch entry stub
SCRATCH = 0x100f0000          # bump allocator for stubbed pool allocations
SCRATCH_END = 0x100fd000

# Stock service entries (brief 16 §3). 'stop' entries end the case in stop mode;
# all entries are logged in both modes.
SERVICES = {
    0x106d16: ('event_wait', 'id,mask,mode,out,timeout', True),
    0x106720: ('semaphore_wait', 'id,timeout', True),
    0x107286: ('mailbox_receive', 'id,msg,timeout', True),
    0x106bf0: ('event_set', 'id,mask', False),
    0x106cce: ('event_clear', 'id,preserve_mask', False),
    0x106672: ('semaphore_release', 'id', False),
    0x895c4: ('mailbox_send', 'id,msg', False),
    0x89490: ('pool_alloc', 'out,pool,size', False),
    0x107e28: ('pool_free', 'msg', False),
    0x107170: ('kernel_send', 'id,msg', False),
    0x107d42: ('pool_alloc_wait', 'pool,size,out,timeout', False),
    0x106e7e: ('kernel_op_106e7e', 'a,b,c,d', False),
    0x10736e: ('kernel_op_10736e', 'a,b,c,d', False),
    0x105dd2: ('kernel_query', 'what,out', False),
    0x1085be: ('task_delay', 'ticks', False),
    # kernel privilege/cache helpers used around uncached allocations (0x104254):
    # raise privilege (svc), cache maintenance, drop to user mode. Irrelevant
    # in emulation and incompatible with the harness's manual exception entry.
    0x1509e4: ('privilege_raise', 'a', False), 0x150bf4: ('cache_op_bf4', 'a', False),
    0x150bfc: ('cache_op_bfc', 'a', False), 0x150c04: ('privilege_drop', 'a', False),
    0x150080: ('cache_op_080', 'a', False),
    # the kernel syscall wrappers themselves (`ldr r0,=body; svc #0; bx lr` and
    # the svc #3..#6 stubs): no svc is ever executed in the fixture, so the
    # harness never needs Unicorn's exception entry and can stay in one mode
    0x150850: ('irq_disable', 'a', False), 0x150870: ('irq_restore', 'mask', False),
    0x150874: ('irq_restore_b', 'mask', False), 0x15089c: ('vector_base_query', 'a', False),
    0x150084: ('svc4', 'a', False), 0x150094: ('svc3', 'a', False),
    0x1500a4: ('svc5', 'a', False), 0x1500c0: ('svc6', 'a', False),
}
I2C_STATUS = 0xc0b20100

# Named RAM presets from the brief 16 answer §2 (address, width, value).
PRESETS = {
    'executor-enable': [(0x180d578e, 1, 1)],
    'executor-disable': [(0x180d578e, 1, 0)],
    'cap-ready': [(0x18079333, 1, 1)],
    'heating-enable': [(0x18079aad, 1, 1)],
    # brief 17 §4: u8[0xe0000172] must be NONZERO for the no-abort gate 0xc4a6a
    # (brief 16 had the polarity reversed).
    'permission': [(0xe0000172, 1, 1)],
    'cap-enables': [(0x18079332, 1, 1), (0x18079333, 1, 1)],
    # software cap state byte plus the published f009/2 bit 0 (brief 17 §2)
    'capped': [(0x18079335, 1, 1), (0x1806646c, 4, 1)],
    # experiment: clear status bank A (f008/0x40, idle value 0x1000 after the
    # emulated boot) and the derived summary word, to see what that bit gates
    'status-clear-A': [(0x18066304, 4, 0), (0x18066300, 4, 0)],
    'guards-clear': [(0x18079a31, 1, 0), (0x180d55bd, 1, 0)],
    'ambient': [(0x1807acd4, 2, 50), (0x1807acd6, 2, 50)],
    # brief 16 §2: values 0xe8446 would install (heating enable, BK/CL repeat
    # counts, pre-phase limit 50, repeat-delay controls 50, LF-stroke enable,
    # suction limits 80); the emulated boot leaves them zero.
    'maint-defaults': [(0x18079aad, 1, 1), (0x18079aae, 1, 1), (0x18079aaf, 1, 1), (0x18079ab0, 2, 50),
                       (0x18079ab4, 4, 50), (0x18079ab8, 4, 50), (0x18079abc, 1, 1),
                       (0x18079abe, 2, 80), (0x18079ac0, 2, 80)],
}

# Direct cases. 'expect' values come from the brief 16 answer §7 and are checked
# with --verify; cases without expectations are exploratory traces.
CASES = {
    'e8324-disabled': {
        'call': 0xe8324, 'args': [1, 0, 0, 1, 0, 0], 'presets': ['executor-disable'],
        'expect': {'returned': True, 'r0': 1, 'mmio_writes': 0},
        'note': 'negative control: zero enable byte returns 1 without work'},
    'e8324-stop': {
        'call': 0xe8324, 'args': [1, 0, 0, 1, 0, 0], 'presets': ['executor-enable'],
        'expect': {'stopped_at': 0x106d16, 'lr': 0xd1ae5, 'mmio_writes': 0,
                   'r0_r3': [0x1a, 0x2010, 0]},
        'note': 'executor reaches the CR+LF initial synchronization wait'},
    'e818a-stop': {
        'call': 0xe818a, 'args': [1, 1], 'presets': [],
        'expect': {'stopped_at': 0x106720, 'lr': 0x9ad7b, 'mmio_writes': 0,
                   'r0_r3': [0x13]},
        'note': 'per-head sequence reaches the shared status semaphore'},
    'init-e8610': {'call': 0xe8610, 'args': [], 'presets': [],
                   'expect': {'returned': True, 'mmio_writes': 0}},
    'init-c763e': {'call': 0xc763e, 'args': [], 'presets': [],
                   'expect': {'returned': True, 'mmio_writes': 0}},
    'e8324-type1-stub': {
        'call': 0xe8324, 'args': [1, 0, 0, 1, 0, 0], 'stub': True,
        'presets': ['executor-enable', 'cap-enables', 'permission', 'guards-clear', 'ambient', 'maint-defaults'],
        'note': 'trace: BK type 1 (preparation only) with services stubbed'},
    'e8324-type2-stub': {
        'call': 0xe8324, 'args': [2, 0, 0, 1, 0, 0], 'stub': True,
        'presets': ['executor-enable', 'cap-enables', 'permission', 'guards-clear', 'ambient', 'maint-defaults'],
        'note': 'trace: BK type 2 (pump branch) with services stubbed'},
    'cap-stub': {'call': 0xe7dd6, 'args': [2, 1, 1], 'stub': True,
                 'presets': ['cap-enables', 'permission', 'guards-clear', 'ambient'],
                 'note': 'trace: ordinary cap sequence call'},
    'uncap-stub': {'call': 0xe7a2a, 'args': [0, 1], 'stub': True,
                   'presets': ['permission', 'guards-clear', 'ambient', 'capped'],
                   'note': 'trace: ordinary uncap call from a capped state (byte and published bit)'},
    'wipe-stub': {'call': 0xe8668, 'args': [0, 1], 'stub': True,
                  'presets': ['permission', 'guards-clear', 'ambient'],
                  'note': 'trace: ordinary wipe wrapper call'},
    'uncap-stub-clearA': {'call': 0xe7a2a, 'args': [0, 1], 'stub': True,
                          'presets': ['permission', 'guards-clear', 'ambient', 'capped', 'status-clear-A'],
                          'note': 'experiment: uncap from capped state with status bank A cleared'},
    'wipe-stub-clearA': {'call': 0xe8668, 'args': [0, 1], 'stub': True,
                         'presets': ['permission', 'guards-clear', 'ambient', 'status-clear-A'],
                         'note': 'experiment: wipe with status bank A cleared'},
    'e8324-type1-stub-clearA': {
        'call': 0xe8324, 'args': [1, 0, 0, 1, 0, 0], 'stub': True,
        'presets': ['executor-enable', 'cap-enables', 'permission', 'guards-clear', 'ambient', 'maint-defaults', 'status-clear-A'],
        'note': 'experiment: BK type 1 with status bank A cleared'},
}

WATCH = [('gates', 0x18079330, 8), ('controls', 0x18079a34, 12), ('cap_state', 0x18079335, 1),
         ('exec_enable', 0x180d578e, 1), ('repeat', 0x18079aa8, 4), ('maint_defaults', 0x180d55b0, 0x38),
         ('cleaning_time', 0x180799e0, 4), ('busy', 0x18079336, 1), ('reentry', 0x18079a31, 1),
         ('cap_busy', 0x180d55bd, 1), ('permission', 0xe0000172, 1), ('wp_wait', 0x180d5730, 16),
         ('status_f008', 0x180662ec, 0x30), ('status_f009', 0x18066468, 0x20),
         ('ready_wait', 0x180d5718, 16), ('ready_flag', 0x180d57e7, 1), ('temperature', 0x1807acd4, 4),
         ('maint_params', 0x18079aa8, 0x20)]


class Fixture(BootTrace):
    """BootTrace with direct-call support and service boundary handling."""

    def __init__(self, **inputs):
        super().__init__(**inputs)
        self.in_call = False
        self.stub = False
        self.service_log = []
        self.ram_writes = []
        self.ram_watch = []
        self.stopped_at = None
        self.scratch = SCRATCH
        self.queued = {}
        self.loop_limit = 400
        self.reply_mode = 'status'
        self.heating_model = True
        self.ram_reads = Counter()
        self.ram_pages = set()
        self.loop_reads = Counter()
        self.last_sent = None
        self.repeat_period, self.repeats = 0, 0
        self.recent_keys = []
        for lo, hi in ((0x10000000, 0x187fffff), (0xe0000000, 0xe000ffff)):
            self.uc.hook_add(UC_HOOK_MEM_WRITE, self.ram_write, begin=lo, end=hi)
            self.uc.hook_add(UC_HOOK_MEM_READ, self.ram_read, begin=lo, end=hi)
        # flash coverage: data reads from the stock image during calls (code
        # coverage comes from the executed-PC counter); 256-byte pages
        self.flash_data_pages = set()
        self.flash_code_pages = set()
        self.boot_data_pages = set()
        self.uc.hook_add(UC_HOOK_MEM_READ, self.flash_read, begin=0, end=0x1fffff)

    # ----- snapshot / restore -----
    def snapshot(self):
        self.saved = {'ram': [(a, bytes(self.uc.mem_read(a, n))) for a, n in RAM_REGIONS],
                      'registers': dict(self.registers), 'eeprom': bytes(self.eeprom)}

    def restore(self):
        for a, data in self.saved['ram']:
            self.uc.mem_write(a, data)
        self.registers = dict(self.saved['registers'])
        self.eeprom = bytearray(self.saved['eeprom'])
        self.scratch = SCRATCH

    # ----- hooks -----
    def ram_write(self, cpu, access, address, size, value, user):
        if not self.in_call:
            return
        self.ram_pages.add(address >> 12)
        if any(lo <= address < lo + n for _, lo, n in self.ram_watch) and len(self.ram_writes) < 2000:
            self.ram_writes.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)), 'address': hex(address),
                                    'size': size, 'value': hex(value), 'instruction': self.instructions})

    def flash_read(self, cpu, access, address, size, value, user):
        pages = self.flash_data_pages if self.in_call else self.boot_data_pages
        pages.add(address >> 8)
        if size > 4 or address & 0xff > 0xf8:
            pages.add((address + size - 1) >> 8)

    def ram_read(self, cpu, access, address, size, value, user):
        if self.in_call:
            key = (cpu.reg_read(UC_ARM_REG_PC), address)
            self.ram_pages.add(address >> 12)
            self.ram_reads[key] += 1
            self.loop_reads[key] += 1

    def code(self, cpu, address, size, user):
        if self.in_call and not STUB <= address < STUB + 0x20:
            sp = cpu.reg_read(UC_ARM_REG_SP)
            if not (SCRATCH_END <= sp <= STACK_TOP + 0x1000 or 0xe0000000 <= sp < 0xe0010000):
                # The stock kernel switched to another task's stack: the fixture
                # call context is gone. Stop and report the path that led here.
                self.stop_reason = (f'context switch: sp {hex(sp)} outside the fixture stack at pc {hex(address)}, '
                                    f'lr {hex(cpu.reg_read(UC_ARM_REG_LR))}')
                self.switch_pcs = [hex(a) for a in list(self.last_pcs)[-24:]]
                cpu.emu_stop()
                return
        if self.in_call and address < 0x200000:
            self.flash_code_pages.add(address >> 8)
        if self.in_call and address == SENTINEL:
            self.stop_reason = 'returned to sentinel'
            cpu.emu_stop()
            return
        if self.in_call and address in SERVICES:
            name, argnames, stops = SERVICES[address]
            names = argnames.split(',')
            regs = [cpu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
            sp = cpu.reg_read(UC_ARM_REG_SP)
            values = regs[:4] + [self.word(sp + 4*i) for i in range(max(0, len(names) - 4))]
            entry = {'service': name, 'pc': hex(address), 'lr': hex(cpu.reg_read(UC_ARM_REG_LR)),
                     'args': {n: hex(v) for n, v in zip(names, values)}, 'instruction': self.instructions,
                     'sp': hex(sp), 'return_addresses': self.backtrace_guess(sp),
                     'mmio_writes_so_far': len(self.mmio_writes) - self.call_mmio_start}
            if name in ('mailbox_send', 'kernel_send') and 0x10000000 <= values[1] < 0x18800000:
                m = values[1]
                entry['message_words'] = [hex(self.word(m + 4*i)) for i in range(0x8c // 4)]
                entry['command'] = hex(self.word(m + 0x24) & 0xffff)
                entry['request_words'] = [hex(self.word(m + 0x28)), hex(self.word(m + 0x2c)), hex(self.word(m + 0x30))]
                entry['decoded'] = self.decode_request(values[0], m)
            if name == 'event_wait':
                entry['stack_words'] = [hex(self.word(sp + 4*i)) for i in range(6)]
            self.service_log.append(entry)
            key = (name, entry['lr'])
            self.recent_keys.append(key)
            period = self.cycle_period()
            if period and period == self.repeat_period:
                self.repeats += 1
            else:
                self.repeat_period, self.repeats = period, 1
                self.loop_reads = Counter()
            if self.stub:
                entry['stubbed'] = True
                if period and self.repeats > self.loop_limit:
                    self.stopped_at = address
                    cycle = ' / '.join(f'{n}@{lr}' for n, lr in self.recent_keys[-period:])
                    self.stop_reason = f'service polling loop (period {period}): {cycle} repeated {self.repeats} times'
                    cpu.emu_stop()
                    return
                self.stub_return(cpu, name, values)
                return
            if stops:
                self.stopped_at = address
                self.stop_reason = f'stopped before {name}'
                cpu.emu_stop()
                return
        super().code(cpu, address, size, user)

    AMBIENT = 50   # raw half-degree units, 25 C; the preset 'ambient' sets this at call start

    def adjust_temperature(self, cpu, delta):
        values = []
        for a in (0x1807acd4, 0x1807acd6):
            t = struct.unpack('<h', bytes(cpu.mem_read(a, 2)))[0]
            t = max(self.AMBIENT, min(t + delta, 2000)) if delta < 0 else min(t + delta, 2000)
            cpu.mem_write(a, struct.pack('<h', t))
            values.append(t)
        self.service_log[-1]['temperature_bk_cl'] = values

    def cycle_period(self):
        keys = self.recent_keys
        for p in (1, 2, 3, 4, 6):
            if len(keys) >= 2*p and keys[-p:] == keys[-2*p:-p]:
                return p
        return 0

    def backtrace_guess(self, sp, words=40):
        # Thumb return addresses found on the stack: a cheap approximate backtrace.
        found = []
        for i in range(words):
            if sp + 4*i >= STACK_TOP:
                break
            v = self.word(sp + 4*i)
            if v & 1 and 0x10000 <= v < 0x1f2000:
                found.append(hex(v))
        return found[:12]

    def decode_request(self, mailbox, m):
        # brief 17 §5-6 message layouts
        command = self.word(m + 0x24) & 0xffff
        s32 = lambda a: struct.unpack('<i', bytes(self.uc.mem_read(a, 4)))[0]
        if mailbox == 0x19 and command == 0x4041:
            return {'kind': 'CR move', 'target': s32(m + 0x28), 'profile_flags': hex(self.word(m + 0x2c))}
        if mailbox == 0x19 and command == 0x4043:
            return {'kind': 'CR profile motion', 'kind_flags': hex(self.word(m + 0x28)),
                    'option_byte': self.uc.mem_read(m + 0x30, 1)[0], 'option_word': hex(self.word(m + 0x34))}
        if mailbox == 0x19 and command == 0x4042:
            return {'kind': 'CR release'}
        if mailbox == 0x18 and command == 0x4031:
            num, den = struct.unpack('<HH', bytes(self.uc.mem_read(m + 0x2c, 4)))
            return {'kind': 'LF move', 'ulf': s32(m + 0x28), 'fraction': f'{num}/{den}',
                    'direction': self.uc.mem_read(m + 0x30, 1)[0], 'profile_flags': hex(self.word(m + 0x34)),
                    'reply_mailbox': hex(self.word(m + 0x10))}
        if mailbox == 2:
            return {'kind': 'NVM envelope', 'opcode': hex(self.word(m + 0x1c) & 0xffffff),
                    'eeprom_offset': hex(self.word(m + 0x2c)), 'length': hex(self.word(m + 0x30))}
        if mailbox == 0x17:
            return {'kind': 'status request', 'selector': hex(self.word(m + 0x28)),
                    'words': [hex(self.word(m + 0x2c)), hex(self.word(m + 0x30))]}
        return None

    # Status server model (brief 17 §1-3, STMMainTask on mailbox 0x17): the
    # getters read the stock RAM banks, publications apply masked updates to
    # them, the reply reuses the request buffer with bit 22 and result 1.
    F008_BANK, F009_BANK = 0x180662ec, 0x18066468

    def status_server_reply(self, cpu, out_pointer):
        msg = self.last_sent
        length = 0x34
        request = bytearray(cpu.mem_read(msg, length))
        control = struct.unpack_from('<I', request, 0x24)[0]
        command = control & 0xffff
        selector = struct.unpack_from('<I', request, 0x28)[0]
        index = (selector & -selector).bit_length() - 1 if selector else -1
        info = {'command': hex(command), 'selector': hex(selector)}
        result = 1
        if command in (0xf008, 0xf009) and index >= 0:
            bank = self.F008_BANK if command == 0xf008 else self.F009_BANK
            value = self.word(bank + 4*index)
            struct.pack_into('<I', request, 0x2c, value)
            info['value'] = hex(value)
        elif command in (0xf004, 0xf005) and index >= 0:
            preserve, replacement = struct.unpack_from('<II', request, 0x2c)
            bank = self.F008_BANK if command == 0xf004 else self.F009_BANK
            old = self.word(bank + 4*index)
            new = (old & preserve) | (replacement & ~preserve)
            cpu.mem_write(bank + 4*index, struct.pack('<I', new & 0xffffffff))
            info.update({'preserve_mask': hex(preserve), 'replacement': hex(replacement),
                         'old': hex(old), 'new': hex(new & 0xffffffff)})
            if command == 0xf004:
                summary = 0
                if self.word(self.F008_BANK + 4*9) or self.word(self.F008_BANK + 4*10):
                    summary |= 1
                if any(self.word(self.F008_BANK + 4*i) for i in (6, 7, 8)):
                    summary |= 2
                if self.word(self.F008_BANK + 4*11):
                    summary |= 4
                cpu.mem_write(self.F008_BANK + 4*5, struct.pack('<I', summary))
                info['summary'] = hex(summary)
        else:
            result = 2
        control = (control & ~0x00200000 & ~0x07000000) | 0x00400000 | (result << 24)
        struct.pack_into('<I', request, 0x24, control)
        cpu.mem_write(msg, bytes(request))
        cpu.mem_write(out_pointer, struct.pack('<I', msg))
        info['result'] = result
        return info

    def stub_return(self, cpu, name, values):
        r0 = 0
        if name == 'irq_disable':
            r0 = 0xc0                # previous I/F mask: both masked
        if name == 'semaphore_wait' and values[1] == 0:
            r0 = 0xffffffce          # brief 8: drain loops take semaphores with timeout 0 until -50
        if name == 'event_wait' and 0x10000000 <= values[3] < 0x18800000:
            cpu.mem_write(values[3], struct.pack('<I', values[1]))   # report requested bits
            if values[0] == 0x1c and values[1] & 0x80000000 and self.heating_model:
                # FIXTURE ASSUMPTION: a completed head work list (event 0x1c bit 31,
                # the heating/prefire completion wait) heats the head by 4 units.
                self.adjust_temperature(cpu, +4)
            if values[0] == 0x33 and self.transfer_pending:
                # EEPROM completion event: complete the modeled I2C transfer the
                # way the scheduler's source-52 injection does, so FIFO data is valid.
                self.registers[I2C_STATUS] = 1
                self.transfer_pending = False
                self.transfers[-1]['irq_delivered'] = 'fixture'
                self.service_log[-1]['eeprom_transfer'] = {k: self.transfers[-1][k] for k in ('operation', 'offset', 'count', 'data')}
        elif name == 'mailbox_send':
            self.last_sent = values[1]
        elif name == 'mailbox_receive' and self.queued.get(values[0]) and 0x10000000 <= values[1] < 0x18800000:
            msg = self.queued[values[0]].pop(0)
            cpu.mem_write(values[1], struct.pack('<I', msg))
            self.service_log[-1]['queued_message'] = hex(msg)
            r0 = 0
        elif name == 'mailbox_receive' and 0x10000000 <= values[1] < 0x18800000:
            # FIXTURE ASSUMPTION (from the observed requester 0x9ad5a..0x9ae0c):
            # the requester accepts a 0x30-byte reply whose word +0x24 carries
            # the same 16-bit command, bit 22 set and bits 24..26 == 1, and then
            # copies words +0x28/+0x2c back to its caller. Reply words default
            # to zero ("nothing reported"); this models no real mechanism state.
            if self.reply_mode == 'status' and self.last_sent is not None:
                self.service_log[-1]['reply'] = self.status_server_reply(cpu, values[1])
            else:
                r0 = 1
        elif name == 'task_delay':
            # FIXTURE ASSUMPTION: time passes. The readiness loop 0xe97c0..0xe97ee
            # polls u16 0x180d5720 (countdown) and u8 0x180d57e7 between delays;
            # decrement the countdown once per delay so the wait can end.
            count = struct.unpack('<H', bytes(cpu.mem_read(0x180d5720, 2)))[0]
            if count:
                cpu.mem_write(0x180d5720, struct.pack('<H', count - 1))
            # FIXTURE ASSUMPTION: the head cools toward ambient while time passes.
            # The pre-phase loop 0xe80ba..0xe80d8 waits (bounded, 1000 x delay 6)
            # while the cached BK/CL temperature (s16 0x1807acd4/6, half-degrees,
            # read by 0xcb29e) is >= 2 * s16[0x18079ab0]. The cache is normally
            # refreshed from the head ADC, which this fixture does not model.
            if self.heating_model:
                self.adjust_temperature(cpu, -1)
        elif name == 'pool_alloc_wait':
            # raw blocking allocation (pool, size, &out): hand out scratch memory
            size = (values[1] + 7) & ~7
            if self.scratch + size > SCRATCH_END or not 0x10000000 <= values[2] < 0x18800000:
                r0 = 1
            else:
                cpu.mem_write(values[2], struct.pack('<I', self.scratch))
                cpu.mem_write(self.scratch, bytes(size))
                self.scratch += size
        elif name == 'pool_alloc':
            size = (values[2] + 7) & ~7
            if self.scratch + size > SCRATCH_END:
                r0 = 1
            else:
                cpu.mem_write(values[0], struct.pack('<I', self.scratch))
                cpu.mem_write(self.scratch, bytes(size))
                self.scratch += size
        cpu.reg_write(UC_ARM_REG_R0, r0)
        cpu.reg_write(UC_ARM_REG_PC, cpu.reg_read(UC_ARM_REG_LR))

    # ----- direct call -----
    def call(self, address, args, budget=2_000_000, cpsr=0xd3):
        cpu = self.uc
        self.in_call = True
        self.stub_mode_used = self.stub
        self.service_log, self.ram_writes = [], []
        self.ram_reads, self.loop_reads = Counter(), Counter()
        self.last_sent, self.repeat_period, self.repeats = None, 0, 0
        self.recent_keys = []
        self.stopped_at, self.stop_reason, self.error = None, None, None
        self.switch_pcs = None
        self.phase = 'fixture'
        self.poll_counts.clear()
        self.call_mmio_start = len(self.mmio_writes)
        self.call_instr_start = self.instructions
        extra = args[4:]
        sp = (STACK_TOP - 4*len(extra)) & ~7
        for i, v in enumerate(extra):
            cpu.mem_write(sp + 4*i, struct.pack('<I', v & 0xffffffff))
        cpu.mem_write(SENTINEL, b'\x00\xbf' * 4)   # Thumb NOPs at the sentinel
        # Enter through an ARM stub that switches mode with a real msr: a CPSR
        # register write does not re-bank sp/lr in this Unicorn, so a later
        # stock `msr cpsr_c, #0x90` (drop to user mode) would expose stale
        # banked registers. Mode comes from --cpsr with IRQ/FIQ masked.
        mode = (cpsr & 0x1f) | 0xc0
        stub = struct.pack('<5I', 0xe321f000 | mode, 0xe59fd008, 0xe59fe008, 0xe59fc008, 0xe12fff1c)
        stub += struct.pack('<3I', sp, SENTINEL | 1, address | 1)
        cpu.mem_write(STUB, stub)
        cpu.reg_write(UC_ARM_REG_CPSR, 0xd3)
        for reg, v in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            cpu.reg_write(reg, v & 0xffffffff)
        try:
            cpu.emu_start(STUB, SENTINEL, count=budget)
        except UcError as exc:
            self.error = str(exc)
        pc = cpu.reg_read(UC_ARM_REG_PC)
        if self.stop_reason is None:
            if pc == SENTINEL:
                self.stop_reason = 'returned to sentinel'
            elif self.error:
                self.stop_reason = 'CPU fault'
            else:
                self.stop_reason = 'instruction budget exhausted'
        self.in_call = False
        return {'returned': pc == SENTINEL, 'r0': cpu.reg_read(UC_ARM_REG_R0),
                'pc': hex(pc), 'lr': hex(cpu.reg_read(UC_ARM_REG_LR)),
                'stop_reason': self.stop_reason, 'error': self.error,
                'stopped_at': hex(self.stopped_at) if self.stopped_at else None,
                'sp': hex(cpu.reg_read(UC_ARM_REG_SP)), 'last_pcs': [hex(a) for a in list(self.last_pcs)[-16:]],
                'switch_pcs': getattr(self, 'switch_pcs', None),
                'instructions': self.instructions - self.call_instr_start,
                'mmio_writes': self.mmio_writes[self.call_mmio_start:],
                'mmio_read_tail': list(self.mmio_tail)[-16:],
                'services': self.service_log, 'ram_writes': self.ram_writes,
                'service_histogram': [[n, lr, c] for (n, lr), c in Counter(
                    (e['service'], e['lr']) for e in self.service_log).most_common(12)],
                'ram_reads_top': [[hex(pc), hex(a), c] for (pc, a), c in self.ram_reads.most_common(24)],
                'loop_window_reads': [[hex(pc), hex(a), c] for (pc, a), c in self.loop_reads.most_common(24)],
                'firmware_bytes_unchanged': bytes(cpu.mem_read(0, len(self.image))) == self.image}

    def window(self, address, n):
        return bytes(self.uc.mem_read(address, n)).hex()


def apply_presets(fx, presets, extra_sets):
    applied = []
    for p in list(presets) + list(extra_sets):
        items = PRESETS[p] if isinstance(p, str) else [p[1:]]
        for address, width, value in items:
            if address >= 0xc0000000:                      # peripheral register model value, not RAM
                fx.registers[address] = value
            else:
                fx.uc.mem_write(address, value.to_bytes(width, 'little'))
            applied.append({'address': hex(address), 'width': width, 'value': hex(value)})
    return applied


def parse_set(text):
    # addr=value[:width]
    addr, _, rest = text.partition('=')
    value, _, width = rest.partition(':')
    return ('set', int(addr, 0), int(width, 0) if width else 1, int(value, 0))


def check(case, result):
    exp = case.get('expect')
    if not exp:
        return None
    problems = []
    if 'returned' in exp and result['returned'] != exp['returned']:
        problems.append('returned')
    if 'r0' in exp and result['r0'] != exp['r0']:
        problems.append(f"r0={result['r0']}")
    if 'mmio_writes' in exp and len(result['mmio_writes']) != exp['mmio_writes']:
        problems.append(f"mmio_writes={len(result['mmio_writes'])}")
    if 'stopped_at' in exp:
        if result['stopped_at'] != hex(exp['stopped_at']):
            problems.append(f"stopped_at={result['stopped_at']}")
        elif result['services']:
            last = result['services'][-1]
            if 'lr' in exp and last['lr'] != hex(exp['lr']):
                problems.append(f"lr={last['lr']}")
            got = [int(v, 16) for v in list(last['args'].values())[:len(exp.get('r0_r3', []))]]
            if got != exp.get('r0_r3', got):
                problems.append(f'args={[hex(v) for v in got]}')
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--eeprom', type=Path, default=ROOT / 'emu/inputs/eeprom.bin')
    ap.add_argument('--output', type=Path, default=ROOT / 'emu/output/fixture/maintenance.json')
    ap.add_argument('--case', action='append', default=[], help='named case (repeatable); default: all named cases')
    ap.add_argument('--call', type=lambda v: int(v, 0), help='ad-hoc call address (Thumb)')
    ap.add_argument('--args', default='', help='comma-separated integers for the ad-hoc call')
    ap.add_argument('--stub', action='store_true', help='stub services for the ad-hoc call')
    ap.add_argument('--set', action='append', default=[], type=parse_set, help='RAM preset addr=value[:width], repeatable')
    ap.add_argument('--preset', action='append', default=[], choices=sorted(PRESETS), help='named preset, repeatable')
    ap.add_argument('--budget', type=int, default=2_000_000, help='instruction limit per call')
    ap.add_argument('--boot-ticks', type=int, default=300)
    ap.add_argument('--boot-budget', type=int, default=12_000_000)
    ap.add_argument('--cpsr', type=lambda v: int(v, 0), default=0xd3,
                    help='mode for the call (default supervisor); the kernel syscall wrappers are stubbed, so no svc executes in the fixture')
    ap.add_argument('--reply', choices=('status', 'none'), default='status', help='stubbed mailbox_receive: answer from the modeled status server (stock RAM banks), or fail')
    ap.add_argument('--queue', action='append', default=[], help='id=addr: a message the stubbed mailbox_receive(id) hands out once (task-body probes)')
    ap.add_argument('--no-heating-model', action='store_true', help='do not raise the cached head temperatures on each stubbed delay')
    ap.add_argument('--loop-limit', type=int, default=400, help='stop a stubbed trace after this many consecutive identical service calls')
    ap.add_argument('--verify', action='store_true', help='fail if a case with expectations deviates')
    opts = ap.parse_args()

    fx = Fixture(ready_level=4, eeprom=opts.eeprom)
    fx.ram_watch = WATCH
    fx.reply_mode, fx.loop_limit = opts.reply, opts.loop_limit
    for q in opts.queue:
        qid, qaddr = q.split('='); fx.queued.setdefault(int(qid, 0), []).append(int(qaddr, 0))
    fx.heating_model = not opts.no_heating_model
    fx.boot()
    boot = fx.run_boot(ticks=opts.boot_ticks, budget=opts.boot_budget)
    assert boot['result']['error'] is None and not boot['faults'], boot['result']
    fx.snapshot()
    report = {'boot': {'instructions': boot['result']['instructions'], 'pc': boot['result']['pc'],
                       'ticks': boot['ticks'], 'eeprom_sha256': boot['eeprom_sha256'],
                       'firmware_sha256': boot['firmware_sha256']},
              'conventions': {'sp': hex(STACK_TOP), 'sentinel': hex(SENTINEL), 'cpsr': hex(opts.cpsr),
                              'services': {hex(a): n for a, (n, _, _) in SERVICES.items()}},
              'idle_windows': {name: fx.window(a, n) for name, a, n in WATCH},
              'cases': []}

    selected = []
    if opts.call is not None:
        args = [int(v, 0) for v in opts.args.split(',') if v]
        selected.append(('adhoc', {'call': opts.call, 'args': args, 'stub': opts.stub,
                                   'presets': list(opts.preset), 'note': 'ad-hoc'}))
    names = opts.case or (list(CASES) if opts.call is None else [])
    selected += [(n, CASES[n]) for n in names]

    failures = []
    for name, case in selected:
        fx.restore()
        fx.stub = bool(case.get('stub'))
        applied = apply_presets(fx, case.get('presets', []), opts.set)
        before = {n: fx.window(a, k) for n, a, k in WATCH}
        result = fx.call(case['call'], case['args'], budget=opts.budget, cpsr=opts.cpsr)
        after = {n: fx.window(a, k) for n, a, k in WATCH}
        problems = check(case, result)
        entry = {'name': name, 'call': hex(case['call']), 'args': [hex(a) for a in case['args']],
                 'stub': fx.stub, 'note': case.get('note', ''), 'presets_applied': applied,
                 'expect': {k: (hex(v) if isinstance(v, int) and k != 'mmio_writes' and k != 'r0' else v)
                            for k, v in case.get('expect', {}).items()},
                 'problems': problems, **result,
                 'windows_changed': {n: {'before': before[n], 'after': after[n]}
                                     for n in before if before[n] != after[n]}}
        report['cases'].append(entry)
        if problems:
            failures.append((name, problems))
        print(f"{name}: {result['stop_reason']} after {result['instructions']} instr, "
              f"r0={result['r0']}, mmio_writes={len(result['mmio_writes'])}, "
              f"services={len(result['services'])}" + (f" PROBLEMS {problems}" if problems else ''))

    def ranges(pages):
        out = []
        for p in sorted(pages):
            if out and out[-1][1] == p:
                out[-1][1] = p + 1
            else:
                out.append([p, p + 1])
        return [[hex(a << 8), hex(b << 8)] for a, b in out]
    boot_pages = {a >> 8 for a in fx.pcs if a < 0x200000}
    pages = sorted(fx.ram_pages)
    runs = []
    for pg in pages:
        if runs and runs[-1][1] == pg:
            runs[-1][1] = pg + 1
        else:
            runs.append([pg, pg + 1])
    report['ram_pages_touched_4k'] = [[hex(a << 12), hex(b << 12)] for a, b in runs]
    report['flash_coverage'] = {
        'page_bytes': 256,
        'boot_code_pages': len(boot_pages - fx.flash_code_pages),
        'call_code_pages': len(fx.flash_code_pages), 'call_data_pages': len(fx.flash_data_pages),
        'call_code_ranges': ranges(fx.flash_code_pages),
        'call_data_ranges': ranges(fx.flash_data_pages),
        'boot_code_ranges': ranges(boot_pages),
        'boot_data_pages': len(fx.boot_data_pages), 'boot_data_ranges': ranges(fx.boot_data_pages),
    }
    opts.output.parent.mkdir(parents=True, exist_ok=True)
    opts.output.write_text(json.dumps(report, indent=1))
    print(f'wrote {opts.output}')
    if opts.verify and failures:
        sys.exit(f'expectations not met: {failures}')


if __name__ == '__main__':
    main()
