#!/usr/bin/env python3
"""Emulate the read-only CR/LF register dump (USB 0x64).

Checks the reply layout and that answering it performs no write to the print
motor family (0xc0c50000-0xc0c58000) beyond what the app already does at boot.
Register values are the model's zero defaults; no mechanism behaviour is modeled.
"""
import json, struct, sys
from pathlib import Path
APP = Path(__file__).resolve().parents[1]; BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests'))
from model import setup  # noqa: E402
from model import checked  # noqa: E402
from model_printer import PrinterModel  # noqa: E402


class LFModel(PrinterModel):
    """Scripted LF position: moves with the command while the LF drive is enabled."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.lf_pos = 0x1000

    @checked
    def write(self, cpu, offset, size, value, user):
        if 0xc0000000 + offset == 0xc0c53100:
            self.lf_pos = value
        return super().write(cpu, offset, size, value, user)

    @checked
    def read(self, cpu, offset, size, user):
        a = 0xc0000000 + offset
        if a == 0xc0c53100:
            if self.regs.get(0xc0c52000) == 0x102:
                self.lf_pos += (self.regs.get(0xc0c52008, 2000) - 2000) // 40
            return self.lf_pos & 0x7ffff
        return super().read(cpu, offset, size, user)


def main():
    boot = (BOOT / 'build/boot_region.bin').read_bytes()
    app = (APP / 'build/application.bin').read_bytes()
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    asym = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
          'symbols': {'bootloader': bs['bootloader'], 'application': asym}}
    e = LFModel(image=(boot + app).ljust(0x200000, b'\xff'), metadata=md)
    e.run(lambda x: bool(x.info()) and x.info()[2] == 2)
    e.regs[0xc0c51100] = 0x12345; e.lf_pos = 0x6789
    before = list(e.motor_writes)
    for n in range(3):
        e.requests.appendleft((f'regs{n}', setup(0xc0, 0x64, 0, 0, 256)))
        e.run(lambda x, n=n: any(name == f'regs{n}' for _, name, _ in x.responses), resume=True)
    data = [d for _, name, d in e.responses if name.startswith('regs')]
    assert len(data) == 3 and all(d == data[0] for d in data)
    words = struct.unpack('<%dI' % (len(data[0]) // 4), data[0])
    assert words[0] == 0x5250474d and words[1] == 1 and words[2] == 41 and len(words) == 44, words
    assert words[3 + 6] == 0x12345 and words[3 + 16] == 0x6789, [hex(w) for w in words]
    assert e.motor_writes == before, 'dump wrote to the motor family'
    assert not e.erases and not e.programs
    # Motor-off counter preparation: writes only to primary blocks / routing, never an enable.
    ENABLES = (0xc0c50000, 0xc0c52000, 0xc0c56000)
    for variant in (1, 3, 7, 9):
        n0 = len(e.motor_writes)
        e.requests.appendleft((f'prep{variant}', setup(0x40, 0x65, variant)))
        e.requests.append((f'regsp{variant}', setup(0xc0, 0x64, 0, 0, 256)))
        e.run(lambda x, v=variant: any(name == f'regsp{v}' for _, name, _ in x.responses), resume=True)
        assert f'prep{variant}' not in e.stalls, e.stalls
        new = e.motor_writes[n0:]
        assert new and all(not (a in ENABLES and v) for a, v in new), new
        allowed = lambda a: (0xc0c51000 <= a < 0xc0c52000 or (variant & 4 and 0xc0c53000 <= a < 0xc0c54000)
                             or (variant & 8 and (0xc0c56000 <= a < 0xc0c58000 or 0xc0c50200 <= a < 0xc0c50220
                                                  or a in (0xc0c50004, 0xc0c50008, 0xc0c52004, 0xc0c52008, 0xc0c53000)
                                                  or a in ENABLES)))
        assert all(allowed(a) for a, v in new), [(hex(a), v) for a, v in new if not allowed(a)]
        d = [d for _, name, d in e.responses if name == f'regsp{variant}'][-1]
        w = struct.unpack('<%dI' % (len(d) // 4), d)
        assert w[3 + 3] == 0x0d and w[3 + 6] == 0xf0000, [hex(x) for x in w]
        if variant & 4: assert w[3 + 13] == 0x0d and w[3 + 16] in (0xf0000, 0x70000)   # hardware masks to 19 bits
        print('PASS prep_variant', variant, 'writes', len(new))
    e.requests.appendleft(('prep_bad', setup(0x40, 0x65, 32)))
    e.run(lambda x: 'prep_bad' in x.stalls, resume=True)
    e.regs[0xc0c50000] = 0x102
    e.requests.appendleft(('prep_refused', setup(0x40, 0x65, 1)))
    e.run(lambda x: 'prep_refused' in x.stalls, resume=True)
    e.regs[0xc0c50000] = 0
    print('PASS prep_rejections')
    # GPIO snapshot: 14 chunks (896 words), chunk 14 stalls, addresses follow the alias/bank layout.
    e.regs[0xc0c84008 + 7 * 0x200 + 5 * 0x10] = 0xa5          # group 7 slot 5
    e.regs[0xc0c81200 + 4 * 100] = 0x5a
    words = []
    for chunk in range(14):
        e.requests.appendleft((f'snap{chunk}', setup(0xc0, 0x66, chunk, 0, 256)))
        e.run(lambda x, c=chunk: any(name == f'snap{c}' for _, name, _ in x.responses), resume=True)
        d = [d for _, name, d in e.responses if name == f'snap{chunk}'][-1]
        words += struct.unpack('<%dI' % (len(d) // 4), d)
    assert len(words) == 896 and words[7 * 32 + 5] == 0xa5 and words[576 + 100] == 0x5a, len(words)
    e.requests.appendleft(('snap_end', setup(0xc0, 0x66, 14, 0, 256)))
    e.run(lambda x: 'snap_end' in x.stalls, resume=True)
    print('PASS gpio_snapshot')

    # LF jog: scripted position follows the command while enabled; sequence and bounds checked.
    for name, cmd, ms in (('lf_bad_cmd', 1300, 50), ('lf_bad_channel', 2120 | 0x3000, 50), ('cr_too_long', 2300 | 0x2000, 1001), ('lf_bad_ms', 2120, 2001), ('lf_zero_ms', 2120, 0)):
        e.requests.appendleft((name, setup(0x40, 0x67, cmd, ms)))
        e.run(lambda x, n=name: n in x.stalls, resume=True)
    e.regs[0xc0c50000] = 0x102
    e.requests.appendleft(('lf_refused_cr_on', setup(0x40, 0x67, 2120, 50)))
    e.run(lambda x: 'lf_refused_cr_on' in x.stalls, resume=True)
    e.regs[0xc0c50000] = 0
    n0 = len(e.motor_writes)
    e.requests.appendleft(('lf_jog', setup(0x40, 0x67, 2120, 50)))
    e.run(lambda x: any(name == 'lf_jog' for _, name, _ in x.responses), resume=True)
    for _ in range(200):                       # status is answered during the pulse (USB polled)
        n = sum(1 for _, name, _ in e.responses if name == 'lf_status')
        e.requests.appendleft(('lf_status', setup(0xc0, 0x68, 0, 0, 160)))
        e.run(lambda x, n=n: sum(1 for _, name, _ in x.responses if name == 'lf_status') > n,
              resume=True, budget=50000000)
        d = [d for _, name, d in e.responses if name == 'lf_status'][-1]
        if len(d) >= 12 and struct.unpack('<3I', d[:12])[2] == 1:
            break
    d = [d for _, name, d in e.responses if name == 'lf_status'][-1]
    w = struct.unpack('<%dI' % (len(d) // 4), d)
    assert w[0] == 0x4a464c4d and w[2] == 1 and w[3] == 2120 and 45 <= w[4] <= 120, [hex(x) for x in w[:8]]   # modeled clock: ~2 ms per loop
    assert w[6] > w[5] and w[7] >= 16, (w[5], w[6], w[7])
    seq = [(a, v) for a, v in e.motor_writes[n0:] if a in (0xc0c52000, 0xc0c52004, 0xc0c52008)]
    assert seq[:3] == [(0xc0c52000, 0), (0xc0c52004, 4000), (0xc0c52008, 2000)], seq
    assert seq[3:] == [(0xc0c52000, 0x102), (0xc0c52008, 2120), (0xc0c52008, 2000), (0xc0c52000, 0)], seq
    assert all(not (a == 0xc0c50000 and v) for a, v in e.motor_writes[n0:])
    assert e.regs[0xc0c52000] == 0 and e.regs[0xc0c52008] == 2000
    print('PASS lf_jog_bounded', [hex(x) for x in w[5:8]])
    # Fourth channel candidate: same bounded sequence on the 0xc0c54000/55000 block only.
    n0 = len(e.motor_writes)
    e.requests.appendleft(('ch4_jog', setup(0x40, 0x67, 2200 | 0x1000, 20)))
    e.run(lambda x: any(name == 'ch4_jog' for _, name, _ in x.responses), resume=True)
    for _ in range(200):
        n = sum(1 for _, name, _ in e.responses if name == 'lf_status')
        e.requests.appendleft(('lf_status', setup(0xc0, 0x68, 0, 0, 160)))
        e.run(lambda x, n=n: sum(1 for _, name, _ in x.responses if name == 'lf_status') > n, resume=True, budget=50000000)
        d = [d for _, name, d in e.responses if name == 'lf_status'][-1]
        if len(d) >= 16 and struct.unpack('<4I', d[:16])[2:] == (1, 2200 | 0x1000):
            break
    seq = [(a, v) for a, v in e.motor_writes[n0:] if a in (0xc0c54000, 0xc0c54008)]
    assert seq == [(0xc0c54000, 0), (0xc0c54008, 2000), (0xc0c54000, 0x102), (0xc0c54008, 2200), (0xc0c54008, 2000), (0xc0c54000, 0)], seq
    assert all(not (a in (0xc0c50000, 0xc0c52000, 0xc0c56000) and v) for a, v in e.motor_writes[n0:])
    print('PASS fourth_channel_jog_bounded')
    # Carriage channel: same bounded sequence on 0xc0c50000/51000, 200 ms cap enforced above.
    n0 = len(e.motor_writes)
    e.requests.appendleft(('cr_jog', setup(0x40, 0x67, 1700 | 0x2000, 20)))
    e.run(lambda x: any(name == 'cr_jog' for _, name, _ in x.responses), resume=True)
    for _ in range(200):
        n = sum(1 for _, name, _ in e.responses if name == 'lf_status')
        e.requests.appendleft(('lf_status', setup(0xc0, 0x68, 0, 0, 160)))
        e.run(lambda x, n=n: sum(1 for _, name, _ in x.responses if name == 'lf_status') > n, resume=True, budget=50000000)
        d = [d for _, name, d in e.responses if name == 'lf_status'][-1]
        if len(d) >= 16 and struct.unpack('<4I', d[:16])[2:] == (1, 1700 | 0x2000):
            break
    seq = [(a, v) for a, v in e.motor_writes[n0:] if a in (0xc0c50000, 0xc0c50008)]
    assert seq == [(0xc0c50000, 0), (0xc0c50008, 2000), (0xc0c50000, 0x102), (0xc0c50008, 1700), (0xc0c50008, 2000), (0xc0c50000, 0)], seq
    assert all(not (a in (0xc0c52000, 0xc0c54000, 0xc0c56000) and v) for a, v in e.motor_writes[n0:])
    print('PASS carriage_channel_jog_bounded')
    (APP / 'build/validation_printregs.json').write_text(json.dumps(
        {'words': [hex(w) for w in words], 'motor_writes_during_dump': 0, 'all_assertions_passed': True}, indent=2) + '\n')
    print('PASS print_regs_dump_read_only', [hex(w) for w in words[:5]])
    print('ALL PRINT REGS ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
