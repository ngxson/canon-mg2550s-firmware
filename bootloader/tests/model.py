"""Small ASIC MMIO/DMA model. No stock dump or firmware function replacement.

Timing/readiness and USB tokens are synthetic. Firmware, headers, CRC checks,
flash bounds, state transitions and USB responses execute as ARM instructions.
"""
from collections import deque
import json
import os
from pathlib import Path
import struct
import zlib
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_CPSR, UC_CPU_ARM_946

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get('MG2550S_BOOT_BUILD', ROOT / 'build')).resolve()
USB = 0xc0780000
ENABLE = 0x80000000
TICKS_PER_MS = 96000


def setup(kind, request, value=0, index=0, length=0):
    return struct.pack('<BBHHH', kind, request, value, index, length)


def checked(callback):
    def wrapper(self, cpu, *args):
        try:
            return callback(self, cpu, *args)
        except BaseException as error:
            self.error = error
            cpu.emu_stop()
            return 0
    return wrapper


class Model:
    def __init__(self, *, image=None, actions=(), bulk=b'', i2c_image=None,
                 command=None, corrupt=False, host=True, speed=True, coalesced=False,
                 cut_after=None, corrupt_frame=False, hold_reboot_ack=False,
                 maintenance=False, metadata=None, dangerous=False):
        self.meta = metadata or json.loads((BUILD / 'manifest.json').read_text())
        self.app_flash = self.meta['layout']['application'][0]
        self.dangerous = dangerous
        self.source = image or (BUILD / 'factory.bin').read_bytes()
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.uc.ctl_set_cpu_model(UC_CPU_ARM_946)
        self.uc.mem_map(0, 0x200000); self.uc.mem_write(0, self.source)
        self.uc.mem_map(0x20000000, 0x800000)
        self.uc.mem_map(0xe0000000, 0x10000)
        self.uc.reg_write(UC_ARM_REG_CPSR, 0xd3)
        self.regs = {}; self.error = None
        self.ticks = 0; self.counter = 0; self.timer_deadline = None
        self.erases = []; self.programs = []; self.leds = 0
        self.erase_sizes = []; self.maintenance = maintenance
        self.stage = 'stage0'
        self.led_patterns = {'stage0': [], 'bootloader': [], 'application': []}
        self.corrupt = corrupt; self.cut_after = cut_after; self.cut = False
        self.host = host; self.speed = speed; self.coalesced = coalesced
        self.armed = set(); self.connected = False; self.generation = -1
        self.bus_stage = 0; self.requests = deque(); self.current = None
        self.actions = list(actions); self.responses = []; self.identities = []
        self.bulk = bulk; self.bulk_position = 0; self.bulk_started = False
        self.configured = False; self.max_packet = 512 if speed else 64
        self.i2c_image = i2c_image; self.command = command; self.selected = 0xffffffff
        self.i2c_tx = []; self.i2c_rx = deque(); self.results = []
        self.corrupt_frame = corrupt_frame; self.i2c_addresses = set()
        self.hold_reboot_ack = hold_reboot_ack; self.stalls = []
        self.entered = []; self.entry_times = []; self.resets = 0
        self.predicate = lambda e: False
        self.uc.mmio_map(0xc0000000, 0x10000000, self.read, None, self.write, None)
        for address in (0, 0x20010000, 0x20020000):
            self.uc.hook_add(UC_HOOK_CODE, self.observe, begin=address, end=address)
        for symbols in self.meta['symbols'].values():
            if 'service_poll' not in symbols: continue
            address = int(symbols['service_poll'], 16)
            self.uc.hook_add(UC_HOOK_CODE, self.observe, begin=address, end=address)

    def observe(self, cpu, address, size, user):
        if address == 0: self.resets += 1; self.stage = 'stage0'
        if address in (0x20010000, 0x20020000):
            self.stage = 'bootloader' if address == 0x20010000 else 'application'
            self.entered.append(address); self.entry_times.append(self.counter)
        if self.predicate(self): cpu.emu_stop()

    def reg(self, offset): return self.regs.get(USB + offset, 0)
    def setreg(self, offset, value): self.regs[USB + offset] = value
    def endpoint(self, direction, number): return (0x900 if direction == 'in' else 0xb00) + number * 32

    def ready(self, direction, number, interrupt=1):
        block = self.endpoint(direction, number)
        mask = (0x840 if direction == 'in' else 0x880) + number * 4
        return ((direction, number) in self.armed and self.reg(mask) & interrupt
                and not self.reg(block + 8)
                and (interrupt == 8 or not self.reg(block) & 0x20000))

    def transfer(self, direction, number, data=b'', is_setup=False):
        block = self.endpoint(direction, number)
        address, capacity = self.reg(block + 0x14), self.reg(block + 0x10) & 0x7ffff
        if direction == 'in': data = bytes(self.uc.mem_read(address, capacity)) if capacity else b''
        else:
            assert len(data) <= capacity
            if data: self.uc.mem_write(address, data)
        self.setreg(block + 0x10, capacity - len(data))
        self.setreg(block + 0x14, address + len(data))
        self.setreg(block, self.reg(block) & ~ENABLE)
        self.setreg(block + 8, 8 if is_setup else 1)
        self.armed.discard((direction, number))
        return data

    def attach(self):
        self.generation += 1; self.bus_stage = 0; self.current = None
        self.configured = False; self.bulk_started = False
        self.requests = deque([
            ('address', setup(0, 5, 7)),
            ('device', setup(0x80, 6, 0x100, length=18)),
            ('config', setup(0x80, 6, 0x200, length=255)),
            ('configure', setup(0, 9, 1)),
        ])
        if self.generation < len(self.actions): self.requests.extend(self.actions[self.generation])

    def finish(self):
        request = self.current; data = bytes(request['data'])
        self.responses.append((self.generation, request['name'], data))
        if request['name'] == 'device': self.identities.append(struct.unpack_from('<H', data, 10)[0])
        if request['name'] == 'configure': self.configured = True
        if request['name'] == 'begin': self.bulk_started = True
        self.current = None

    def host_step(self):
        if not self.host or not self.connected or self.reg(0x804) & 2: return
        if self.reg(0x14) or any(self.reg(b+8) for b in (0x900, 0xb00, 0xb20)): return
        if self.bus_stage < 2:
            event = 0x3000 if self.coalesced else (0x1000, 0x2000)[self.bus_stage]
            self.setreg(0x14, event); self.bus_stage += 2 if self.coalesced else 1; return
        if self.current:
            request = self.current
            if self.reg(0x900)&0x200000:
                self.stalls.append(request['name']); self.current=None; return
            if request['phase'] == 'in' and self.ready('in', 0):
                if self.hold_reboot_ack and request['name'].startswith('reboot'): return
                if request['name'] == 'address': assert self.reg(0x800) & 0x7f0 == 0x70
                data = self.transfer('in', 0); request['data'].extend(data)
                if not request['setup'][0] & 0x80: assert not data; self.finish()
                elif len(data) < 64 or len(request['data']) >= request['length']: request['phase'] = 'status'
            elif request['phase'] == 'status' and self.ready('out', 0):
                self.transfer('out', 0); self.finish()
            return
        if self.requests and self.ready('out', 0, 8):
            name, packet = self.requests.popleft()
            self.current = {'name': name, 'setup': packet, 'data': bytearray(), 'phase': 'in',
                            'length': struct.unpack_from('<H', packet, 6)[0]}
            self.transfer('out', 0, packet, True); return
        if self.bulk_started and self.bulk_position < len(self.bulk) and self.ready('out', 1):
            chunk = self.bulk[self.bulk_position:self.bulk_position+self.max_packet]
            self.transfer('out', 1, chunk); self.bulk_position += len(chunk); return
        if self.configured: self.requests.append(('info', setup(0xc0, 0x42, length=32)))

    def info(self):
        replies = [data for gen, name, data in self.responses if name == 'info' and gen == self.generation]
        return struct.unpack('<8I', replies[-1]) if replies else None

    @checked
    def read(self, cpu, offset, size, user):
        address = 0xc0000000 + offset; self.ticks += 1
        if address == 0xc0aa0014: return 2
        if address in (0xc0aa0018, 0xc0a90014): return 1
        if address == 0xc0aa0050: return 0x1540ef if self.regs.get(0xc0aa0010) == 2 else 0
        if address == 0xc0100000: return self.regs.get(address, 0) | 0x80008000
        if address == 0xc0c82404: return 1
        if address == 0xc0c82008: self.counter = (self.counter + TICKS_PER_MS) & 0xffffffff; return self.counter
        if address == 0xc0140100 and self.timer_deadline and self.ticks >= self.timer_deadline:
            self.regs[address] = 0; self.timer_deadline = None
        if address == 0xc0b20100: return 1 | (0x40 if not self.i2c_rx else 0)
        if address in (0xc0b2001c, 0xc0b20118): return 0
        if address == 0xc0b2011c: return self.i2c_rx.popleft()
        if address == 0xc0c88024: return self.regs.get(address, 0) | 0x100
        if address == USB + 0x10: return (self.reg(0x10) & ~0x31) | ENABLE
        if address == USB + 0x808: return 0 if self.speed else 2
        if address == USB + 0x14: self.host_step()
        return self.regs.get(address, 0)

    def i2c_response(self):
        if self.selected == 0xffffffff:
            offer = 4 if self.command is not None else ((1 if self.maintenance else 2) if self.i2c_image else 0)
            image = self.i2c_image or b''
            body = struct.pack('<7I', 0xffffffff, 0x31435352, 1, len(image), zlib.crc32(image), self.command or 0, offer)
        else:
            body = struct.pack('<I', self.selected) + self.i2c_image[self.selected:self.selected+24].ljust(24, b'\xff')
        return body + struct.pack('<I', zlib.crc32(body) ^ (1 if self.corrupt_frame else 0))

    @checked
    def write(self, cpu, offset, size, value, user):
        address = 0xc0000000 + offset; self.ticks += 1
        off = address - USB
        # Endpoint n: IN control at 0x900+32n, OUT control at 0xb00+32n (n < 16).
        if off == 0x14 or (0x900 <= off < 0xd00 and off % 32 == 8):
            self.setreg(off, self.reg(off) & ~value); return
        if 0x900 <= off < 0xd00 and off % 32 == 0:
            direction = 'in' if off < 0xb00 else 'out'; number = (off-(0x900 if direction=='in' else 0xb00))//32
            if value & 0x40000000: value &= ~ENABLE; self.armed.discard((direction, number))
            elif value & ENABLE: self.armed.add((direction, number))
            else: self.armed.discard((direction, number))
            if value & 0x08000000: value |= 0x20000
            if value & 0x04000000: value &= ~0x20000
            value &= ~0x4c000000
        self.regs[address] = value
        if off == 0x804:
            if value & 2: self.connected = False
            elif not self.connected:
                self.connected = True; self.attach()
        if address == 0xc0c82008: self.counter = value
        if address == 0xc0140100:
            if value == 0x80000000: self.regs[address] = 0; self.timer_deadline = None
            elif value & 1: self.timer_deadline = self.ticks + 128
        if address == 0xc0c8d014 and value == 1:
            self.leds += 1
            self.led_patterns[self.stage].append(tuple(
                self.regs.get(0xc0c8d140 + channel*8, 0) for channel in range(4)))
        if address == 0xc0b2001c and value == 1: self.i2c_tx = []; self.i2c_rx = deque()
        if address == 0xc0b20120: self.i2c_tx.append(value & 255)
        if address == 0xc0b20118 and value == 1:
            slave = self.regs[0xc0b2010c]; self.i2c_addresses.add(slave); assert slave == 0x42
            if self.regs[0xc0b20114] == 1:
                self.i2c_rx = deque(self.i2c_response())
            elif len(self.i2c_tx) == 5:
                data = int.from_bytes(bytes(self.i2c_tx[1:]), 'little')
                if self.i2c_tx[0] == 0x52: self.selected = data
                elif self.i2c_tx[0] == 0x44:
                    self.results.append(data); self.i2c_image = None; self.command = None
        if address == 0xc0aa0054 and value == 1 and self.regs.get(0xc0aa0040) in (0x20, 0xd8):
            opcode = self.regs[0xc0aa0040]
            dest = self.regs[0xc0aa004c]; length = 0x1000 if opcode == 0x20 else 0x10000
            assert dest % length == 0 and dest+length <= 0x200000
            if self.stage == 'stage0':
                assert self.maintenance and opcode == 0x20 and 0x4000 <= dest < 0x10000
            else:
                assert opcode == 0xd8 and (self.dangerous or dest >= self.app_flash)
            cpu.mem_write(dest, b'\xff'*length)
            # Peripheral writes bypass emulated CPU stores. Unicorn 2.1.4 can
            # otherwise reuse old translated flash instructions after reset,
            # even though data reads see the newly programmed bytes. Drop only
            # those translation blocks; this is not a hardware I-cache model.
            cpu.ctl_remove_cache(dest, dest+length)
            self.erases.append(dest); self.erase_sizes.append(length)
        if address == 0xc0aa0058 and value == 1:
            dest, source = self.regs[0xc0aa005c], self.regs[0xc0a90000]
            assert dest % 4096 == 0 and dest+4096 <= 0x200000 and self.regs[0xc0aa0060] == 4096
            if self.stage == 'stage0': assert self.maintenance and 0x4000 <= dest < 0x10000
            else: assert self.dangerous or dest >= self.app_flash
            data = bytes(cpu.mem_read(source, 4096)); old = bytes(cpu.mem_read(dest, 4096))
            data = bytes(a & b for a, b in zip(old, data))
            if self.corrupt: data = bytes([data[0] ^ 1]) + data[1:]
            cpu.mem_write(dest, data)
            cpu.ctl_remove_cache(dest, dest+len(data))
            self.programs.append(dest)
            if self.cut_after == len(self.programs): self.cut = True; cpu.emu_stop()

    def run(self, predicate, *, resume=False, budget=30000000):
        self.predicate = predicate
        self.uc.emu_start(self.uc.reg_read(UC_ARM_REG_PC) if resume else 0, 0x207fe000, count=budget)
        if self.error: raise self.error
        assert self.cut or predicate(self), {'pc': hex(self.uc.reg_read(UC_ARM_REG_PC)),
            'entered': self.entered, 'identities': self.identities, 'info': self.info(), 'results': self.results}
        protected = 0 if self.dangerous else (0x4000 if self.maintenance else self.app_flash)
        assert bytes(self.uc.mem_read(0, protected)) == self.source[:protected]
        if self.maintenance:
            assert bytes(self.uc.mem_read(0x10000, 0x1f0000)) == self.source[0x10000:]

    def summary(self):
        return {'identities': [hex(x) for x in self.identities], 'entered': [hex(x) for x in self.entered],
                'resets': self.resets, 'erases': self.erases, 'erase_sizes': self.erase_sizes, 'programs': self.programs,
                'i2c_results': self.results, 'i2c_addresses': sorted(self.i2c_addresses),
                'led_frames': self.leds,
                'led_pattern_samples': {stage: frames[:8] for stage, frames in self.led_patterns.items()},
                'protected_prefix_bytes': 0 if self.dangerous else (0x4000 if self.maintenance else self.app_flash),
                'protected_prefix_unchanged': True,
                'main_app_unchanged_during_maintenance': True if self.maintenance else None}
