#!/usr/bin/env python3
"""Emulate the register tunnel (USB 0x90..0x94): allowlist, lock, counters.

Reads inside the allowlisted peripheral blocks return the modeled register
values; reads outside (VIC, USB core, flash, RAM) stall; writes stall while
locked, succeed in writable blocks after the unlock key, and stall in the
motor and panel blocks even when unlocked; a wrong key locks again.
"""
import json, struct, sys
from pathlib import Path
APP = Path(__file__).resolve().parents[1]; BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests'))
from model import setup  # noqa: E402
from model_printer import PrinterModel  # noqa: E402

KEY = 0x7d4e


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes(); app = (APP / 'build/application.bin').read_bytes()
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]}, 'symbols': {'bootloader': bs['bootloader'], 'application': asym}}
    e = PrinterModel(image=(boot + app).ljust(0x200000, b'\xff'), metadata=md)
    e.regs[0xc0c84e08] = 1; e.regs[0xc0c29030] = 0x5; e.regs[0xc0c51100] = 0x12340
    e.run(lambda x: bool(x.info()) and x.info()[2] == 2)

    def command(name, req, value=0, index=0, expect_stall=False):
        n = sum(1 for _, nm, _ in e.responses if nm == name); st = len(e.stalls)
        e.requests.appendleft((name, setup(0x40, req, value, index)))
        e.run(lambda x, n=n, st=st: sum(1 for _, nm, _ in x.responses if nm == name) > n or len(x.stalls) > st, resume=True)
        assert (len(e.stalls) > st) == expect_stall, (name, e.stalls)

    def read(name, length, expect_stall=False):
        n = sum(1 for _, nm, _ in e.responses if nm == name); st = len(e.stalls)
        e.requests.appendleft((name, setup(0xc0, 0x91, 0, 0, length)))
        e.run(lambda x, n=n, st=st: sum(1 for _, nm, _ in x.responses if nm == name) > n or len(x.stalls) > st, resume=True)
        assert (len(e.stalls) > st) == expect_stall, (name, e.stalls)
        return None if expect_stall else [d for _, nm, d in e.responses if nm == name][-1]

    def status():
        n = sum(1 for _, nm, _ in e.responses if nm == 'tstat')
        e.requests.appendleft(('tstat', setup(0xc0, 0x94, 0, 0, 28)))
        e.run(lambda x, n=n: sum(1 for _, nm, _ in x.responses if nm == 'tstat') > n, resume=True)
        return struct.unpack('<7I', [d for _, nm, d in e.responses if nm == 'tstat'][-1])

    def addr(a): command('addr', 0x90, a & 0xffff, a >> 16)

    addr(0xc0c84e08); d = read('pe', 4); assert struct.unpack('<I', d)[0] == 1
    addr(0xc0c29030); d = read('he', 8); assert struct.unpack('<2I', d)[0] == 5
    addr(0xc0c51100); d = read('cr', 4); assert struct.unpack('<I', d)[0] == 0x12340
    for name, a in (('vic', 0xc0130000), ('usb', 0xc0780014), ('flash', 0x10000), ('ram', 0x20020000), ('edge', 0xc0c29ffc + 4)):
        addr(a); read(name, 4, expect_stall=True)
    addr(0xc0c29ffc); read('last_word', 4); read('over_edge', 8, expect_stall=True)
    print('PASS reads and allowlist')
    addr(0xc0c29b10); command('w_locked', 0x92, 0x63, 0, expect_stall=True)
    command('unlock', 0x93, KEY); command('w_head', 0x92, 0x63, 0); assert e.regs.get(0xc0c29b10) == 0x63
    addr(0xc0c50008); command('w_motor', 0x92, 2400, 0, expect_stall=True); assert e.regs.get(0xc0c50008) != 2400
    addr(0xc0c8d140); command('w_panel', 0x92, 1, 0, expect_stall=True)
    addr(0xc0c84604); command('w_gpio', 0x92, 1, 0); assert e.regs.get(0xc0c84604) == 1
    command('lock', 0x93, 1); addr(0xc0c29b14); command('w_relocked', 0x92, 1, 0, expect_stall=True)
    s = status(); assert s[0] == 0x4e55544d and s[3] == 4 and s[4] == 2 and s[5] >= 8 and s[6] == 0, s
    print('PASS writes, lock and counters', s[3:7])
    assert not e.erases and not e.programs
    (APP / 'build/validation_tunnel.json').write_text(json.dumps({'assumptions': __doc__, 'status': list(s), 'all_assertions_passed': True}, indent=2) + '\n')
    print('ALL TUNNEL ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
