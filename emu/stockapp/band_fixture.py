#!/usr/bin/env python3
"""Band-path fixture (shape B, towards a stock-quality printed page).

Replays RE brief 8's executed recipe on the fixture harness and then goes one
step further than that brief could: with the RTOS services stubbed, publish a
firing descriptor Q and run the stock band executor on it, logging every
service call and MMIO write. Steps:

  1. adc14(job)   stock print-buffer manager allocation (black, 600 dpi, width 320)
  2. a8422(P)     stock raster worker on a synthetic 8-row packet (RB2PB setup);
                  the converter hardware is not modeled, its completion events
                  are answered by the stubs
  3. b12a2(0)     Q builder with a hand-filled band-selection matrix (group 6,
                  row 18 -> slot 0); the publication at 0xb1aa0 is captured
  4. e6b80(Q)     band dispatch/executor on that Q

Nothing here is a verified print recipe: the Q comes from a synthetic
scheduler state and the ASIC conversion did not run. The ordered MMIO of step
4 is the dynamic answer to brief 11's first question, to be checked against
the brief when it is answered.
"""
import argparse, ctypes, json, struct, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'emu'), str(ROOT / 'emu/stockapp')]
from unicorn import UC_HOOK_CODE, UC_PROT_ALL  # noqa: E402
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1  # noqa: E402
from fixture import Fixture, apply_presets, WATCH  # noqa: E402

JOB, PACKET, SOURCE = 0x10010000, 0x10020000, 0x10030000
MATRIX, R_BYTE, D_HALF = 0x1806cd04, 0x1806c86a, 0x1806cad4
PUBLISH_SITE = 0xb1aa0


def alias_high_windows(fx):
    """Brief 8 §2: 0x10100000..0x107fffff and 0x18100000..0x187fffff share RAM
    (the allocator's cached/uncached alias 0x104300/0x104308)."""
    keep = bytes(fx.uc.mem_read(0x18100000, 0x700000))
    fx.uc.mem_unmap(0x18100000, 0x700000)
    fx.shared = (ctypes.c_ubyte * 0x700000)()
    for base in (0x10100000, 0x18100000):
        fx.uc.mem_map_ptr(base, 0x700000, UC_PROT_ALL, ctypes.addressof(fx.shared))
    fx.uc.mem_write(0x18100000, keep)


