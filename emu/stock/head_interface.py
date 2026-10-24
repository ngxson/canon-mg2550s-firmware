"""Empirical cartridge-read contract shared by stock and replacement tests.

Evidence: app/evidence/CARTRIDGE.md, success with the full fixed 0xc3096
configuration, after the 295xx subset alone still returned stuck-low sense.
This accepts the recovered configuration as a GROUP. It does not establish
that every word/value is individually necessary, model analog timing, or
measure power. Fuse contents and the optional response fault are test inputs.
"""
ENABLE, SELECT, PAIR, TRIGGER, SENSE, FUSECUT = (
    0xc0c2954c, 0xc0c29540, 0xc0c29550, 0xc0c29544, 0xc0c84618, 0xc0c84624)
CTL_ACQ, CTL_COLD, CTL_RES = 0xc0c84604, 0xc0c84654, 0xc0c84664
BANK_ROUTE, BANK_CFG = 0xc0c812c0, 0xc0c812c4
CONN_IN = {0xc0c84638: 0xc0c85e04, 0xc0c84648: 0xc0c85e14}
HEAD_CONTROLS = (CTL_ACQ, CTL_COLD, CTL_RES, BANK_ROUTE, BANK_CFG)

# Stock ROM-derived fixed words; 291d0 is compared by its observed read mask.
FUSE_CONFIG = {
    0xc0c29588: 0, 0xc0c29590: 0x14040007, 0xc0c29598: 0x700,
    0xc0c295a0: 0x1f07000b, 0xc0c295a8: 0x07010002,
}
DRIVE_CONFIG = {
    0xc0c29d4c: 0, 0xc0c291d0: 0x3fc, 0xc0c29050: 0xff,
    0xc0c29060: 7, 0xc0c29070: 0x02022033, 0xc0c290d0: 0x58,
    0xc0c290b0: 0xfc, 0xc0c290f0: 0x04040506, 0xc0c290f4: 0x02020303,
    0xc0c29130: 0x70701, 0xc0c29138: 0x70701, 0xc0c2913c: 0x70700,
    0xc0c29150: 8, 0xc0c29154: 0xfc, 0xc0c29158: 0x1c,
    0xc0c29160: 0xa, 0xc0c29168: 0xa, 0xc0c29174: 0x00121c12,
    0xc0c29184: 0,
}
HEAD_CONFIG = {**FUSE_CONFIG, **DRIVE_CONFIG}
# These additional profile words were present in the successful hardware run,
# but have not been isolated as fuse prerequisites. Record, do not require them.
PROFILE_CONFIG = {0xc0c29580: 1, 0xc0c29584: 0, 0xc0c295f0: 0}
RESET_VALUES = {a: 0 for a in (*HEAD_CONFIG, *PROFILE_CONFIG)}
RESET_VALUES.update({0xc0c29590: 7, 0xc0c29598: 0x700,
                     0xc0c295a0: 0x70000, 0xc0c295a8: 0x07000000,
                     0xc0c29130: 0x70707, 0xc0c29138: 0x70707,
                     0xc0c2913c: 0x70707, 0xc0c29150: 0xb,
                     0xc0c29174: 0x0012180b})
SELECTOR_BASE = {0: (0x641, 0x643, 0x541, 0x543, 0x641, 0x643),
                 1: (0x441, 0x443, 0x343, 0x341, 0x243, 0x241)}


def selector(head, bit):
    return SELECTOR_BASE[head][bit // 8] + 0x10 * (bit % 8)


SELECTOR_MAP = {}
for _head in (0, 1):
    for _bit in range(48):
        SELECTOR_MAP.setdefault(selector(_head, _bit), (_head, _bit))


class HeadInterface:
    def __init__(self, registers, fuse=None, response_enabled=True,
                 heads_connected=(True, True), reset_defaults=True):
        self.regs = registers
        self.fuse = dict({0: 0, 1: 0} if fuse is None else fuse)
        self.response_enabled = response_enabled
        self.heads_connected = heads_connected
        self.triggers, self.config_writes = [], []
        if reset_defaults:
            self.regs.update(RESET_VALUES)
            self.regs[SENSE] = 0

    def missing_config(self):
        return [a for a, value in HEAD_CONFIG.items() if self.regs.get(a) != value]

    def controls_match(self):
        return (self.regs.get(CTL_ACQ) == self.regs.get(CTL_COLD) ==
                self.regs.get(CTL_RES) == 1 and bool(self.regs.get(BANK_CFG, 0) & 1)
                and not self.regs.get(BANK_ROUTE, 0) & 1)

    def write(self, address, size, value):
        self.regs[address] = value
        if address == 0xc0c291d0:
            self.regs[address] = value & 0x3ff  # observed readback, not inferred width
        if address in HEAD_CONFIG or address in PROFILE_CONFIG:
            self.config_writes.append((address, size, value))
        if address != TRIGGER or not value & 1:
            return None
        sel = self.regs.get(SELECT, 0)
        pair = self.regs.get(PAIR, 0)
        mode = self.regs.get(ENABLE, 0)
        missing = self.missing_config()
        selected = SELECTOR_MAP.get(sel)
        reasons = []
        if mode != 1: reasons.append('transaction_mode')
        if pair != 0: reasons.append('nonzero_pair')
        if not self.controls_match(): reasons.append('interface_controls')
        if missing: reasons.append('head_configuration')
        if not self.response_enabled: reasons.append('injected_response_fault')
        if selected and not self.heads_connected[selected[0]]:
            reasons.append('disconnected_head')
        if sel and selected is None: reasons.append('unknown_selector')
        sense = 0
        if selected and not reasons:
            head, bit = selected
            sense = 1 - ((self.fuse[head] >> bit) & 1)
        self.regs[SENSE] = sense
        # Hardware read 0 after the trigger. Self-clear versus write-only is
        # unresolved; this model provides zero readback without inventing timing.
        self.regs[TRIGGER] = 0
        event = dict(selector=sel, pair=pair, mode=mode, selected=selected,
                     sense=sense, blocked_by=reasons,
                     missing_config=[hex(a) for a in missing])
        self.triggers.append(event)
        return event

    def read(self, address):
        if address in CONN_IN:
            head = int(address == 0xc0c84648)
            if self.regs.get(CTL_RES) == 1 and self.heads_connected[head]:
                return 1 - (self.regs.get(CONN_IN[address], 0) & 1)
            return 1
        return self.regs.get(address, 0)
