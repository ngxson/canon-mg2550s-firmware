"""Scripted print-carriage mechanism shared by the carriage, head and stamp suites.

Position follows the drive command with friction and momentum: from rest the
command must exceed `static_pct` of neutral to start (the 30 % unseat seen on
the bench), once rolling it must stay above `kinetic_pct` or the carriage
coasts to a stop, and the speed lags the command with a first-order time
constant of about 10 ms so a move coasts a little after the command returns
to neutral. Speeds are of the order of the bench observations (about 60 to
100 counts per millisecond at 18 to 30 %). Counts step by 64 as on the ASIC.
"""
CR_C, CR_POS, TRAVEL = 0xc0c50000, 0xc0c51100, 0x1d000


class CarriageMixin:
    def cr_init(self, start=60000, frozen=False, static_pct=28, kinetic_pct=2):   # 2 %: the sweep controller crawls at 10 k counts/s on hardware with an effort of about 3 %
        self.wall = 0x40000; self.pos = self.wall + start; self.frozen = frozen; self.cmds = []
        self.static = static_pct * 20; self.kinetic = kinetic_pct * 20; self.rolling = False; self.v = 0.0

    def cr_write(self, a, value):
        if a == CR_POS: self.wall = value - (self.pos - self.wall); self.pos = value
        if a == 0xc0c50008 and self.regs.get(CR_C) == 0x102: self.cmds.append(value)

    def cr_read(self, a):
        """Return the counter value for CR_POS reads, else None."""
        if a != CR_POS: return None
        if self.regs.get(CR_C) == 0x102 and not self.frozen:
            effort = self.regs.get(0xc0c50008, 2000) - 2000
            mag, sign = abs(effort), (1 if effort > 0 else -1)
            if not self.rolling and mag >= self.static: self.rolling = True
            target = sign * max(4, (mag - self.kinetic) / 10) if (self.rolling and mag >= self.kinetic) else 0.0
            self.v += (target - self.v) / 40
            if abs(self.v) < 0.5 and target == 0: self.v = 0.0; self.rolling = False
            self.pos = max(self.wall, min(self.wall + TRAVEL, self.pos + int(round(self.v))))
        else:
            self.rolling = False; self.v = 0.0
        return (self.pos & 0x7ffff) & ~63

    def cr_x(self): return self.pos - self.wall