def step(fx, name, address, args, budget=3_000_000):
    start_mmio = len(fx.mmio_writes)
    r = fx.call(address, args, budget=budget)
    mmio = [{'pc': w['pc'], 'address': w['address'], 'value': w['value']} for w in fx.mmio_writes[start_mmio:]]
    services = [{k: v for k, v in e.items() if k in ('service', 'lr', 'args', 'decoded', 'command', 'instruction', 'reply')} for e in r['services']]
    print(f"{name}: {r['stop_reason']} r0={r['r0']:#x} instr={r['instructions']} services={len(services)} mmio={len(mmio)}")
    if 'context switch' in r['stop_reason'] or 'loop' in r['stop_reason'] or r['error']:
        for e in r['services'][-8:]:
            print('    ', e['instruction'], e['service'], e['lr'], e['args'], e.get('return_addresses', [])[:5])
        print('     switch pcs:', r.get('switch_pcs'))
    if r['instructions'] < 2000 or r['error']:
        print('     RAM reads (pc, address, count):', r['ram_reads_top'][:16])
        print('     last pcs:', r['last_pcs'])
    return {'name': name, 'stop_reason': r['stop_reason'], 'r0': r['r0'], 'instructions': r['instructions'],
            'services': services, 'mmio_writes': mmio, 'error': r['error'], 'ram_reads_top': r['ram_reads_top'], 'last_pcs': r['last_pcs']}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--eeprom', type=Path, default=ROOT / 'emu/inputs/eeprom.bin')
    ap.add_argument('--output', type=Path, default=ROOT / 'emu/output/fixture/band.json')
    ap.add_argument('--skip-band', action='store_true', help='stop after the Q publication')
    ap.add_argument('--clear-gate', action='store_true', help='zero the 0x180cda70 block the dispatch gate 0xe3a1c reads (experiment)')
    ap.add_argument('--gate-index', type=int, default=None, help='set byte 0x180cda70+N to 1 before the dispatch (experiment)')
    ap.add_argument('--entry', choices=('e6b80', 'e6d44'), default='e6b80', help='dispatch entry: executor e6b80(Q) or band task entry e6d44(Q)')
    ap.add_argument('--set', action='append', default=[], help='RAM preset before the dispatch, addr=value[:width] (experiment)')
    ap.add_argument('--job-mode', type=int, default=1, help='stock print-mode index written at job+0x28 (1 = the black-only fixture)')
    ap.add_argument('--job-width', type=int, default=320)
    ap.add_argument('--prepare', action='store_true', help='brief 11 answer: repair Q, scheduler state, profile table; run e6aea (e3ad6) before the executor')
    opts = ap.parse_args()

    fx = Fixture(ready_level=4, eeprom=opts.eeprom)
    fx.ram_watch = WATCH
    fx.boot(); fx.run_boot(ticks=300, budget=12_000_000)
    alias_high_windows(fx)
    fx.snapshot()
    fx.stub = True
    applied = apply_presets(fx, ['executor-enable', 'permission', 'cap-enables', 'guards-clear', 'ambient', 'maint-defaults', 'status-clear-A'], [])
    report = {'presets': applied, 'steps': []}

    # 1. allocation (brief 8 §1 recipe)
    fx.uc.mem_write(JOB, bytes(0x80))
    fx.uc.mem_write(JOB + 0x28, struct.pack('<H', opts.job_mode))
    fx.uc.mem_write(JOB + 0x2c, struct.pack('<I', opts.job_width))
    fx.uc.mem_write(JOB + 0x30, struct.pack('<H', 600))
    report['steps'].append(step(fx, 'adc14 allocation', 0xadc14, [JOB]))
    report['managers'] = []
    for g in range(7):
        m = bytes(fx.uc.mem_read(0x1879b09c + 0x1c * g, 0x1c)); sl, _, wd, st, sz, md = struct.unpack_from('<HHHHIH', m, 0); base = struct.unpack_from('<I', m, 0x18)[0]
        report['managers'].append(dict(group=g, slots=sl, width=wd, stride=st, size=sz, mode=md, factor=m[12], R=m[13], base=hex(base)))
        print(f'manager {g}: slots={sl} width={wd} stride={st} size={sz} mode={md} factor={m[12]} R={m[13]} base={base:#x}')
    m6 = bytes(fx.uc.mem_read(0x1879b09c + 0x1c * 6, 0x1c))
    report['manager6'] = m6.hex()
    slots, _, width, stride, size, mode = struct.unpack_from('<HHHHIH', m6, 0)
    base = struct.unpack_from('<I', m6, 0x18)[0]
    print(f'manager 6: slots={slots} width={width} stride={stride} size={size} mode={mode} base={base:#x}')
    report['manager6_fields'] = dict(slots=slots, width=width, stride=stride, size=size, mode=mode, base=hex(base))

    # 2. raster worker on a synthetic packet (brief 8 §2)
    src = bytearray(320)
    for r in range(8):
        src[40 * r + r] = 0x80 >> r
    fx.uc.mem_write(SOURCE, bytes(src))
    p = bytearray(0x40)
    struct.pack_into('<HHB', p, 0, 1, 600, 0)
    struct.pack_into('<I', p, 8, 0x18)
    struct.pack_into('<I', p, 0xc, 320)
    struct.pack_into('<I', p, 0x14, SOURCE)
    struct.pack_into('<HHHH', p, 0x18, 0, 0, 1, 8)
    struct.pack_into('<H', p, 0x20, 0)
    p[0x24] = 0
    fx.uc.mem_write(PACKET, bytes(p))
    report['steps'].append(step(fx, 'a8422 raster worker', 0xa8422, [PACKET]))
    if base:
        raster = bytes(fx.uc.mem_read(base + 0x780, 0x6400)) if 0x10000000 <= base < 0x18800000 else b''
        report['raster_nonzero_bytes'] = sum(1 for b in raster if b)
        print('raster storage nonzero bytes:', report['raster_nonzero_bytes'], '(converter is not modeled)')

    # 3. Q builder with a hand-filled selection matrix (brief 7 answer §1)
    matrix = bytearray(b'\xff\xff' * (7 * 40))
    struct.pack_into('<h', matrix, 2 * (6 * 40 + 18), 0)
    fx.uc.mem_write(MATRIX, bytes(matrix))
    fx.uc.mem_write(R_BYTE, bytes([8]))
    fx.uc.mem_write(D_HALF, struct.pack('<H', 600))
    published = {}

    def capture(cpu, address, size, user):
        if address == PUBLISH_SITE and not published:
            published['r0'] = cpu.reg_read(UC_ARM_REG_R0); published['q'] = cpu.reg_read(UC_ARM_REG_R1)
    fx.uc.hook_add(UC_HOOK_CODE, capture, begin=PUBLISH_SITE, end=PUBLISH_SITE)
    report['steps'].append(step(fx, 'b12a2 Q builder', 0xb12a2, [0]))
    q = published.get('q')
    report['publication'] = {k: hex(v) for k, v in published.items()}
    print('publication at 0xb1aa0:', report['publication'])
    if q and (0x18000000 <= q < 0x18800000 or 0x10000000 <= q < 0x10100000):
        qb = bytes(fx.uc.mem_read(q, 0x494)); report['q_hex'] = qb.hex()
        words = struct.unpack('<%dI' % (0x494 // 4), qb)
        print('Q nonzero words:', sum(1 for w in words if w), 'of', len(words), '; first 16:', [hex(w) for w in words[:16]])
    if opts.skip_band or not q:
        opts.output.parent.mkdir(parents=True, exist_ok=True); opts.output.write_text(json.dumps(report, indent=1))
        print('wrote', opts.output); return

    # 3b. brief 11 answer §2-3: repair Q and the scheduler state, load the
    # profile table, run the lane preparation e3ad6(Q) (via e6aea) before the executor.
    if opts.prepare:
        N = 320
        B = base + 0x780; S = stride; END = B + (N // 16 - 1) * S
        def w16(off, v): fx.uc.mem_write(q + off, struct.pack('<h', v))
        def w32(off, v): fx.uc.mem_write(q + off, struct.pack('<I', v & 0xffffffff))
        w16(0x28, 0); w16(0x3a, 5000); w16(0x3c, 0)
        for i in range(10): w16(0x3e + 2 * i, 0)
        fx.uc.mem_write(q + 0x52, struct.pack('<H', 0x83)); fx.uc.mem_write(q + 0x54, b'\x01')
        for i in (0, 1):
            w16(0x56 + 2 * i, 0); w16(0x6a + 2 * i, 4 * N)
            w32(0x11c + 4 * i, B); w32(0x144 + 4 * i, END); w32(0x16c + 4 * i, S)
            w32(0x194 + 4 * i, B); w32(0x1bc + 4 * i, END); w32(0x1e4 + 4 * i, 0x4f)
            w32(0x20c + 4 * i, 0); w32(0x234 + 4 * i, 0)
            fx.uc.mem_write(q + 0x2ac + 12 * i, struct.pack('<III', 0x10000, 0, 0))
        for i in range(2, 10):
            w16(0x56 + 2 * i, -1); w16(0x6a + 2 * i, -1)
            for off in (0x11c, 0x144, 0x194, 0x1bc): w32(off + 4 * i, 0xffffffff)
            fx.uc.mem_write(q + 0x2ac + 12 * i, bytes(12))
        w32(0x350, 1); w32(0x354, 3)
        for i in range(5): w32(0x394 + 4 * i, 0x009f0000)
        w32(0x3a8, 9)
        fx.uc.mem_write(0x180cda80, struct.pack('<II', 0, 0)); fx.uc.mem_write(0x180cda88, b'\x01')
        fx.uc.mem_write(0x180cff5d, b'\x00'); fx.uc.mem_write(0x180cd590, struct.pack('<i', 0))
        fx.uc.mem_write(0x18089900, bytes(fx.image[0x1847c8:0x1847c8 + 14 * 0xf8]))   # CR profile table from ROM
        report['steps'].append(step(fx, 'e6aea lane preparation (e3ad6)', 0xe6aea, [q]))
        A = bytes(fx.uc.mem_read(0x180cda34, 0x70))
        bounds = struct.unpack_from('<IIII', A, 0); approach, final, target = struct.unpack_from('<III', A, 0x10)
        direction, prev = A[0x1c], A[0x1d]; profile = struct.unpack_from('<I', A, 0x20)[0]
        lanes = list(fx.uc.mem_read(0x180cda70, 10))
        print(f'A: bounds={bounds} approach={approach} final={final} target={target} dir={direction} profile={profile:#x} lanes={lanes}')
        report['A'] = dict(bounds=bounds, approach=approach, final=final, target=target, direction=direction, profile=profile, lanes=lanes)
    # 4. band dispatch on Q (brief 4 §7: e2764 -> e6d44 -> e6b80(Q))
    gate = bytes(fx.uc.mem_read(0x180cda70, 12))
    print('gate block 0x180cda70 before dispatch:', gate.hex())
    report['gate_block_before'] = gate.hex()
    if opts.clear_gate:
        fx.uc.mem_write(0x180cda70, bytes(12))
    if opts.gate_index is not None:
        fx.uc.mem_write(0x180cda70 + opts.gate_index, b'\x01')
    for item in opts.set:
        addr, _, rest = item.partition('='); value, _, width = rest.partition(':')
        fx.uc.mem_write(int(addr, 0), int(value, 0).to_bytes(int(width, 0) if width else 4, 'little'))
        print(f'set {addr} = {value}')
    entry = {'e6b80': 0xe6b80, 'e6d44': 0xe6d44}[opts.entry]
    report['steps'].append(step(fx, f'{opts.entry} band dispatch', entry, [q], budget=6_000_000))
    last = report['steps'][-1]
    keys = ('0xc0c51700', '0xc0c51800', '0xc0c51900', '0xc0c51a00', '0xc0c51704', '0xc0c51804', '0xc0c51904', '0xc0c51a04',
            '0xc0c51d34', '0xc0c51d38', '0xc0c51108', '0xc0c51114', '0xc0c51d04', '0xc0c51d0c', '0xc0c51500', '0xc0c51504', '0xc0c51508',
            '0xc0c27000', '0xc0c27004', '0xc0c27100', '0xc0c27200', '0xc0c27300', '0xc0c27810', '0xc0c28080', '0xc0c280c0')
    regs = {}
    for wv in last['mmio_writes']:
        if wv['address'] in keys: regs[wv['address']] = wv['value']
    print('key registers:', regs)
    for e in last['services']:
        if e.get('decoded') and e['decoded'].get('kind') == 'CR move': print('CR request:', e['decoded'])
    report['key_registers'] = regs
    pages = sorted(fx.ram_pages); runs = []
    for pg in pages:
        if runs and runs[-1][1] == pg: runs[-1][1] = pg + 1
        else: runs.append([pg, pg + 1])
    report['ram_pages_touched_4k'] = [[hex(a << 12), hex(b << 12)] for a, b in runs]
    print('RAM pages touched:', len(pages))
    opts.output.parent.mkdir(parents=True, exist_ok=True)
    opts.output.write_text(json.dumps(report, indent=1))
    print('wrote', opts.output)


if __name__ == '__main__':
    main()
