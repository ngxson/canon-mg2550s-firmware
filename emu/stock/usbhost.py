"""USB host model for the stock trace, ported from the replacement firmware's test model.

The replacement firmware's suites drive the same USB device core (base
0xc0780000) with a scripted host (bootloader/tests/model.py): bus reset and
enumeration-done events in the core interrupt status (+0x14, masked by +0x18),
SETUP packets delivered into the buffer the firmware arms on endpoint 0 OUT,
IN data read from the buffer the firmware arms on endpoint 0 IN, the status
stages, the address load check (DCFG +0x800), and bulk OUT data to an armed
endpoint. Those register semantics come from our own hardware-proven driver,
not from stock code. This class applies them to the unmodified stock firmware:
the host attaches when stock clears the soft-disconnect bit (+0x804 bit 1),
enumerates with the standard requests, parses the configuration descriptor
stock returns to find its bulk endpoints, and, if a BJL file is given, streams
it to the bulk OUT endpoint. If `usb_source` is given, that VIC source is
injected whenever masked core events are pending, so an interrupt-driven
driver sees them; without it the events are only visible to polling.

Everything the firmware returns (descriptors, device ID, stalls) is recorded as
data. Reaching a given stage proves only that the modeled host and the stock
driver agree up to there.
"""
from collections import deque
import struct

from .boot import BootTrace
from .scheduler import TickScheduler
from . import startup as E
from unicorn import UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_PC

USB = 0xc0780000
ENABLE = 0x80000000


def setup(kind, request, value=0, index=0, length=0):
    return struct.pack('<BBHHH', kind, request, value, index, length)


