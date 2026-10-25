"""Panel key injection after stock boot reaches idle (black-box experiment).

Stock task 72 polls the panel key register 0xc0c8d028 during the modeled boot
(the register the replacement firmware's buttons.c reads; Cancel 0x20, Copy B/W
0x400, Copy Colour 0x1000 were physically identified there). This model ORs a
caller-chosen bit mask into that register's value between two ticks and records
what the unmodified firmware does afterwards: new code ranges, panel/LED writes,
USB-core writes, motor writes, RTOS event posts and mailbox receives, and the
next peripheral wall. No instruction, RAM flag or message is patched. Whether a
given bit is a physical key is not established by this model; a reaction is.
"""
from collections import Counter

from .boot import BootTrace
from . import startup as E
from unicorn.arm_const import UC_ARM_REG_PC

KEY_REG = 0xc0c8d028
PANEL_LO, PANEL_HI = 0xc0c8d000, 0xc0c8e000
USB_CORE_LO, USB_CORE_HI = 0xc0780000, 0xc0790000
USB_MISC = {0xc0c88024, 0xc0c880a8, 0xc0c88058, 0xc0c88060, 0xc0c88064, 0xc0c88078, 0xc07f0194}


class KeyPress(BootTrace):
    def __init__(self, key_mask=0, key_tick=200, key_ticks=40, key_idle=0, **inputs):
        super().__init__(**inputs)
        self.key_mask, self.key_tick, self.key_release = key_mask, key_tick, key_tick + key_ticks
        self.key_idle = key_idle              # bits always present (the real board reads 0x8 at rest)
        # Key-event latch word 0xc0c8d02c: the board reads 0x48 at rest and 0x68 from the first
        # press on (sticky). latch_mode: None = modeled as written (0), 'sticky', 'pressed'.
        self.latch_mode = None; self.latch_idle = 0x48; self.latch_bit = 0x20; self.latched = False
        self.key_reads_pressed = 0
        self.pcs_before = None
        self.writes_before = 0
        self.points_before = 0

    def read(self, cpu, offset, size, user):
        value = super().read(cpu, offset, size, user)
        if E.PERI + offset == KEY_REG:
            value |= self.key_idle
            if self.key_tick <= self.ticks < self.key_release: value |= self.key_mask; self.key_reads_pressed += 1; self.latched = True
        if E.PERI + offset == KEY_REG + 4 and self.latch_mode:
            value = self.latch_idle
            pressed = self.key_tick <= self.ticks < self.key_release
            if (self.latch_mode == 'sticky' and self.latched) or (self.latch_mode == 'pressed' and pressed): value |= self.latch_bit
        return value

    def inject(self):
        if self.ticks == self.key_tick and self.pcs_before is None:
            self.pcs_before = set(self.boot_pcs); self.writes_before = len(self.mmio_writes)
            self.points_before = len(self.boot_points)
        super().inject()

    def run_keys(self, ticks=2000, budget=60000000):
        result = self.run_boot(ticks=ticks, budget=budget)
        after = self.mmio_writes[self.writes_before:]
        new = set(self.boot_pcs) - (self.pcs_before or set())
        kinds = Counter()
        for w in after:
            a = int(w['address'], 16)
            if PANEL_LO <= a < PANEL_HI: kinds['panel'] += 1
            elif USB_CORE_LO <= a < USB_CORE_HI or a in USB_MISC: kinds['usb'] += 1
            elif 0xc0c50000 <= a < 0xc0c58000: kinds['motor'] += 1
            elif 0xc0c20000 <= a < 0xc0c2a000: kinds['head'] += 1
        posts = [p for p in self.boot_points[self.points_before:] if p['pc'] == '0x106bf0']
        receives = [p for p in self.boot_points[self.points_before:] if p['pc'] == '0x107286']
        result['key_press'] = {
            'mask': hex(self.key_mask), 'idle': hex(self.key_idle), 'latch': self.latch_mode, 'tick': self.key_tick, 'release': self.key_release,
            'reads_while_pressed': self.key_reads_pressed,
            'new_pc_count_after_press': len(new),
            'new_pc_ranges_after_press': [[hex(lo), hex(hi)] for lo, hi in _ranges(sorted(new))][:40],
            'writes_after_press': dict(kinds), 'total_writes_after_press': len(after),
            'panel_writes_after_press': [(w['pc'], w['address'], w['value'], w['tick']) for w in after
                                         if PANEL_LO <= int(w['address'], 16) < PANEL_HI][:40],
            'usb_writes_after_press': [(w['pc'], w['address'], w['value'], w['tick']) for w in after
                                       if USB_CORE_LO <= int(w['address'], 16) < USB_CORE_HI or int(w['address'], 16) in USB_MISC][:40],
            'event_posts_after_press': sorted(Counter((p['r0_r3'][0], p['r0_r3'][1]) for p in posts).items()),
            'mailbox_receives_after_press': sorted(Counter((p['task'], p['r0_r3'][0]) for p in receives).items()),
            'tasks_seen_after_press': sorted(set(p['task'] for p in self.boot_points[self.points_before:])),
            'next_wall': getattr(self, 'next_wall', None),
        }
        return result


def _ranges(pcs):
    out = []
    for pc in pcs:
        if out and pc - out[-1][1] <= 4: out[-1][1] = pc + 2
        else: out.append([pc, pc + 2])
    return out
