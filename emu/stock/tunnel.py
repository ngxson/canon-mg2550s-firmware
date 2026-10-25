"""Hardware-in-the-loop: forward selected stock MMIO accesses to the real board.

The stock firmware runs here; accesses inside the forwarded address ranges are
sent over TCP to tools/tunnel_proxy.py on the host, which performs them on the
printer through the replacement firmware's register tunnel (allowlisted, writes
locked unless the proxy was started with --unlock). Reads return the board's
value; writes are forwarded when `forward_writes` is set, otherwise recorded
and applied to the model only. Everything else stays modeled, including the
interrupt controller: our firmware runs with interrupts masked, so stock's VIC
writes never reach the board. Every forwarded access is logged with its PC,
task and round-trip time. A board answer is a hardware observation of the
unmodified ASIC; the sequence of accesses is still stock's choice.
"""
import socket, time

from .boot import BootTrace
from . import startup as E
from unicorn.arm_const import UC_ARM_REG_PC


def parse_ranges(text):
    out = []
    for item in text.split(','):
        if not item: continue
        lo, hi = item.split('-'); out.append((int(lo, 16), int(hi, 16)))
    return out


class Proxy:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=10); self.f = self.sock.makefile('rwb')
        self.ask('P')

    def ask(self, line):
        self.f.write((line + '\n').encode()); self.f.flush()
        return self.f.readline().decode().strip()

    def read32(self, a):
        r = self.ask('R %x 4' % a)
        if r == 'E' or len(r) < 8: raise IOError('read refused at %x' % a)
        return int.from_bytes(bytes.fromhex(r), 'little')

    def write32(self, a, v):
        if self.ask('W %x %x' % (a, v)) != 'OK': raise IOError('write refused at %x' % a)


class TunnelTrace(BootTrace):
    def __init__(self, proxy=None, forward=(), forward_writes=False, **inputs):
        super().__init__(**inputs)
        self.proxy, self.forward, self.forward_writes = proxy, list(forward), forward_writes
        self.tunnel_log = []; self.tunnel_errors = []

    def forwarded(self, a):
        return any(lo <= a < hi for lo, hi in self.forward)

    def read(self, cpu, offset, size, user):
        a = E.PERI + offset
        if self.proxy is not None and self.forwarded(a):
            t0 = time.monotonic()
            try:
                v = self.proxy.read32(a & ~3)
                if size == 1: v = (v >> (8 * (a & 3))) & 0xff
                elif size == 2: v = (v >> (16 * ((a >> 1) & 1))) & 0xffff
                self.tunnel_log.append({'op': 'R', 'address': hex(a), 'value': hex(v), 'size': size, 'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
                                        'task': self.current_task, 'tick': self.ticks, 'ms': round((time.monotonic() - t0) * 1000, 2)})
                self.registers[a & ~3] = v if size == 4 else self.registers.get(a & ~3, 0)
                super().read(cpu, offset, size, user)        # keep the parent's bookkeeping
                return v
            except IOError as exc:
                self.tunnel_errors.append({'address': hex(a), 'error': str(exc), 'tick': self.ticks})
        return super().read(cpu, offset, size, user)

    def write(self, cpu, offset, size, value, user):
        a = E.PERI + offset
        if self.proxy is not None and self.forwarded(a):
            entry = {'op': 'W', 'address': hex(a), 'value': hex(value), 'size': size, 'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
                     'task': self.current_task, 'tick': self.ticks, 'forwarded': self.forward_writes}
            if self.forward_writes:
                t0 = time.monotonic()
                try: self.proxy.write32(a & ~3, value); entry['ms'] = round((time.monotonic() - t0) * 1000, 2)
                except IOError as exc: self.tunnel_errors.append({'address': hex(a), 'error': str(exc), 'tick': self.ticks}); entry['forwarded'] = False
            self.tunnel_log.append(entry)
        super().write(cpu, offset, size, value, user)

    def run_tunnel(self, ticks=2000, budget=60000000):
        result = self.run_boot(ticks=ticks, budget=budget)
        reads = [x for x in self.tunnel_log if x['op'] == 'R']; writes = [x for x in self.tunnel_log if x['op'] == 'W']
        result['tunnel'] = {'forward': [[hex(lo), hex(hi)] for lo, hi in self.forward], 'forward_writes': self.forward_writes,
                            'reads': len(reads), 'writes': len(writes), 'writes_forwarded': sum(1 for w in writes if w['forwarded']),
                            'errors': self.tunnel_errors[:20], 'mean_read_ms': round(sum(r['ms'] for r in reads) / len(reads), 2) if reads else None,
                            'log_head': self.tunnel_log[:400]}
        return result