class UsbHost(BootTrace):
    def __init__(self, usb_source=None, bjl=b'', speed=True, usb_force=False, usb_attach=None, **inputs):
        super().__init__(**inputs)
        self.usb_source, self.bjl, self.speed, self.usb_force = usb_source, bjl, speed, usb_force
        self.usb_attach = usb_attach          # tick at which the host attaches even if stock keeps soft-disconnect (experiment)
        self.usb_mask = None; self.last_injected = None; self.usb_reads = []; self.variant = set()
        self.max_packet = 512 if speed else 64
        self.armed = set(); self.connected = False; self.generation = -1
        self.bus_stage = 0; self.requests = deque(); self.current = None
        self.responses = []; self.stalls = []; self.configured = False
        self.bulk_position = 0; self.bulk_out_ep = None; self.bulk_in_ep = None
        self.usb_events = []; self.usb_irqs = 0; self.address_loaded = None
        self.descriptors = {}
        self.irq_ram_writes = []; self.irq_window_until = -1; self.irq_index = 0
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self.ram_write, begin=0x18000000, end=0x187fffff)

    def ram_write(self, cpu, access, address, size, value, user):
        if self.instructions <= self.irq_window_until:
            self.irq_ram_writes.append({'irq': self.irq_index, 'i': self.instructions, 'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
                                        'address': hex(address), 'size': size, 'value': hex(value), 'task': self.current_task})

    # --- register semantics (identical to bootloader/tests/model.py) ---
    def reg(self, offset): return self.registers.get(USB + offset, 0)
    def setreg(self, offset, value): self.registers[USB + offset] = value
    def endpoint(self, direction, number): return (0x900 if direction == 'in' else 0xb00) + number * 32

    def ready(self, direction, number, interrupt=1):
        block = self.endpoint(direction, number)
        mask = (0x840 if direction == 'in' else 0x880) + number * 4
        return ((direction, number) in self.armed and self.reg(mask) & interrupt
                and not self.reg(block + 8) and (interrupt == 8 or not self.reg(block) & 0x20000))

    def transfer(self, direction, number, data=b'', is_setup=False):
        block = self.endpoint(direction, number)
        address, capacity = self.reg(block + 0x14), self.reg(block + 0x10) & 0x7ffff
        if direction == 'in':
            data = self.buffer_read(address, capacity) if capacity else b''
        else:
            data = data[:capacity]
            if data: self.buffer_write(address, data)
        size = self.reg(block + 0x10)
        packets = (size >> 19) & 0x3ff; supcnt = (size >> 29) & 3
        size = (size & ~0x7ffff & ~(0x3ff << 19) & ~(3 << 29)) | (capacity - len(data))
        size |= max(packets - 1, 0) << 19                                 # packet count decrements per packet
        size |= (max(supcnt - 1, 0) if (is_setup and 'nosupdec' not in self.variant) else supcnt) << 29
        self.setreg(block + 0x10, size); self.setreg(block + 0x14, address + len(data))
        self.setreg(block, self.reg(block) & ~ENABLE)
        self.setreg(block + 8, (1 if 'setup1' in self.variant else 9) if is_setup else 1)   # variant: SETUP as transfer-complete only
        self.summarize()
        self.armed.discard((direction, number))
        self.usb_events.append({'tick': self.ticks, 'event': 'transfer', 'dir': direction, 'ep': number,
                                'bytes': len(data), 'setup': is_setup})
        return data

    # Stock's endpoint buffers can live in the controller's own address space (observed:
    # the SETUP buffer at 0xc07e0640), which the emulator maps as MMIO; those bytes are
    # kept in the register map so the firmware's word reads see them.
    def buffer_read(self, address, n):
        if address < E.PERI: return bytes(self.uc.mem_read(address, n))
        out = bytearray()
        for i in range(0, n, 4): out += self.registers.get((address + i) & ~3, 0).to_bytes(4, 'little')
        return bytes(out[:n])

    def buffer_write(self, address, data):
        if address < E.PERI: self.uc.mem_write(address, data); return
        padded = data + b'\0' * (-len(data) % 4)
        for i in range(0, len(padded), 4): self.registers[(address + i) & ~3] = int.from_bytes(padded[i:i + 4], 'little')

    def summarize(self):
        """Endpoint summary bits as our driver's mask (0xc3000) names them: core status bit 18
        for IN endpoints, bit 19 for OUT endpoints, with the per-endpoint bitmap at +0x818
        (IN n = bit n, OUT n = bit 16+n), derived from the per-endpoint status words."""
        daint = 0
        for n in range(16):
            if self.reg(0x900 + 32 * n + 8): daint |= 1 << n
            if self.reg(0xb00 + 32 * n + 8): daint |= 1 << (16 + n)
        self.setreg(0x818, daint)
        # Stock's handler reads +0x838/+0x83c for the endpoint bitmap (observed), not +0x818;
        # both mirror the same bitmap here until the split is established.
        self.setreg(0x838, 0 if 'outonly' in self.variant else daint); self.setreg(0x83c, daint)
        status = self.reg(0x14) & ~0xc0000
        if daint & 0xffff: status |= 0x40000
        if daint >> 16: status |= 0x80000
        self.setreg(0x14, status)

    def attach(self):
        self.generation += 1; self.bus_stage = 0; self.current = None; self.configured = False
        self.usb_events.append({'tick': self.ticks, 'event': 'attach', 'generation': self.generation})
        self.requests = deque([
            ('address', setup(0, 5, 7)),
            ('device', setup(0x80, 6, 0x100, length=18)),
            ('config', setup(0x80, 6, 0x200, length=255)),
            ('string0', setup(0x80, 6, 0x300, length=255)),
            ('configure', setup(0, 9, 1)),
            ('device_id', setup(0xa1, 0, 0, 0, length=1024)),
        ])

    def finish(self):
        request = self.current; data = bytes(request['data'])
        self.responses.append((self.generation, request['name'], data.hex()))
        self.usb_events.append({'tick': self.ticks, 'event': 'response', 'name': request['name'], 'bytes': len(data)})
        if request['name'] == 'address': self.address_loaded = (self.reg(0x800) & 0x7f0) == 0x70
        if request['name'] in ('device', 'config', 'string0', 'device_id'): self.descriptors[request['name']] = data.hex()
        if request['name'] == 'config': self.parse_config(data)
        if request['name'] == 'configure': self.configured = True
        self.current = None

    def parse_config(self, data):
        i, eps = 0, []
        while i + 1 < len(data) and data[i]:
            if data[i + 1] == 5 and i + 4 < len(data):
                eps.append((data[i + 2], data[i + 3] & 3))
            i += data[i]
        self.descriptors['endpoints'] = [(hex(a), t) for a, t in eps]
        outs = [a for a, t in eps if t == 2 and not a & 0x80]; ins = [a for a, t in eps if t == 2 and a & 0x80]
        self.bulk_out_ep = outs[0] if outs else None; self.bulk_in_ep = ins[0] & 0x7f if ins else None

    def host_step(self):
        if not self.connected or (self.reg(0x804) & 2 and self.usb_attach is None): return
        if self.reg(0x14) or any(self.reg(b + 8) for b in (0x900, 0xb00)): return
        if self.bus_stage < 2:
            self.setreg(0x14, (0x1000, 0x2000)[self.bus_stage]); self.bus_stage += 1
            self.usb_events.append({'tick': self.ticks, 'event': ('bus reset', 'enumeration done')[self.bus_stage - 1]})
            return
        if self.current:
            request = self.current
            if self.reg(0x900) & 0x200000:
                self.stalls.append(request['name']); self.usb_events.append({'tick': self.ticks, 'event': 'stall', 'name': request['name']})
                self.current = None; return
            if request['phase'] == 'in' and self.ready('in', 0):
                data = self.transfer('in', 0); request['data'].extend(data)
                if not request['setup'][0] & 0x80: self.finish()
                elif len(data) < 64 or len(request['data']) >= request['length']: request['phase'] = 'status'
            elif request['phase'] == 'status' and self.ready('out', 0):
                self.transfer('out', 0); self.finish()
            return
        if self.requests and self.ready('out', 0, 8):
            name, packet = self.requests.popleft()
            self.current = {'name': name, 'setup': packet, 'data': bytearray(), 'phase': 'in',
                            'length': struct.unpack_from('<H', packet, 6)[0]}
            self.transfer('out', 0, packet, True); return
        if self.configured and self.bulk_out_ep is not None and self.bulk_position < len(self.bjl) \
                and self.ready('out', self.bulk_out_ep):
            chunk = self.bjl[self.bulk_position:self.bulk_position + self.max_packet]
            self.transfer('out', self.bulk_out_ep, chunk); self.bulk_position += len(chunk)

    def read(self, cpu, offset, size, user):
        address = E.PERI + offset
        if address == USB + 0x14: self.host_step()
        value = super().read(cpu, offset, size, user)
        if address == USB + 0x10: value = (value & ~0x31) | ENABLE
        if address == USB + 0x808: value = 0 if self.speed else 2
        if (USB <= address < USB + 0x2000 or 0xc07e0000 <= address < 0xc07f0000) and self.connected:
            self.usb_reads.append({'i': self.instructions, 'tick': self.ticks, 'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
                                   'address': hex(address), 'value': hex(value), 'task': self.current_task})
        return value

    def write(self, cpu, offset, size, value, user):
        address = E.PERI + offset; off = address - USB
        if 0 <= off < 0x1000:
            if off == 0x14 or (0x900 <= off < 0xd00 and off % 32 == 8):
                self.setreg(off, self.reg(off) & ~value)
                if off != 0x14: self.summarize()
                self.mmio_writes.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)), 'address': hex(address), 'value': hex(value),
                                         'size': size, 'task': self.current_task, 'tick': self.ticks,
                                         'instruction': self.instructions, 'phase': self.phase})
                return
            if 0x900 <= off < 0xd00 and off % 32 == 0:
                direction = 'in' if off < 0xb00 else 'out'; number = (off - (0x900 if direction == 'in' else 0xb00)) // 32
                if value & 0x40000000: value &= ~ENABLE; self.armed.discard((direction, number))
                elif value & ENABLE: self.armed.add((direction, number)); self.usb_events.append({'tick': self.ticks, 'event': 'arm', 'dir': direction, 'ep': number})
                else: self.armed.discard((direction, number))
                if value & 0x08000000: value |= 0x20000
                if value & 0x04000000: value &= ~0x20000
                value &= ~0x4c000000
        super().write(cpu, offset, size, value, user)
        if off == 0x804:
            if value & 2: self.connected = False; self.usb_events.append({'tick': self.ticks, 'event': 'soft disconnect'})
            elif not self.connected: self.connected = True; self.attach()

    def inject(self):
        if self.usb_attach is not None and self.ticks == self.usb_attach and not self.connected:
            self.connected = True; self.attach(); self.usb_events.append({'tick': self.ticks, 'event': 'forced attach'})
            if self.usb_mask is not None:
                self.setreg(0x18, self.usb_mask); self.usb_events.append({'tick': self.ticks, 'event': 'mask preset', 'value': hex(self.usb_mask)})
        pending = self.reg(0x14) & (0xffffffff if self.usb_force else self.reg(0x18))
        if pending != self.last_injected: self.last_injected = None       # status changed: eligible again
        if self.usb_source is not None and self.connected and pending and self.last_injected is None and \
                (self.usb_force or self.enabled[self.group_for(self.usb_source)] & self.mask_for(self.usb_source)):
            saved = (self.source, self.force)
            self.source, self.force = self.usb_source, self.usb_force
            TickScheduler.inject(self); self.timer_pending = False; self.usb_irqs += 1; self.last_injected = pending
            self.irq_index = self.usb_irqs; self.irq_window_until = self.instructions + 4000
            self.source, self.force = saved
            return
        self.host_step()
        super().inject()

    def run_usb(self, ticks=2000, budget=60000000):
        result = self.run_boot(ticks=ticks, budget=budget)
        result['usb'] = {
            'source': self.usb_source, 'irqs': self.usb_irqs, 'connected': self.connected, 'generation': self.generation,
            'bus_stage': self.bus_stage, 'configured': self.configured, 'address_loaded': self.address_loaded,
            'responses': self.responses, 'stalls': self.stalls, 'descriptors': self.descriptors,
            'bulk_bytes_sent': self.bulk_position, 'bulk_total': len(self.bjl),
            'events': self.usb_events[:200], 'armed_now': sorted(self.armed), 'reads': self.usb_reads[:600], 'irq_ram_writes': self.irq_ram_writes[:3000],
            'core_regs': {hex(a): hex(v) for a, v in self.registers.items() if USB <= a < USB + 0x1000},
        }
        return result
