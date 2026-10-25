"""Head work-engine model for the stationary prefire tests (src/head_fire.c).

Extends the cartridge/head-interface model with the stock work engine as the
briefs describe its software contract: a write of 1 to 0xc0c29af0 arms the
work; the engine reports completion by setting bit 0 of 0xc0c29030, which the
firmware polls (stock: IRQ 0x2f). The model raises the bit after a few status
reads, or never when `never_complete` is set, and snapshots the lane mask,
count registers, waveform banks and interface state at each completion. It
keeps an ordered trace of every write to the head engine block
(0xc0c29000..0xc0c29fff), the head-interface controls and the two CR work
words (0xc0c51d14/1c) so a test can compare the firmware's register sequence
against the brief, step by step. It also hosts the scripted carriage used by
the carriage tests, so the firmware can home before firing.

Synthetic timing and readiness only: this is the software contract, not an
electrical model of the head, and it says nothing about drops or heater energy.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'bootloader' / 'tests'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from model import checked, TICKS_PER_MS            # noqa: E402
from model_cartridge import CartridgeModel         # noqa: E402
from stock.head_interface import HEAD_CONTROLS, CTL_ACQ  # noqa: E402
from model_carriage import CarriageMixin, CR_C, CR_POS, TRAVEL  # noqa: E402

HE_STATUS, HE_CTRL, HE_ARM, HE_SELECT, HE_INTERVAL = 0xc0c29030, 0xc0c29034, 0xc0c29af0, 0xc0c29ae0, 0xc0c29ae4
HE_UNION, HE_COUNT0 = 0xc0c29aa0, 0xc0c29b10
CR_WORK = (0xc0c51d14, 0xc0c51d1c)
ENGINE_LO, ENGINE_HI = 0xc0c29000, 0xc0c2a000


class HeadModel(CarriageMixin, CartridgeModel):
    def __init__(self, *a, never_complete=False, complete_after=4, **k):
        super().__init__(*a, **k)
        self.cr_init()
        self.trace = []            # ordered (addr, value): engine, controls, CR work words (application stage)
        self.fires = []            # snapshot per completion
        self.arms = []             # dict per 29af0=1 write
        self.never_complete = never_complete; self.complete_after = complete_after
        self.work_armed = False; self.polls = 0

    @checked
    def write(self, cpu, offset, size, value, user):
        a = 0xc0000000 + offset
        if self.stage == 'application' and (ENGINE_LO <= a < ENGINE_HI or a in CR_WORK or a in HEAD_CONTROLS):
            self.trace.append((a, value))
        self.cr_write(a, value)
        if a == HE_ARM:
            if value == 1:
                self.work_armed = True; self.polls = 0
                self.arms.append(dict(ctrl=self.regs.get(HE_CTRL, 0), acquired=self.regs.get(CTL_ACQ) == 1,
                                      controls=self.head.controls_match(), interface=self.interface_ready()))
            else:
                self.work_armed = False
        return super().write(cpu, offset, size, value, user)

    def snapshot(self):
        r = self.regs.get
        return dict(mask=r(HE_SELECT, 0), union=r(HE_UNION, 0), interval=r(HE_INTERVAL, 0),
                    counts=[r(HE_COUNT0 + 4 * i, 0) for i in range(10)],
                    bk=[r(0xc0c29850 + 4 * j, 0) for j in range(16)], bk_tail=r(0xc0c295dc, 0),
                    lcl=[r(0xc0c29700 + 4 * j, 0) for j in range(16)], lcl_tail=r(0xc0c295d0, 0),
                    scl=[r(0xc0c297e0 + 4 * j, 0) for j in range(16)], scl_tail=r(0xc0c295d8, 0),
                    rec18=r(0xc0c291e0, 0), rec1c=r(0xc0c291e8, 0), sel_b=r(0xc0c29ac0, 0),
                    interface=self.interface_ready(), acquired=r(CTL_ACQ) == 1,
                    missing=[hex(a) for a in self.head.missing_config()], controls=self.head.controls_match(),
                    commit=r(0xc0c29608, 0), x=self.cr_x())

    @checked
    def read(self, cpu, offset, size, user):
        a = 0xc0000000 + offset
        if a == HE_STATUS:
            if self.work_armed and not self.never_complete:
                self.polls += 1
                if self.polls >= self.complete_after:
                    self.regs[a] = self.regs.get(a, 0) | 1
                    self.work_armed = False
                    self.fires.append(self.snapshot())
            return self.regs.get(a, 0)
        cr = self.cr_read(a)
        if cr is not None: return cr
        if a == 0xc0c82008 and self.stage == 'application':
            self.counter = (self.counter + TICKS_PER_MS // 4) & 0xffffffff
            return self.counter & 0x1fffffff
        return super().read(cpu, offset, size, user)
