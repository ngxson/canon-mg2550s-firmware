#!/usr/bin/env python3
"""Emulate the hybrid package from reset: bootloader validates and copies the
whole-remainder payload, jumps to 0x20020000, the entry word branches into the
app inside the hole, the app answers INFO as the application, and the new USB
0x95 request calls two stock data initializers in place from flash. Their RAM
side effects (brief 16 §2) are read back through the tunnel's read-only stock
RAM range. Nothing here proves mechanical behaviour.
"""
import json, struct, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
APP = HERE.parent; BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests')); sys.path.insert(0, str(APP / 'tests'))
from model import setup  # noqa: E402
from model_printer import PrinterModel  # noqa: E402

KEY = 0x7d4e
import os
BUILD = Path(os.environ.get('HYBRID_BUILD', str(HERE / 'build')))


def main():
    image = (BUILD / 'candidate.bin').read_bytes()
    manifest = json.loads((BUILD / 'manifest.json').read_text())
    bs = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    md = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
          'symbols': {'bootloader': bs['bootloader'], 'application': manifest['symbols']['application']}}
    LF_C, LF_POS, SENSOR = 0xc0c52000, 0xc0c53100, 0xc0c84e08
    PICK_COUNTS, CLEAR_COUNTS = 15000, 12000
    class HybridModel(PrinterModel):
        # Readiness the stock emulator models too (emu/stock/boot.py level 3):
        # a timer channel's reset bit 31 reads back clear once requested.
        # Plus the feed suite's LF model (app/tests/validate_feed.py): the
        # encoder follows the companion command, a sheet reaches the sensor
        # after PICK_COUNTS of reverse travel and clears it after CLEAR_COUNTS forward.
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.pos = 0x1000; self.paper = True; self.present = False; self.reverse = 0; self.forward = 0; self.lf_cmd_writes = []
        def write(self, cpu, offset, size, value, user):
            a = 0xc0000000 + offset
            if a == LF_POS: self.pos = value
            if a == 0xc0c29af0: self.he_armed = value == 1; self.he_reads = 0; self.he_arms = getattr(self, 'he_arms', 0) + (1 if value == 1 else 0)
            if a == 0xc0c29030 and not value & 1: self.he_done = False
            if a == 0xc0c52008 and len(self.lf_cmd_writes) < 20000: self.lf_cmd_writes.append(value)
            return super().write(cpu, offset, size, value, user)
        def read(self, cpu, offset, size, user):
            address = 0xc0000000 + offset
            if address == LF_POS:
                if self.regs.get(LF_C) == 0x102:
                    step = (self.regs.get(0xc0c52008, 2000) - 2000) // 40
                    self.pos += step
                    if step < 0 and not self.present:
                        self.reverse -= step
                        if self.paper and self.reverse >= PICK_COUNTS: self.present = True; self.forward = 0
                    if step > 0 and self.present:
                        self.forward += step
                        if self.forward >= CLEAR_COUNTS: self.present = False; self.reverse = 0
                return self.pos & 0x7ffff
            if address == SENSOR: return 0 if self.present else 1
            if address == 0xc0c29030:
                # head engine (app/tests/model_head.py contract): armed work completes after a few status reads
                if getattr(self, 'he_armed', False):
                    self.he_reads = getattr(self, 'he_reads', 0) + 1
                    if self.he_reads > 4: self.he_done = True; self.he_armed = False
                return (self.regs.get(0xc0c29030, 0) & ~1) | (1 if getattr(self, 'he_done', False) else 0)
            value = super().read(cpu, offset, size, user)
            if 0xc0140000 <= address <= 0xc0140700 and address & 0xff == 0:
                value &= ~0x80000000
            return value
    if __import__('os').environ.get('HYBRID_IPP_PRINT'):
        sys.path.insert(0, str(HERE.parent / 'tests')); import validate_escl as v
        class HybridIppModel(v.IppUsbHost, HybridModel): pass
        e = HybridIppModel(image=image, metadata=md)
    else:
        e = HybridModel(image=image, metadata=md)
    if __import__('os').environ.get('HYBRID_FLASH_READ_WATCH'):
        # Which code reads these flash ranges? (e.g. "0x16a000-0x17a000,0x1e5000-0x1ee000": candidate
        # regions for relocating the RAM snapshot). Summarised at exit by (pc, 4 KiB sector).
        import atexit
        from unicorn import UC_HOOK_MEM_READ
        from unicorn.arm_const import UC_ARM_REG_PC as _WPC
        from collections import Counter
        watch_hits = Counter()
        def watch_read(cpu, access, address, size, value, user):
            watch_hits[(cpu.reg_read(_WPC), address & ~0xfff)] += 1
        for rng in __import__('os').environ['HYBRID_FLASH_READ_WATCH'].split(','):
            lo, hi = (int(x, 16) for x in rng.split('-'))
            e.uc.hook_add(UC_HOOK_MEM_READ, watch_read, begin=lo, end=hi - 1)
        atexit.register(lambda: print('flash read watch (pc, sector, reads):', sorted(((hex(pc), hex(sec), n) for (pc, sec), n in watch_hits.items()), key=lambda t: t[1])))
    # On the board 0x10000000, 0x18000000 and 0x20000000 are the same DRAM
    # (measured 2026-10-26); stock's allocator relies on the cached/uncached
    # alias arithmetic. Share one buffer across the three windows.
    import ctypes
    from unicorn import UC_PROT_ALL
    e.uc.mem_unmap(0x20000000, 0x800000)
    e.shared_dram = (ctypes.c_ubyte * 0x800000)()
    for base in (0x10000000, 0x18000000, 0x20000000):
        e.uc.mem_map_ptr(base, 0x800000, UC_PROT_ALL, ctypes.addressof(e.shared_dram))
    # Deliver SVC exceptions like the CPU: stock code calls its kernel with svc #0.
    from unicorn import UC_HOOK_INTR, UC_HOOK_MEM_READ_UNMAPPED, UC_HOOK_MEM_WRITE_UNMAPPED
    from unicorn.arm_const import UC_ARM_REG_CPSR, UC_ARM_REG_SPSR, UC_ARM_REG_LR as LR, UC_ARM_REG_PC as PC
    # Exception entry through real instructions: a CPSR register write does
    # not re-bank sp/lr in Unicorn, so the entry is done by a RAM trampoline
    # that switches to supervisor mode with msr, loads the banked lr and SPSR
    # from literals the hook fills in, and jumps to the vector.
    TRAMP = 0x20700000
    tramp_code = struct.pack('<8I', 0xe321f093,            # msr cpsr_c, #0x93
                             0xe59fe010,                   # ldr lr, [pc, #16]  -> next_pc
                             0xe52d0004,                   # push {r0}
                             0xe59f000c,                   # ldr r0, [pc, #12]  -> old cpsr
                             0xe16ff000,                   # msr spsr_cxsf, r0
                             0xe49d0004,                   # pop {r0}
                             0xe3a0f008,                   # mov pc, #8
                             0)
    def interrupt(cpu, number, user):
        if number != 2:
            e.error = RuntimeError(f'unexpected CPU exception {number}'); cpu.emu_stop(); return
        old = cpu.reg_read(UC_ARM_REG_CPSR); next_pc = cpu.reg_read(PC)
        if old & 0x1f == 0x10:
            e.error = RuntimeError('svc from user mode is not supported by the hybrid validator'); cpu.emu_stop(); return
        cpu.mem_write(TRAMP, tramp_code[:28] + struct.pack('<I', next_pc) + struct.pack('<I', old))
        cpu.reg_write(PC, TRAMP)
    e.uc.hook_add(UC_HOOK_INTR, interrupt)
    def unmapped(cpu, access, address, size, value, user):
        print(f'UNMAPPED access at pc {cpu.reg_read(PC):#x}: address {address:#x} size {size}')
        return False
    e.uc.hook_add(UC_HOOK_MEM_READ_UNMAPPED | UC_HOOK_MEM_WRITE_UNMAPPED, unmapped)
    # Bootloader CRC over the 1.9 MiB payload plus the copy need a large budget.
    e.run(lambda x: bool(x.info()) and x.info()[2] == 2, budget=400_000_000)
    info = e.info()
    assert info[2] == 2, info
    pc_app = int(manifest['layout']['app_ram'], 16)
    from unicorn.arm_const import UC_ARM_REG_PC
    pc = e.uc.reg_read(UC_ARM_REG_PC)
    assert 0x20020000 in e.entered and pc_app <= pc < pc_app + 0x40000, (hex(pc), [hex(a) for a in e.entered])   # app code runs at the hole's RAM address
    print('PASS boot: bootloader -> hole app, INFO mode', info[2], 'app RAM', hex(pc_app))
    from collections import deque as _dq
    from unicorn import UC_HOOK_CODE as _HC
    from unicorn.arm_const import UC_ARM_REG_CPSR as _CPSR, UC_ARM_REG_LR as _LR
    _ring = _dq(maxlen=400)
    _frozen = []; _modes = []; _last_mode = [None]
    def _ring_hook(cpu, a, sz, u):
        if _frozen: return
        if a == 0: _frozen.append(True); return          # reset vector: keep the history that led here
        m = cpu.reg_read(_CPSR) & 0x3f
        if m != _last_mode[0]:
            _modes.append((hex(a), hex(m), hex(cpu.reg_read(_LR)))); _last_mode[0] = m
        _ring.append((a, m))
    # Per-instruction Python tracing dominates long URF/printing tests. Keep
    # the crash-history hook available explicitly; normal runs still retain
    # unmapped-access hooks, register assertions and targeted stage captures.
    _ring_handle = e.uc.hook_add(_HC, _ring_hook) if __import__('os').environ.get('HYBRID_TRACE_PC') else None
    import atexit
    def _dump():
        sym = manifest['symbols']['application']
        vals = {n: struct.unpack('<I', bytes(e.uc.mem_read(int(sym[n], 16), 4)))[0] for n in ('trace_n', 'routines_run', 'routine_pending', 'last_result', 'errors', 'ram_ready')}
        print('DEBUG pc', hex(e.uc.reg_read(UC_ARM_REG_PC)), 'lr', hex(e.uc.reg_read(_LR)), 'shim', vals)
        pcs = [(hex(a), hex(m)) for a, m in _ring]
        print('reset seen:', bool(_frozen)); print('last pcs:', pcs[-60:]); print('mode transitions (pc, mode, lr):', _modes[-30:])
    atexit.register(_dump)

    def command(name, req, value=0, index=0, expect_stall=False, budget=30000000):
        n = sum(1 for _, nm, _ in e.responses if nm == name); st = len(e.stalls)
        e.requests.appendleft((name, setup(0x40, req, value, index)))
        e.run(lambda x, n=n, st=st: sum(1 for _, nm, _ in x.responses if nm == name) > n or len(x.stalls) > st, resume=True, budget=budget)
        assert (len(e.stalls) > st) == expect_stall, (name, e.stalls)

    def read_in(name, req, length, value=0, index=0):
        n = sum(1 for _, nm, _ in e.responses if nm == name)
        e.requests.appendleft((name, setup(0xc0, req, value, index, length)))
        e.run(lambda x, n=n: sum(1 for _, nm, _ in x.responses if nm == name) > n, resume=True)
        return [d for _, nm, d in e.responses if nm == name][-1]

    def tunnel_word(address):
        command('addr', 0x90, address & 0xffff, address >> 16)
        return struct.unpack('<I', read_in('tr', 0x91, 4))[0]

    def call(address, expect_stall=False):
        command('call', 0x95, address & 0xffff, address >> 16, expect_stall=expect_stall)
        return struct.unpack('<6I', read_in('hres', 0x96, 24))

    # Locked: refused. Outside the stock image: refused.
    r = call(0xe8611, expect_stall=True); assert r[4] == 1 and r[3] == 0, r
    command('unlock', 0x93, KEY)
    r = call(0x20020001, expect_stall=True); assert r[4] == 2, r
    r = call(0xe8610, expect_stall=True); assert r[4] == 3, r          # ARM address bit clear
    # 0xe8610: maintenance data initializer; sets u32 0x18079aa8 = 1, clears u8 0x180d55bd.
    e.uc.mem_write(0x180d55bd, b'\x5a')
    r = call(0xe8611); assert r[0] == 0x42594848 and r[1] == 0xe8611 and r[3] == 1, r
    assert tunnel_word(0x18079aa8) == 1, hex(tunnel_word(0x18079aa8))
    assert tunnel_word(0x180d55bc) >> 8 & 0xff == 0, hex(tunnel_word(0x180d55bc))
    # 0xc763e: mechanism gate initializer; sets bytes 0x18079330/331/332 and the four age thresholds.
    r = call(0xc763f); assert r[3] == 2, r
    assert tunnel_word(0x18079330) & 0xffffff == 0x010101, hex(tunnel_word(0x18079330))
    thresholds = [tunnel_word(0x18079a98 + 4*i) for i in range(4)]
    assert thresholds == [20160, 43200, 64800, 129600], thresholds
    # Our own RAM stays unreadable through the tunnel.
    command('addr', 0x90, 0x0000, 0x2002); n = len(e.stalls)
    e.requests.appendleft(('ram', setup(0xc0, 0x91, 0, 0, 4)))
    e.run(lambda x, n=n: len(x.stalls) > n, resume=True)
    print('PASS stock calls: 0xe8610 and 0xc763e executed in place; RAM effects match brief 16 §2')

    # Cold-start prerequisite: the first stock CR request may come from the
    # paper loader, before band_run. The sparse RAM snapshot omits the profile
    # table, and direct moves do not run stock's separate initializer. Stop after RAM restoration,
    # before any stock service or motor task, and require the actual ROM copy.
    before = struct.unpack('<16I', read_in('cold_before', 0x98, 64, value=0xffff))[2]
    # Relocated USB DMA must survive stock's large scatter-table RAM clear.
    # This fixture uses only EP0 here, leaving both IPP TX buffers untouched.
    dma = int(manifest['symbols']['application']['tx_buffers'], 16)
    dma_sentinel = bytes(range(256)) * 128
    e.uc.mem_write(dma, dma_sentinel)
    command('cold_ram', 0x97, 5 | 0x400, 1 << 8, budget=60000000)
    for _ in range(100):
        cold = struct.unpack('<16I', read_in('cold_after', 0x98, 64, value=0xffff))
        if cold[2] > before: break
    assert cold[2] == before + 1, cold
    assert bytes(e.uc.mem_read(0x18089900, 14 * 0xf8)) == image[0x1847c8:0x1847c8 + 14 * 0xf8], 'cold CR profile table is incomplete'
    assert bytes(e.uc.mem_read(dma, len(dma_sentinel))) == dma_sentinel, 'stock RAM init erased USB DMA buffers'
    print('PASS cold RAM: all 14 CR profiles restored before any band or loader call')
    print('PASS cold RAM: relocated USB DMA buffers preserved across stock initialization')

    # Shim: run stock routines in place with our services, dry motion.
    KIND = {1: 'CR_MOVE', 2: 'CR_PROFILE', 3: 'CR_RELEASE', 4: 'LF_MOVE', 5: 'STATUS_GET', 6: 'STATUS_PUB', 7: 'NVM', 8: 'NOTIFY', 9: 'WAIT_TIMEOUT', 10: 'DELAY', 11: 'ROUTINE', 12: 'UNKNOWN'}

    def run_status():
        return struct.unpack('<16I', read_in('rstat', 0x98, 64, value=0xffff))

    def trace():
        rows = []
        for i in range(160):
            n = sum(1 for _, nm, _ in e.responses if nm == 'trace'); st = len(e.stalls)
            e.requests.appendleft(('trace', setup(0xc0, 0x98, i, 0, 16)))
            e.run(lambda x, n=n, st=st: sum(1 for _, nm, _ in x.responses if nm == 'trace') > n or len(x.stalls) > st, resume=True)
            if len(e.stalls) > st: break
            k, a, b, c = struct.unpack('<4I', [d for _, nm, d in e.responses if nm == 'trace'][-1])
            rows.append((KIND.get(k, k), a, b, c))
        return rows

    # Any store into the flash window during a shim run is a stock pointer we left unset.
    from unicorn import UC_HOOK_MEM_WRITE
    from unicorn.arm_const import UC_ARM_REG_LR
    stray = []
    def flash_write(cpu, access, address, size, value, user):
        if len(stray) < 40:
            stray.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)), 'lr': hex(cpu.reg_read(UC_ARM_REG_LR)), 'address': hex(address), 'size': size, 'value': hex(value)})
    e.uc.hook_add(UC_HOOK_MEM_WRITE, flash_write, begin=0, end=0x1fffff)
    from collections import deque
    from unicorn import UC_HOOK_CODE
    from unicorn.arm_const import UC_ARM_REG_CPSR as CPSR
    ring = deque(maxlen=48)
    def ring_hook(cpu, address, size, user):
        ring.append((address, cpu.reg_read(CPSR) & 0x3f))
    import os
    from collections import Counter
    census = Counter(); census_values = {}
    from unicorn import UC_HOOK_MEM_READ
    def mmio_read(cpu, access, address, size, value, user):
        pc = cpu.reg_read(PC)
        if pc < 0x200000:
            census[(pc, address)] += 1
            v = e.regs.get(address, 0)
            census_values.setdefault((pc, address), set()).add(v)
    mmio_writes = []
    def mmio_write(cpu, access, address, size, value, user):
        pc = cpu.reg_read(PC)
        if pc < 0x200000 and len(mmio_writes) < 40000:
            mmio_writes.append((pc, address, value))
    def shim_log(filter_=None):
        log = b''
        for _ in range(400):
            part = read_in('log', 0x50, 256)
            if not part: break
            log += part
        return [l for l in log.decode(errors='replace').splitlines() if filter_ is None or filter_ in l]
    # Band allocation lifetime (brief 22 answer §1): nothing allocated while a prepared band
    # is live may land inside it, and the three pattern buffers Q+0x264.. (20 KiB each) must
    # reach the executor unchanged. Pool allocations are observed at stock 0xa109a/0xa109e.
    arena_base = int(manifest['symbols']['application']['arena'], 16)
    from unicorn.arm_const import UC_ARM_REG_R2 as _R2l
    pool_allocs = []
    arena_used_addr = int(manifest['symbols']['application']['arena_used'], 16)
    pool_entry = int(manifest['symbols']['application']['shim_pool_alloc'], 16) & ~1
    band_live_addr = int(manifest['symbols']['application']['band_live'], 16); band_mark_addr = int(manifest['symbols']['application']['band_mark'], 16)
    pool_violations = []
    def pool_probe(cpu, a, sz, u):
        # every stock pool request reaches the shim's bump allocator here; the block starts at arena + arena_used
        rec = {'request': cpu.reg_read(_R2l), 'result': arena_base + struct.unpack('<I', bytes(cpu.mem_read(arena_used_addr, 4)))[0]}
        pool_allocs.append(rec)
        if cpu.mem_read(band_live_addr, 1)[0] and rec['result'] < arena_base + struct.unpack('<I', bytes(cpu.mem_read(band_mark_addr, 4)))[0]:
            pool_violations.append(rec)
    e.uc.hook_add(UC_HOOK_CODE, pool_probe, begin=pool_entry, end=pool_entry)
    def band_lifetime_begin(q, used):
        ranges = [(struct.unpack('<I', bytes(e.uc.mem_read(q + 0x264 + 4 * j, 4)))[0], 20480) for j in range(3)]
        mark = struct.unpack('<I', bytes(e.uc.mem_read(int(manifest['symbols']['application']['band_mark'], 16), 4)))[0]
        assert mark == used, (mark, used)
        del pool_allocs[:]
        return {'mark': mark, 'patterns': [(p, bytes(e.uc.mem_read(p, n))) for p, n in ranges if p]}
    def band_lifetime_check(tag, live):
        bad = [a for a in pool_allocs if 'result' in a and a['result'] and arena_base <= a['result'] < arena_base + live['mark']]
        assert not bad, f'{tag}: {len(bad)} allocations inside the live band (mark {live["mark"]}): {[hex(a["result"]) for a in bad][:8]}'
        for j, (p, before) in enumerate(live['patterns']):
            after = bytes(e.uc.mem_read(p, len(before)))
            assert after == before, f'{tag}: pattern buffer {j} at {p:#x} changed during execution ({sum(1 for x, y in zip(before, after) if x != y)} bytes)'
        print(f'PASS band lifetime {tag}: {len(pool_allocs)} executor allocations above the live band ({live["mark"]} bytes), {len(live["patterns"])} pattern buffers unchanged')
    def wide_band(cols, rows, place, tag, budget=80000000, fresh=True):
        """Wide band for page printing: BK selection matrix entries 0..rows/8-1 -> slots, manager
        allocated `cols` wide, host placement. Returns the allocation, builder masks, raster-feed
        extents and CR windows after a stock-CR dry executor run."""
        command(f'band_params_{tag}', 0x9a, cols, rows); command(f'band_place_{tag}', 0x9b, place & 0xffff)
        rb = run_status()[2]
        if os.environ.get('HYBRID_RAM_DIFF'):
            # Capture the Q builder's input RAM at its entry (0xb12a2) and at the lane
            # preparation entry (0xe6aea), compare with the previous pass's captures.
            regions = [(0x1806b000, 0x3000), (0x180cd000, 0x4000), (0x180cf000, 0x2000), (0x18079000, 0x2000)]
            if not hasattr(e, 'ram_caps'):
                e.ram_caps = {}
                def cap_hook(cpu, a, sz, u):
                    e.ram_caps.setdefault(a, []).append({r: bytes(e.uc.mem_read(r, n)) for r, n in regions})
                for a in (0xb12a2, 0xe6aea, 0xe6b80): e.uc.hook_add(UC_HOOK_CODE, cap_hook, begin=a, end=a)
            def report_caps():
                for a, caps in e.ram_caps.items():
                    if len(caps) < 2: continue
                    x, y = caps[-2], caps[-1]; diffs = []
                    for r, n in regions:
                        for i in range(0, n, 4):
                            if x[r][i:i+4] != y[r][i:i+4]: diffs.append((hex(r + i), x[r][i:i+4].hex(), y[r][i:i+4].hex()))
                    print(f'RAM at entry {a:#x}: pass {len(caps) - 1} vs pass {len(caps)}: {len(diffs)} differing words'); [print('   ', d) for d in diffs[:80]]
            e.report_caps = report_caps
        try:
            command(f'prepare_{tag}', 0x97, 5 | (0x400 if fresh else 0), 0, budget=budget)
        except AssertionError:
            print(f'wide band {tag}: prepare did not finish within {budget} instructions; pc {e.uc.reg_read(PC):#x} lr {e.uc.reg_read(LR):#x}')
            print('shim log:'); [print('   ', l) for l in shim_log('[shim]')[-30:]]
            print('last pcs:', [(hex(a), hex(m)) for a, m in list(ring)[-40:]])
            sym = manifest['symbols']['application']
            tn = struct.unpack('<I', bytes(e.uc.mem_read(int(sym['trace_n'], 16), 4)))[0]
            recs = [struct.unpack('<4I', bytes(e.uc.mem_read(int(sym['trace'], 16) + 16*i, 16))) for i in range(min(tn, 160))]
            print('trace_n', tn); [print('   ', (KIND.get(k, k), hex(a), hex(b), hex(c))) for k, a, b, c in recs[-24:]]
            m6 = bytes(e.uc.mem_read(0x1879b144, 0x1c)); print('manager 6:', m6.hex(), struct.unpack_from('<HHHHIH', m6, 0))
            raise
        for _ in range(400):
            st = run_status()
            if st[2] > rb: break
        assert st[2] == rb + 1 and st[1] == 1, st
        q, base = st[12], st[13]
        m6 = bytes(e.uc.mem_read(0x1879b144, 0x1c)); slots, _, width, stride, size = struct.unpack_from('<HHHHI', m6, 0)
        used = struct.unpack('<I', bytes(e.uc.mem_read(int(manifest['symbols']['application']['arena_used'], 16), 4)))[0]
        print(f'wide band {tag}: arena used {used} bytes after prepare (manager size {size})')
        masks = [struct.unpack('<3I', bytes(e.uc.mem_read(q + 0x2ac + 12 * i, 12))) for i in range(10)]
        live = band_lifetime_begin(q, used)
        mmio_writes.clear(); rb = run_status()[2]; command(f'fire_{tag}', 0x97, 7 | 0x1000 | 0x2000, 0, budget=budget)
        fire_writes = list(mmio_writes)
        band_lifetime_check(tag, live)
        Path(f'/project/emu/output/fixture/writes_{tag}.json').write_text(json.dumps([(hex(a), hex(v)) for pc, a, v in fire_writes if 0xc0c20000 <= a < 0xc0c2a000 or 0xc0c51000 <= a < 0xc0c52000]))
        for _ in range(400):
            st = run_status()
            if st[2] > rb: break
        assert st[2] == rb + 1, st
        rows_ = trace()
        addrs = [0xc0c51d34, 0xc0c51d38, 0xc0c51108, 0xc0c51114] + [0xc0c51700 + t + 4 * k for t in (0, 0x100, 0x200, 0x300) for k in range(3)] + \
                [0xc0c27000, 0xc0c27004, 0xc0c27100, 0xc0c27104, 0xc0c27200, 0xc0c27204, 0xc0c27300, 0xc0c27304] + [0xc0c27570 + 0x10 * i + 4 * j for i in (0, 1) for j in range(3)] + \
                [0xc0c28200, 0xc0c28210, 0xc0c28214, 0xc0c28218, 0xc0c2821c, 0xc0c28220, 0xc0c27810, 0xc0c27814, 0xc0c28080, 0xc0c280c0, 0xc0c28000]
        regs = {hex(a): (hex(e.regs[a]) if a in e.regs else None) for a in addrs}
        out = dict(result=st[1], errors=st[3], q=hex(q), base=hex(base), slots=slots, width=width, stride=stride, size=size,
                   masks=[[hex(v) for v in m] for m in masks], regs=regs, moves=[r for r in rows_ if r[0] == 'CR_MOVE'],
                   lanes=hex(struct.unpack('<I', bytes(e.uc.mem_read(0x180cda70, 4)))[0]),
                   A=[hex(x) for x in struct.unpack('<9I', bytes(e.uc.mem_read(0x180cda34, 36)))], log=shim_log('band'))
        out['writes'] = fire_writes; out['trace'] = rows_
        print(f'wide band {tag}: {json.dumps(out)}')
        return out
    if os.environ.get('HYBRID_PLANE_PROBE'):
        # Which plane id makes the raster worker reserve a slot in which manager (mode 7)?
        cols = 256
        for plane in [int(x, 0) for x in os.environ['HYBRID_PLANE_PROBE'].split(',')]:
            command(f'band_params_p{plane:#x}', 0x9a, cols, 304 | 0x800 | (7 << 12)); command(f'band_place_p{plane:#x}', 0x9b, 152)
            command(f'band_plane_p{plane:#x}', 0x9b, plane, 1)
            rb = run_status()[2]; command(f'prepare_p{plane:#x}', 0x97, 5 | 0x400, 0, budget=200000000)
            for _ in range(400):
                st = run_status()
                if st[2] > rb: break
            used = []
            for g in (0, 2, 4, 6):
                m = bytes(e.uc.mem_read(0x1879b09c + 0x1c * g, 0x1c)); slots = struct.unpack_from('<H', m, 0)[0]; base = struct.unpack_from('<I', m, 0x18)[0]
                if not base: continue
                desc = bytes(e.uc.mem_read(base, 24 * slots))
                nz = [i for i in range(slots) if desc[24 * i] != 0 or any(desc[24 * i + 8 + 2 * k:24 * i + 10 + 2 * k] not in (b'\xff\xff', b'\x00\x00') for k in range(4))]
                raster = bytes(e.uc.mem_read(base + 24 * slots, 64))
                used.append((g, nz[:6], raster.hex()[:32]))
            print(f'plane {plane:#x}: prepare result {st[1]:#x} a8422 -> {st[15]} ; managers with reserved slots: {used}')
        return
    if os.environ.get('HYBRID_IPP_PRINT'):
        # End to end: an IPP Print-Job with a small URF page streams into the print engine (dry
        # mode: no homing, head or sheet checks, dry executor), which runs load, first advance,
        # per band prepare / tile raster / executor / advance, eject through the shim.
        sys.path.insert(0, str(HERE.parent.parent / 'tools')); import ipp_print as ip
        e.requests.extend([('altset2', setup(1, 11, 0, 2)), ('altset3', setup(1, 11, 0, 3))])
        e.run(lambda x: any(n == 'altset3' for g, n, d in x.responses if g == x.generation), resume=True)
        command('dry', 0xa3, 1, 1)
        quality = None
        mono_quality = bool(os.environ.get('HYBRID_IPP_MONO')); sparse = bool(os.environ.get('HYBRID_IPP_SPARSE')); deep = bool(os.environ.get('HYBRID_IPP_DEEP'))
        if os.environ.get('HYBRID_IPP_QUALITY'):
            import print_quality_capture as pq
            quality = pq.Capture(e, manifest, mono=mono_quality, sparse=sparse, deep=deep)
        width, rows_n = 2480, 60                                # 38 rows above the printable area, 22 printable -> 44 output rows -> 2 colour bands
        row = bytes([90, 150, 200]) * 1200 + bytes([255, 255, 255]) * (width - 1200)   # left: ink in all four planes; right: white
        if quality:
            width, rows_n = pq.WIDTH, 200 if mono_quality else pq.HEIGHT + (pq.DEEP if deep else 0)
        document = ip.urf_document([(width, rows_n, 1 if mono_quality else 3, pq.input_rows(mono_quality, sparse, deep) if quality else [row] * rows_n)])
        replay = __import__('os').environ.get('HYBRID_IPP_DOC')        # replay a recorded URF document (e.g. a physical job's) and report its sweeps
        if replay:
            document = open(replay, 'rb').read(); rows_n = struct.unpack('>I', document[12 + 24:12 + 28])[0]
            delays = []                                                 # (routines completed so far, delay ticks) for every stock delay()
            syms = {k: int(v, 16) for k, v in manifest['symbols']['application'].items()}
            from unicorn.arm_const import UC_ARM_REG_R0 as _DR0
            e.uc.hook_add(UC_HOOK_CODE, lambda cpu, a, sz, u: delays.append((int.from_bytes(cpu.mem_read(syms['routines_run'], 4), 'little'), cpu.reg_read(_DR0))),
                          begin=syms['shim_delay'], end=syms['shim_delay'])
        body = ip.print_job_request(document, 'emu', request_id=11)
        print(f'IPP print: URF {len(document)} bytes, request {len(body)} bytes')
        t0 = e.ticks
        v.send(e, 0, (f'POST /ipp/print HTTP/1.1\r\nHost: {v.HOST}\r\nContent-Type: application/ipp\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body)
        status, headers, resp, _ = v.receive(e, 0, budget=3_000_000_000)
        ipp_status, attrs = ip.ipp_parse(resp)
        print(f'Print-Job: HTTP {status} IPP {ipp_status:#06x} attrs job-id {attrs.get("job-id")} job-state {attrs.get("job-state")}')
        assert status == 200 and ipp_status == 0, (status, ipp_status)
        assert attrs['job-id'] == [(0x21, b'\x00\x00\x00\x01')] and attrs['job-state'] == [(0x23, b'\x00\x00\x00\x05')], attrs
        for _ in range(400):
            w = struct.unpack('<16I', read_in('prt', 0xa2, 64)); f = w[1]
            state = ip.ENGINE_STATES[f & 0xff]
            if state in ('completed', 'canceled', 'failed'): break
            e.run(lambda x: True, resume=True, budget=20_000_000)
        print(f'engine: {state}/{ip.STEPS[f >> 8 & 0xff]} rows_in {w[2]} bands {w[4]} sweeps {w[7]} pages {w[6]} failures {w[8]} last_fail {w[9]} routines {w[15]}')
        lines = shim_log()
        print('shim/print log:'); [print('   ', l) for l in lines if '[print]' in l or 'returned' in l or 'arena' in l]
        assert state == 'completed' and w[6] == 1 and w[8] == 0, (state, w)
        if replay:
            print('replay sweeps:', [l for l in lines if 'band N=' in l])
            print('replay advances:', [l for l in lines if 'advance ulf=' in l])
            import collections
            order = [l.split('run routine ')[1].split()[0] for l in lines if 'run routine ' in l]
            per = collections.defaultdict(list)
            for n, t in delays: per[n].append(t)
            print('replay delays per routine (index: routine -> [ticks]):', [(n, order[n] if n < len(order) else '?', per[n]) for n in sorted(per)])
            return
        # Sweeps: the quality fixtures re-derive the plan from the expected paper image
        # (print_quality_capture.expected_plan); the plain row's ink ends at output column
        # 2083, so one cropped sweep per phase covers it: 2 bands x 2 passes x 1 sweep.
        expected_sweeps, expected_skipped = (len(quality.expected_plan()[0]), quality.expected_plan()[1]) if quality else (4, 0)
        assert w[7] == expected_sweeps, ('sweeps', w[7], expected_sweeps)
        expected_rows = 2 * (rows_n - 38)
        expected_bands = 2 if not quality else max(1, -(-(expected_rows + (0 if mono_quality else 128)) // (304 if mono_quality else 152)))
        assert sum(1 for l in lines if 'band arena' in l) == expected_sweeps and any(f'page data complete: {expected_rows} rows, {expected_bands} bands' in l for l in lines), lines[-20:]
        assert not pool_violations, pool_violations[:5]
        # Job timing profile (print-speed brief step 1): USB 0xa2 wIndex 1 = engine phases, 2 = shim
        # counters, both per job. The emulator's clock is a model (1 ms per clock read), so only the
        # structure is checked here: disjoint phases sum within the job time, counts match the job.
        p1 = struct.unpack('<17I', read_in('prf1', 0xa2, 68, index=1)); p2 = struct.unpack('<17I', read_in('prf2', 0xa2, 68, index=2))
        assert p1[0] == 0x31465250 and p2[0] == 0x32465250, (hex(p1[0]), hex(p2[0]))
        prof = dict(zip(ip.PROFILE_ENGINE, p1[1:])); shim = dict(zip(ip.PROFILE_SHIM, p2[1:]))
        print('profile (model ms):', prof); print('shim (model ms):', shim)
        assert prof['total'] > 0 and prof['fills'] == expected_sweeps and prof['rows'] == rows_n - 38 and prof['bytes'] == len(document), prof
        assert sum(prof[k] for k in ('convert', 'fill', 'input-wait', 'prepare', 'fire', 'advance', 'load', 'eject', 'home', 'head', 'other')) == prof['total'], prof
        assert prof['prepare'] > 0 and prof['fire'] > 0 and prof['home'] == 0 and prof['head'] == 0, prof   # dry mode: no homing or head work
        assert prof['skipped'] == expected_skipped, (prof['skipped'], expected_skipped)
        assert expected_sweeps <= shim['cr-moves'] <= 2 * expected_sweeps and shim['lf-moves'] >= 1 and shim['arena'] <= 1091816, shim   # dry: no return before the first sweep, advances refuse without paper
        assert any(l.startswith('[print] profile ms: total') for l in lines) and any(l.startswith('[print] shim ms: cr') for l in lines), lines[-6:]
        if quality:
            quality.verify()
        body = ip.ipp_message(0x0009, 12, [(0x47, b'attributes-charset', b'utf-8'), (0x48, b'attributes-natural-language', b'en'),
                                           (0x45, b'printer-uri', b'ipp://localhost/ipp/print'), (0x21, b'job-id', b'\x00\x00\x00\x01')])
        status, headers, resp, _ = v.http(e, 1, 'POST', '/ipp/print', body, headers=('Content-Type: application/ipp',))
        ipp_status, attrs = ip.ipp_parse(resp)
        assert ipp_status == 0 and attrs['job-state'] == [(0x23, b'\x00\x00\x00\x09')], (ipp_status, attrs)
        print(f'PASS ipp print: Print-Job accepted (job 1 processing), engine completed 1 page in {expected_sweeps} sweeps over 2 bands ({expected_skipped} empty phases skipped), no allocation inside a live band, Get-Job-Attributes reports completed')
        return
    if os.environ.get('HYBRID_COLOUR_ONLY'):
        # Colour band: stock print mode 7 (C/M/Y groups 61 slots, black 81, 8-row groups), the
        # C/M/Y selection matrix groups filled, Q lanes 2/5/7 on managers 0/2/4. Records the
        # allocation, the builder's lane masks, lane-active bytes and the executor's lane DMA image.
        cols = int(os.environ['HYBRID_COLOUR_ONLY'])
        from unicorn.arm_const import UC_ARM_REG_R0 as _R0c, UC_ARM_REG_R6 as _R6c
        allocs = []
        def alloc_probe(cpu, a, sz, u):
            if a == 0xa109a: allocs.append({'request': cpu.reg_read(_R6c)})
            elif a == 0xa109e and allocs: allocs[-1]['result'] = hex(cpu.reg_read(_R0c))
            elif a == 0xa10a2: print(f'ALLOCATION FAILED for {allocs[-1] if allocs else "?"}: stock halts at 0xa10b2'); cpu.emu_stop(); e.cut = True
        for a in (0xa109a, 0xa109e, 0xa10a2): e.uc.hook_add(UC_HOOK_CODE, alloc_probe, begin=a, end=a)
        import atexit; atexit.register(lambda: print('pool allocations:', allocs))
        e.uc.hook_add(UC_HOOK_MEM_WRITE, mmio_write, begin=0xc0000000, end=0xcfffffff)
        npass = int(os.environ.get('HYBRID_COLOUR_PASSES', '2'))
        for k, fresh in [(1, True)] + [(i, False) for i in range(2, npass + 1)]:
            tag = f'c{cols}_pass{k}'
            mode_ = int(os.environ.get('HYBRID_COLOUR_MODE', '7'))
            command(f'band_params_{tag}', 0x9a, cols, 304 | 0x800 | (mode_ << 12)); command(f'band_place_{tag}', 0x9b, 152)
            rb = run_status()[2]; command(f'prepare_{tag}', 0x97, 5 | (0x400 if fresh else 0), 0, budget=200000000)
            for _ in range(400):
                st = run_status()
                if st[2] > rb: break
            print(f'{tag}: prepare result {st[1]:#x} errors {st[3]} Q {st[12]:#x}')
            mans = []
            for g in range(7):
                m = bytes(e.uc.mem_read(0x1879b09c + 0x1c * g, 0x1c)); sl, _, wd, sd, sz = struct.unpack_from('<HHHHI', m, 0); base = struct.unpack_from('<I', m, 0x18)[0]
                mans.append((g, sl, wd, sd, sz, hex(base)))
            print('  managers (group, slots, width, stride, size, base):', mans)
            if st[1] != 1: print('  shim log:'); [print('   ', l) for l in shim_log('[shim]')[-12:]]; break
            q = st[12]
            for i in (0, 1, 2, 5, 7):
                rows_q = [struct.unpack('<I', bytes(e.uc.mem_read(q + off + 4 * i, 4)))[0] for off in (0x11c, 0x144, 0x16c, 0x1e4)]
                masks = struct.unpack('<3I', bytes(e.uc.mem_read(q + 0x2ac + 12 * i, 12)))
                print(f'  Q lane {i}: start {rows_q[0]:#x} end {rows_q[1]:#x} stride {rows_q[2]} count {rows_q[3]} masks {[hex(x) for x in masks]}')
            print('  Q+350/354:', [hex(struct.unpack('<I', bytes(e.uc.mem_read(q + o, 4)))[0]) for o in (0x350, 0x354)])
            used = struct.unpack('<I', bytes(e.uc.mem_read(int(manifest['symbols']['application']['arena_used'], 16), 4)))[0]
            live = band_lifetime_begin(q, used)
            mmio_writes.clear(); rb = run_status()[2]; command(f'fire_{tag}', 0x97, 7 | 0x1000 | 0x2000, 0, budget=200000000)
            for _ in range(400):
                st = run_status()
                if st[2] > rb: break
            band_lifetime_check(tag, live)
            lanes = bytes(e.uc.mem_read(0x180cda70, 11))
            print(f'  executor result {st[1]:#x} errors {st[3]}; lane active bytes {lanes.hex()}')
            Path(f'/project/emu/output/fixture/writes_{tag}.json').write_text(json.dumps([(hex(a), hex(v)) for pc, a, v in mmio_writes if 0xc0c20000 <= a < 0xc0c2a000 or 0xc0c51000 <= a < 0xc0c52000]))
            for i in (0, 1, 2, 5, 7):
                print(f'  DMA lane {i}: addr {e.regs.get(0xc0c27000 + 4 * i)!r:>12} stride {e.regs.get(0xc0c27100 + 4 * i)} second {e.regs.get(0xc0c27200 + 4 * i)!r} extent {e.regs.get(0xc0c27300 + 4 * i)} masks {[hex(e.regs.get(0xc0c27570 + 0x10 * i + 4 * j, 0)) for j in range(3)]}')
            print('  window', hex(e.regs.get(0xc0c51114, 0)), hex(e.regs.get(0xc0c51108, 0)), 'lane windows', [hex(e.regs.get(0xc0c51700 + t + 4 * k, 0)) for t in (0, 0x100, 0x200, 0x300) for k in (0, 2, 5, 7)])
            print('  feed offsets 28300..', [e.regs.get(0xc0c28300 + 4 * k) for k in range(10)], 'sizes 28400..', [e.regs.get(0xc0c28400 + 4 * k) for k in range(10)])
            print('  trace:', [r for r in trace() if r[0] in ('WAIT_TIMEOUT', 'CR_MOVE')])
        return
    if os.environ.get('HYBRID_WIDE_ONLY'):
        cols = int(os.environ['HYBRID_WIDE_ONLY'])
        from unicorn.arm_const import UC_ARM_REG_R0 as _R0w, UC_ARM_REG_R1 as _R1w, UC_ARM_REG_R6 as _R6w
        allocs = []
        def alloc_probe(cpu, a, sz, u):
            if a == 0xa109a: allocs.append({'request': cpu.reg_read(_R6w)})
            elif a == 0xa109e and allocs: allocs[-1]['result'] = hex(cpu.reg_read(_R0w))
            elif a == 0xa10a2: print(f'ALLOCATION FAILED for {allocs[-1] if allocs else "?"} bytes: stock halts at 0xa10b2'); e.cut = True
        for a in (0xa109a, 0xa109e, 0xa10a2): e.uc.hook_add(UC_HOOK_CODE, alloc_probe, begin=a, end=a)
        if os.environ.get('HYBRID_ALLOC_TRACE'):
            # Every instruction between the allocation call and its return: function entries
            # (targets of bl) and the tail, to see which routine refuses a wide buffer.
            from unicorn.arm_const import UC_ARM_REG_R2 as _R2w, UC_ARM_REG_R3 as _R3w, UC_ARM_REG_LR as _LRw
            win = {'on': False, 'pcs': []}
            def alloc_trace(cpu, a, sz, u):
                if a == 0xa109a: win['on'] = True
                elif a == 0xa109e: win['on'] = False
                elif win['on'] and len(win['pcs']) < 400000: win['pcs'].append((a, cpu.reg_read(_R0w), cpu.reg_read(_R1w), cpu.reg_read(_R2w), cpu.reg_read(_R3w), cpu.reg_read(_LRw)))
            e.uc.hook_add(UC_HOOK_CODE, alloc_trace)
            import atexit
            def dump():
                pcs = win['pcs']; print('allocation window instructions:', len(pcs))
                entries = [(i, x) for i, x in enumerate(pcs) if i and pcs[i - 1][0] + 2 == x[5] - 1 or (i and x[5] & ~1 == pcs[i - 1][0] + 4)]
                print('function entries (index, pc, r0, r1, r2, r3, lr):'); [print('   ', i, hex(x[0]), hex(x[1]), hex(x[2]), hex(x[3]), hex(x[4]), hex(x[5])) for i, x in entries[:120]]
                print('tail (pc, r0, r1, r2, r3, lr):'); [print('   ', hex(x[0]), hex(x[1]), hex(x[2]), hex(x[3]), hex(x[4]), hex(x[5])) for x in pcs[-120:]]
            atexit.register(dump)
        e.uc.hook_add(UC_HOOK_MEM_WRITE, mmio_write, begin=0xc0000000, end=0xcfffffff)
        try:
            budget_ = int(os.environ.get('HYBRID_WIDE_BUDGET', '200000000'))
            a = wide_band(cols, 304, 152, f'w{cols}_pass1', budget=budget_)
            if os.environ.get('HYBRID_PASSES'):
                # Consecutive non-fresh passes as page_live runs them: each must prepare without a
                # stall, keep the window, and not wait on 0x1c:bit31 (stock's post-pass path runs now).
                for k in range(1, int(os.environ['HYBRID_PASSES'])):
                    r = wide_band(cols, 304, 152, f'w{cols}_pass{k + 1}', budget=budget_, fresh=False)
                    waits = [x for x in r['trace'] if x[0] == 'WAIT_TIMEOUT']
                    if hasattr(e, 'report_caps'): e.report_caps()
                    print(f'pass {k + 1}: result {r["result"]} errors {r["errors"]} window {r["regs"]["0xc0c51114"]}..{r["regs"]["0xc0c51108"]} masks {r["regs"]["0xc0c27570"]} {r["regs"]["0xc0c27574"]} waits {waits} moves {r["moves"]}')
            if os.environ.get('HYBRID_TWO_PASSES'):
                # Second band exactly as page_live does it: fresh prepare, executor again, same placement.
                b = wide_band(cols, 304, 152, f'w{cols}_pass2', budget=budget_, fresh=not os.environ.get('HYBRID_SECOND_NOT_FRESH'))
                wa, wb = [(x[1], x[2]) for x in a['writes']], [(x[1], x[2]) for x in b['writes']]
                print(f'pass1 writes {len(wa)}, pass2 writes {len(wb)}')
                first = next((i for i in range(min(len(wa), len(wb))) if wa[i] != wb[i]), None)
                print('first differing write index:', first, (wa[first] if first is not None else None, wb[first] if first is not None else None),
                      'pass1 pc', hex(a['writes'][first][0]) if first is not None else None)
                from collections import Counter as _C
                ca, cb = _C(wa), _C(wb)
                print('only in pass1:', [(hex(k[0]), hex(k[1]), n) for k, n in (ca - cb).items()][:60])
                print('only in pass2:', [(hex(k[0]), hex(k[1]), n) for k, n in (cb - ca).items()][:60])
                ta, tb = [r for r in a['trace'] if r[0] in ('WAIT_TIMEOUT', 'CR_MOVE', 'CR_PROFILE', 'CR_RELEASE', 'UNKNOWN', 'NOTIFY')], [r for r in b['trace'] if r[0] in ('WAIT_TIMEOUT', 'CR_MOVE', 'CR_PROFILE', 'CR_RELEASE', 'UNKNOWN', 'NOTIFY')]
                print('pass1 trace:', ta); print('pass2 trace:', tb)
                print('pass1 log:', a['log']); print('pass2 log:', b['log'])
        finally:
            print('pool allocation (0xa109a, size -> result):', allocs)
        return
    if os.environ.get('HYBRID_READ_CENSUS'):
        e.uc.hook_add(UC_HOOK_MEM_READ, mmio_read, begin=0xc0000000, end=0xcfffffff)
        e.uc.hook_add(UC_HOOK_MEM_WRITE, mmio_write, begin=0xc0000000, end=0xcfffffff)
    results = {}
    for routine, name in ((1, 'cap'), (3, 'wipe'), (2, 'uncap')):
        runs_before = run_status()[2]
        hook = e.uc.hook_add(UC_HOOK_CODE, ring_hook)
        try:
            command('run_' + name, 0x97, routine, 0)
            e.uc.hook_del(hook)
        except Exception as exc:
            from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_R0
            sym = manifest['symbols']['application']
            tn = struct.unpack('<I', bytes(e.uc.mem_read(int(sym['trace_n'], 16), 4)))[0]
            recs = [struct.unpack('<4I', bytes(e.uc.mem_read(int(sym['trace'], 16) + 16*i, 16))) for i in range(min(tn, 64))]
            print('FAILED during', name, repr(exc), 'pc', hex(e.uc.reg_read(UC_ARM_REG_PC)), 'lr', hex(e.uc.reg_read(UC_ARM_REG_LR)), 'sp', hex(e.uc.reg_read(UC_ARM_REG_SP)))
            print('trace_n', tn); [print('   ', (KIND.get(k, k), hex(a), hex(b), hex(c))) for k, a, b, c in recs[-12:]]
            print('stray flash writes:'); [print('   ', w) for w in stray]
            print('last pcs (pc, mode):', [(hex(a), hex(m)) for a, m in ring])
            raise
        # the routine runs inside the main loop; poll the run status until the count advances
        for _ in range(400):
            try:
                st = run_status()
            except AssertionError:
                print('run_status failed; resets', e.resets, 'entered', [hex(a) for a in e.entered])
                print('last pcs (pc, mode):', [(hex(a), hex(m)) for a, m in ring]); raise
            if st[2] > runs_before: break
        if st[2] != runs_before + 1:
            log = b''
            for _ in range(400):
                part = read_in('log', 0x50, 256)
                if not part: break
                log += part
            print('firmware log:\n' + log.decode(errors='replace'))
        assert st[2] == runs_before + 1, st
        rows = trace()
        results[name] = {'result': st[1], 'errors': st[3], 'trace': rows}
        if census:
            print(f'peripheral reads by stock code during {name} (pc, address, count, modeled values):')
            for (pc, a), n in sorted(census.items(), key=lambda kv: -kv[1])[:40]:
                print(f'   {pc:#x} {a:#x} x{n} {[hex(v) for v in sorted(census_values[(pc, a)])][:4]}')
            census.clear(); census_values.clear()
            print(f'peripheral writes by stock code during {name} (pc address value), {len(mmio_writes)}:')
            for pc, a, v in mmio_writes: print(f'   {pc:#x} {a:#x} = {v:#x}')
            mmio_writes.clear()
        print(f'{name}: result={st[1]} errors={st[3]} trace={len(rows)} records')
        for r in rows: print('   ', r)
    cap = results['cap']; wipe = results['wipe']
    assert cap['result'] == 1 and cap['errors'] == 0, cap
    assert ('CR_MOVE', 2688, 3, 0) in cap['trace'] and any(r[0] == 'CR_PROFILE' and r[1] == 0x2005 for r in cap['trace']), cap['trace']
    # the cap-state publication's bit comes through the NVM overlay, which the stand-in answers with 0
    assert any(r[0] == 'STATUS_PUB' and r[1] == 0xf005 and r[2] == 2 for r in cap['trace']), [r for r in cap['trace'] if r[0] != 'STATUS_GET']
    assert wipe['result'] == 1 and ('CR_MOVE', 16640, 3, 0) in wipe['trace'] and any(r[0] == 'CR_RELEASE' for r in wipe['trace']), wipe
    print('PASS shim: cap and wipe ran in place with our services (dry motion), request sequence as in the fixture')
    # stock paper load (routine 8) with the stock LF task and servo against the LF model
    table = [(i, struct.unpack('<H', bytes(e.uc.mem_read(0x180d571e + 2 * i, 2)))[0]) for i in range(256)]
    print('resource table 0x180d571e nonzero entries before feed:', [(i, v) for i, v in table if v][:20])
    runs_before = run_status()[2]
    feed_probe = []
    def feed_probe_hook(cpu, address, size, user):
        from unicorn.arm_const import UC_ARM_REG_LR as _L, UC_ARM_REG_SP as _S, UC_ARM_REG_R0 as _R0, UC_ARM_REG_R1 as _R1
        if len(feed_probe) < 40:
            sp = cpu.reg_read(_S); stack = struct.unpack('<16I', bytes(cpu.mem_read(sp, 64)))
            feed_probe.append((hex(address), hex(cpu.reg_read(_L)), hex(cpu.reg_read(_R0)), hex(cpu.reg_read(_R1)), [hex(x) for x in stack if 0x10000 <= x < 0x1f2000][:8]))
    FEED_PROBES = {0xdce50: 'load', 0xdcd2a: 'prepare', 0xc7126: 'cond. maintenance', 0xc6ec0: 'maint a', 0xc7a20: 'maint b', 0xc87d0: 'c87d0', 0xc6414: 'leave cap', 0xe97f0: 'resource wait', 0xdd8ce: 'record select', 0xdcb18: 'pickup', 0xdca82: 'align', 0xdc95c: 'dc95c', 0xd80f6: 'lf adjust'}
    for a in FEED_PROBES: e.uc.hook_add(UC_HOOK_CODE, feed_probe_hook, begin=a, end=a)
    loop_hits = [0]
    def loop_hook(cpu, address, size, user): loop_hits[0] += 1
    e.uc.hook_add(UC_HOOK_CODE, loop_hook, begin=0xc87f0, end=0xc87f0)
    if _ring_handle is not None: e.uc.hook_del(_ring_handle)   # the per-instruction debug ring makes the servo loop 20x slower
    try:
        command('run_feed', 0x97, 8 | 0x800 | 0x400, 0, budget=60000000)   # fresh snapshot
    except Exception as exc:
        from unicorn.arm_const import UC_ARM_REG_LR as _L, UC_ARM_REG_SP as _S
        print('FAILED during feed', repr(exc)[:300], 'pc', hex(e.uc.reg_read(PC)), 'lr', hex(e.uc.reg_read(_L)), 'sp', hex(e.uc.reg_read(_S)))
        print('feed probes (entry, lr, r0, r1, stack code words):'); [print('   ', FEED_PROBES.get(int(x[0], 16), '?'), x) for x in feed_probe]
        print('0xc87f0 loop iterations:', loop_hits[0])
        print('LF timer state:', struct.unpack('<HH', bytes(e.uc.mem_read(0x180c6edc, 4))), 'countdown', struct.unpack('<H', bytes(e.uc.mem_read(0x180d571e, 2))), 'model pos', hex(e.pos), 'cmd writes', len(e.lf_cmd_writes), e.lf_cmd_writes[-6:])
        sym = manifest['symbols']['application']
        tn = struct.unpack('<I', bytes(e.uc.mem_read(int(sym['trace_n'], 16), 4)))[0]
        recs = [struct.unpack('<4I', bytes(e.uc.mem_read(int(sym['trace'], 16) + 16*i, 16))) for i in range(min(tn, 160))]
        print('trace_n', tn); [print('   ', (KIND.get(k, k), hex(a), hex(b), hex(c))) for k, a, b, c in recs[-14:]]
        raise
    import time as _time
    t_feed = _time.monotonic(); polls = 0
    last_report = _time.monotonic()
    while True:
        st = run_status(); polls += 1
        if st[2] > runs_before: break
        if _time.monotonic() - last_report > 30:
            last_report = _time.monotonic()
            print(f'  feed in progress: {int(last_report - t_feed)} s, trace_n {st[4]}, errors {st[3]}, model pos {e.pos & 0xfffff:#x} present={e.present} reverse={e.reverse} forward={e.forward} lf cmd writes {len(e.lf_cmd_writes)} last {e.lf_cmd_writes[-3:]}', flush=True)
        if _time.monotonic() - t_feed > 300:
            rows = trace()
            print(f'FEED TIMEOUT after {polls} polls: status {st}; trace tail:'); [print('   ', r) for r in rows[-12:]]
            print(f'model: present={e.present} pos={e.pos:#x} reverse={e.reverse} forward={e.forward} lf cmd writes={len(e.lf_cmd_writes)} last={e.lf_cmd_writes[-5:]}')
            print('LF timer state:', struct.unpack('<HH', bytes(e.uc.mem_read(0x180c6edc, 4))), 'countdown', struct.unpack('<H', bytes(e.uc.mem_read(0x180d571e, 2))))
            log = b''
            for _ in range(400):
                part = read_in('log', 0x50, 256)
                if not part: break
                log += part
            print('\n'.join(l for l in log.decode(errors='replace').splitlines() if 'shim' in l or 'feed' in l)[-2500:])
            raise SystemExit('feed did not finish')
    rows = trace()
    log = b''
    for _ in range(400):
        part = read_in('log', 0x50, 256)
        if not part: break
        log += part
    print('shim log during feed:'); print('\n'.join('   ' + l for l in log.decode(errors='replace').splitlines() if '[shim]' in l and 'STATUS' not in l)[-4000:])
    print('feed probes (entry, lr, r0, r1, stack code words):'); [print('   ', FEED_PROBES.get(int(x[0], 16), '?'), x) for x in feed_probe]
    print('0xc87f0 loop iterations:', loop_hits[0], 'head engine arms:', getattr(e, 'he_arms', 0))
    print(f'feed: result={st[1]:#x} errors={st[3]} trace={len(rows)}; LF moves: {[r for r in rows if r[0] == "LF_MOVE"]}; CR moves: {[r for r in rows if r[0] == "CR_MOVE"]}')
    print(f'feed model: present={e.present} pos={e.pos:#x} reverse={e.reverse} forward={e.forward} lf command writes={len(e.lf_cmd_writes)} range={min(e.lf_cmd_writes) if e.lf_cmd_writes else None}..{max(e.lf_cmd_writes) if e.lf_cmd_writes else None}')
    print('feed trace tail:'); [print('   ', r) for r in rows[-16:]]
    results['feed'] = {'result': st[1], 'errors': st[3], 'trace': rows, 'present': e.present, 'lf_writes': len(e.lf_cmd_writes)}
    feed_ok = st[1] == 0 and len(e.lf_cmd_writes) > 100 and any(r[0] == 'LF_MOVE' and r[1] == 2050 for r in rows) and any(r[0] == 'LF_MOVE' and r[1] == 646 for r in rows)
    print(('PASS' if feed_ok else 'NOTE') + f' feed: stock paper load ran in place with the stock LF servo (model present={e.present}; hardware verified 2026-10-26)')
    # band preparation: allocation, raster worker (converter not modeled), Q builder
    runs_before = run_status()[2]
    hook = e.uc.hook_add(UC_HOOK_CODE, ring_hook)
    try:
        command('run_band', 0x97, 5, 0)
    except Exception as exc:
        print('FAILED during band-prepare', repr(exc), 'pc', hex(e.uc.reg_read(PC)))
        print('last pcs (pc, mode):', [(hex(a), hex(m)) for a, m in ring])
        sym = manifest['symbols']['application']
        tn = struct.unpack('<I', bytes(e.uc.mem_read(int(sym['trace_n'], 16), 4)))[0]
        recs = [struct.unpack('<4I', bytes(e.uc.mem_read(int(sym['trace'], 16) + 16*i, 16))) for i in range(min(tn, 160))]
        print('trace_n', tn); [print('   ', (KIND.get(k, k), hex(a), hex(b), hex(c))) for k, a, b, c in recs[-10:]]
        raise
    e.uc.hook_del(hook)
    for _ in range(400):
        st = run_status()
        if st[2] > runs_before: break
    assert st[2] == runs_before + 1 and st[1] == 1, st
    q, base = st[12], st[13]
    print(f'band-prepare: result={st[1]} Q={q:#x} manager6 base={base:#x} adc14={st[14]} a8422={st[15]}')
    assert q and base, st
    m6 = bytes(e.uc.mem_read(0x1879b144, 0x1c)); slots, _, width, stride = struct.unpack_from('<HHHH', m6, 0)
    assert (slots, width, stride) == (80, 320, 1280), (slots, width, stride)
    results['band_prepare'] = {'q': hex(q), 'base': hex(base), 'manager6': m6.hex()}
    print('PASS band-prepare: stock allocation, raster worker and Q builder ran in place (manager 6: 80 slots, width 320, stride 1280)')
    if census or mmio_writes:
        print(f'peripheral writes by stock code during band-prepare: {len(mmio_writes)} (not listed)')
        census.clear(); census_values.clear(); mmio_writes.clear()
    # band-run: brief 11 recipe + lane preparation + executor with the dry carriage adapter
    runs_before = run_status()[2]
    from unicorn.arm_const import UC_ARM_REG_R0 as _R0
    def gate_probe(cpu, a, sz, u):
        if a == 0xd1e18: print(f'GATE 0xcd6b6 (executor enable) -> {cpu.reg_read(_R0):#x}, byte 0x180d578e = {bytes(e.uc.mem_read(0x180d578e, 1))[0]}')
        if a == 0xd1e20: print(f'GATE 0xebbac -> {cpu.reg_read(_R0):#x}, S20 = {struct.unpack("<I", bytes(e.uc.mem_read(0x18066300, 4)))[0]:#x}, bank200 = {struct.unpack("<I", bytes(e.uc.mem_read(0x18066310, 4)))[0]:#x}, platform = {bytes(e.uc.mem_read(0x180d57e9, 1))[0]}')
        if a == 0xd1f7e: print('GATE d1f70 reached (after d2a9c)')
        if a == 0xe3c76: print('GATE executor submit site 0xe3c76 reached')
    for a in (0xd1e18, 0xd1e20, 0xd1f7e, 0xe3c76): e.uc.hook_add(UC_HOOK_CODE, gate_probe, begin=a, end=a)
    CHAIN = {0xe6b80: 'e6b80 executor', 0xe3a1c: 'e3a1c lane-flag gate', 0xe402a: 'e402a', 0xe7094: 'e7094', 0xe703e: 'e703e', 0xe40ee: 'e40ee position prep', 0xd227e: 'd227e CR wait',
             0xca372: 'ca372', 0xe7074: 'e7074', 0xe3be6: 'e3be6', 0xe4574: 'e4574 register image', 0xe419c: 'e419c', 0xe3c14: 'e3c14 pass config', 0xe458c: 'e458c', 0xe270a: 'e270a failure cleanup',
             0xe5c06: 'e5c06 cold prep', 0xe1984: 'e1984 owner acquire', 0xddfb6: 'ddfb6', 0xe6c2a: 'e6c2a', 0xe6ba0: 'e6ba0 return 1'}
    seen = []
    from unicorn.arm_const import UC_ARM_REG_LR as _LR2
    def chain_probe(cpu, a, sz, u):
        if a in CHAIN and len(seen) < 60: seen.append((CHAIN[a], hex(cpu.reg_read(_LR2)), hex(cpu.reg_read(_R0))))
    for a in CHAIN: e.uc.hook_add(UC_HOOK_CODE, chain_probe, begin=a, end=a)
    command('run_bandrun', 0x97, 6, 0)
    last_tn = -1; stuck = 0
    for _ in range(80):
        st = run_status()
        if st[2] > runs_before: break
        stuck = stuck + 1 if st[4] == last_tn else 0; last_tn = st[4]
        if stuck > 20:
            print('band-run appears stuck; trace so far:'); [print('   ', r) for r in trace()]
            print('firmware log tail:'); log = b''
            for _ in range(400):
                part = read_in('log', 0x50, 256)
                if not part: break
                log += part
            print('\n'.join(l for l in log.decode(errors='replace').splitlines() if 'shim' in l)[-3000:])
            break
    rows = trace()
    print('executor chain entries (name, lr, r0):'); [print('   ', x) for x in seen]
    print('band-run trace (last records):'); [print('   ', r) for r in rows]
    assert st[2] == runs_before + 1, st
    moves = [r for r in rows if r[0] == 'CR_MOVE']
    print(f'band-run: result={st[1]} errors={st[3]} trace={len(rows)}; CR moves: {moves}')
    probe = {}
    for name, a, n in (('exec_enable_180d578e', 0x180d578c, 4), ('platform_180d57e9', 0x180d57e8, 4), ('S20_18066300', 0x18066300, 4), ('bank200_18066310', 0x18066310, 4), ('bank400', 0x18066314, 4), ('A_target_180cda4c', 0x180cda4c, 4), ('A_profile', 0x180cda54, 4), ('lanes', 0x180cda70, 4)):
        probe[name] = hex(struct.unpack('<I', bytes(e.uc.mem_read(a, 4)))[0])
    print('gate inputs after band-run:', probe)
    if census or mmio_writes:
        print('peripheral reads by stock code during band-run (pc, address, count, modeled values):')
        for (pc, a), n in sorted(census.items(), key=lambda kv: -kv[1])[:60]:
            print(f'   {pc:#x} {a:#x} x{n} {[hex(v) for v in sorted(census_values[(pc, a)])][:4]}')
        census.clear(); census_values.clear()
        print(f'peripheral writes by stock code during band-run (pc address value), {len(mmio_writes)}:')
        for pc, a, v in mmio_writes: print(f'   {pc:#x} {a:#x} = {v:#x}')
        mmio_writes.clear()
    command('unlock2', 0x93, KEY)
    r = call(0xebbad); print('0xebbac() ->', hex(r[2]))
    waits = [r for r in rows if r[0] == 'WAIT_TIMEOUT']
    print(f'band-run: wait timeouts (dry, expected): {waits}')
    results['band_run'] = {'result': st[1], 'moves': moves, 'timeouts': waits, 'trace': rows}
    assert any(r[0] == 'UNKNOWN' and r[1] == 0xe6aea and r[3] & 0xffff == 0x0101 for r in rows), rows   # lanes 1,1 after e3ad6
    assert any(r[0] == 'UNKNOWN' and r[1] == 0xe6b80 for r in rows), rows                              # executor returned
    print('PASS band-run (dry): lane preparation gave BK lanes active and the stock band executor ran in place to a return')
    # routine 7: executor again on the already prepared band (the host uploads its own raster between 5 and 7)
    runs_before = run_status()[2]
    command('run_bandfire', 0x97, 7, 0)
    for _ in range(400):
        st = run_status()
        if st[2] > runs_before: break
    assert st[2] == runs_before + 1, st
    rows = trace(); moves = [r for r in rows if r[0] == 'CR_MOVE']
    print(f'band-fire (routine 7, no re-prepare): result={st[1]} errors={st[3]}; CR moves: {moves}')
    results['band_fire'] = {'result': st[1], 'moves': moves, 'trace': rows}
    assert st[1] == 1 and moves, rows
    print('PASS band-fire (dry): the executor re-ran on the prepared band without a new preparation')
    # stock CR task for the pass (emulator: no homed carriage, flag 0x2000): where does the body go?
    cr_probe = []
    def cr_probe_hook(cpu, address, size, user):
        from unicorn.arm_const import UC_ARM_REG_LR as _L, UC_ARM_REG_R0 as _R0, UC_ARM_REG_R1 as _R1
        if address == 0xd5b7a: return
        if len(cr_probe) < 60: cr_probe.append((hex(address), hex(cpu.reg_read(_L)), hex(cpu.reg_read(_R0)), hex(cpu.reg_read(_R1))))
    CR_PROBES = [0xd4820, 0xd4cda, 0xd4e6e, 0xd49c4, 0xd33ae, 0xd3418, 0x109ec4, 0xd4c6c, 0xd48e0, 0xd4948, 0xd4958, 0xd494c, 0xd494e, 0xd48aa, 0xd385a, 0xd44c6, 0xd369a, 0xd4328, 0xd3dfc,
                 0xd4a42, 0xd4ab6, 0xd33f8, 0xd41ee, 0xd4f48, 0xd2788, 0xd49b2, 0xd499a, 0xd4a30, 0xd4952, 0xd4b68, 0xd2966]
    e.regs[0xc0c51100] = 0xac0        # the board's raw CR counter at the park frame (stock's capped position 2688 - 64)
    for a in CR_PROBES: e.uc.hook_add(UC_HOOK_CODE, cr_probe_hook, begin=a, end=a)
    runs_before = run_status()[2]
    command('run_prepare_fresh2', 0x97, 5 | 0x400, 0, budget=60000000)
    for _ in range(400):
        st = run_status()
        if st[2] > runs_before: break
    runs_before = run_status()[2]
    command('run_bandfire_crstock', 0x97, 7 | 0x1000 | 0x2000, 0, budget=60000000)
    for _ in range(400):
        st = run_status()
        if st[2] > runs_before: break
    rows = trace()
    print(f'band-fire with stock CR task: result={st[1]:#x} errors={st[3]}; CR records: {[r for r in rows if r[0] in ("CR_MOVE", "CR_PROFILE", "CR_RELEASE")]}')
    print('CR probes (entry, lr, r0, r1):'); [print('   ', x) for x in cr_probe]
    log = b''
    for _ in range(400):
        part = read_in('log', 0x50, 256)
        if not part: break
        log += part
    print('shim log (CR lines):'); print('\n'.join('   ' + l for l in log.decode(errors='replace').splitlines() if 'CR' in l or 'routine 7' in l)[-2500:])
    print('CR timer 0xc0140200 =', hex(e.regs.get(0xc0140200, 0)), ' CR drive 0xc0c50000 =', hex(e.regs.get(0xc0c50000, 0)))
    ck = {'head_state': e.uc.mem_read(0x180cd401, 1)[0], 'owner': e.uc.mem_read(0x180cd405, 1)[0],
          '87000': e.regs.get(0xc0c87000), '8700c': e.regs.get(0xc0c8700c), '87010': e.regs.get(0xc0c87010), '812c0': e.regs.get(0xc0c812c0),
          '295f0': e.regs.get(0xc0c295f0), '51400': e.regs.get(0xc0c51400), '28000': e.regs.get(0xc0c28000), '28080': e.regs.get(0xc0c28080), '51d04': e.regs.get(0xc0c51d04), '51d0c': e.regs.get(0xc0c51d0c)}
    print('brief 19 checkpoints after the stock-CR pass:', ck)
    assert ck['87000'] == 1 and ck['8700c'] == 4 and ck['87010'] == 7 and (ck['812c0'] or 0) & 1, ck    # the real owner-3 acquire e1984 ran
    print('PASS owner-3 acquire: stock e1984 programmed the 0xc0c87000 block and set 0xc0c812c0 bit 0 before the sweep')
    # Wide band (page printing): the BK selection matrix maps its 40 eight-row entries to
    # raster slots 0..39, the manager is allocated 4576 columns wide, and the stock common
    # placement comes from the host. Records the allocation, the builder's lane masks, the
    # raster-feed lane extents and the CR windows; a second placement measures the window shift.
    N = 4224
    wide = wide_band(N, 304, 152, 'p152')
    results['wide_band_p152'] = wide
    assert (wide['slots'], wide['width'], wide['stride'], wide['size']) == (80, N, 1280, N // 16 * 1280), wide
    assert wide['result'] == 1 and int(wide['lanes'], 16) & 0xffff == 0x0101, wide
    B = int(wide['base'], 16) + 0x780
    assert int(wide['regs']['0xc0c27000'], 16) == (B + 63) & ~63 and int(wide['regs']['0xc0c27100'], 16) == 1280, wide['regs']   # 64-byte aligned raster start
    assert wide['masks'][0] == wide['masks'][1] == ['0xfffffffc', '0xff', '0x0'], wide['masks']          # entries 2..39 -> bits 2..39 (no first-pass shift: 0x1802ef84 cleared, brief 20 §2)
    assert wide['regs']['0xc0c27570'] == '0x3' and wide['regs']['0xc0c27574'] == '0xffffff00', wide['regs']   # lane DMA mask: groups 2..39 enabled
    start, end = int(wide['regs']['0xc0c51114'], 16), int(wide['regs']['0xc0c51108'], 16)
    assert start == 4 * 152 + 34272 and end == start + 16 * N + 640, (start, end)   # stock's own cached common offset (band_run no longer zeroes it on non-fresh runs)
    print(f'PASS wide band: manager 6 allocated {N} columns, BK lanes active, DMA base at the raster storage, 38 nozzle groups selected, CR window {start}..{end} = 4*place+34272 .. +16N+640')
    wide2 = wide_band(N, 304, 1152, 'p1152')
    results['wide_band_p1152'] = wide2
    start2 = int(wide2['regs']['0xc0c51114'], 16)
    assert abs(start2 - (start + 4000)) <= 64 and start2 % 64 == 0, (start, start2)   # 4 counts per placement unit, window start quantized to 64 counts
    print(f'PASS wide band placement: +1000 in Q+0x3a moved the CR window start by {start2 - start} raw counts (4 per unit, quantized to 64)')
    results['wide_band_window'] = {'start_p152': start, 'end_p152': end, 'shift_per_1000_place': start2 - start}
    command('band_params_back', 0x9a, 320, 8); command('band_place_back', 0x9b, 5000)
    if os.environ.get('HYBRID_POSITION_PROBE'):
        # Where does the executor learn the carriage's current position? Log RAM reads of the
        # previous target (40036 = 0x9c64 now) during a third run, then poke candidates.
        from unicorn import UC_HOOK_MEM_READ
        reads = Counter()
        def probe(cpu, access, address, size, value, user):
            pc = cpu.reg_read(PC)
            if pc < 0x200000 and size == 4:
                v = struct.unpack('<I', bytes(cpu.mem_read(address, 4)))[0]
                if v == 40036: reads[(pc, address)] += 1
        h = e.uc.hook_add(UC_HOOK_MEM_READ, probe, begin=0x18000000, end=0x187fffff)
        runs_before = run_status()[2]; command('run_bandfire2', 0x97, 7, 0)
        for _ in range(400):
            st = run_status()
            if st[2] > runs_before: break
        e.uc.hook_del(h)
        rows = trace(); print('third run moves:', [r for r in rows if r[0] == 'CR_MOVE'])
        print('reads of the previous target (pc, address, count):'); [print('   ', hex(pc), hex(a), n) for (pc, a), n in sorted(reads.items())]
        def run7(tag):
            rb = run_status()[2]; command(tag, 0x97, 7, 0)
            for _ in range(400):
                st = run_status()
                if st[2] > rb: break
            rows = trace(); A = bytes(e.uc.mem_read(0x180cda34, 0x60))
            print(f'{tag}: moves {[r for r in rows if r[0] == "CR_MOVE"]} A+1c/1d={A[0x1c]}/{A[0x1d]} bounds={[hex(struct.unpack("<I", A[i:i+4])[0]) for i in (0, 4, 8, 0xc)]} approach={hex(struct.unpack("<I", A[0x10:0x14])[0])}/{hex(struct.unpack("<I", A[0x14:0x18])[0])} Q+54={e.uc.mem_read(st[12] + 0x54, 1)[0]}')
        A = bytes(e.uc.mem_read(0x180cda34, 0x60)); print('A after run 3: +1c/1d', A[0x1c], A[0x1d])
        e.uc.mem_write(0x180cda51, b'\x01'); run7('poke_A1d_1')
        e.uc.mem_write(0x180cda34, bytes(0x60)); run7('zero_A')
        rb = run_status()[2]; command('prepare_fresh', 0x97, 5 | 0x400, 0)
        for _ in range(400):
            st = run_status()
            if st[2] > rb: break
        print('fresh prepare result', st[1])
        run7('after_fresh_prepare'); run7('second_after_fresh')
        for addr in [a for (pc, a) in reads if a != 0x180cda4c] + [0x180cda4c]:
            old_v = e.uc.mem_read(addr, 4)
            e.uc.mem_write(addr, struct.pack('<I', 2688))
            runs_before = run_status()[2]; command('run_bandfire3', 0x97, 7, 0)
            for _ in range(400):
                st = run_status()
                if st[2] > runs_before: break
            rows = trace(); print(f'poke {addr:#x} = 2688 -> moves', [r for r in rows if r[0] == 'CR_MOVE'], 'A+18', hex(struct.unpack('<I', bytes(e.uc.mem_read(0x180cda4c, 4)))[0]))
    # Bounded forward-step API: reject bad lengths, do not move without paper,
    # and pass exactly the requested length/profile/direction to stock. Dry
    # requests exercise the compiled wrapper without claiming physical travel.
    from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3
    advance_calls = []
    def advance_request(cpu, address, size, user):
        advance_calls.append([cpu.reg_read(r) for r in
                              (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)])
    ah = e.uc.hook_add(UC_HOOK_CODE, advance_request, begin=0xd614c, end=0xd614c)
    arms_before = getattr(e, 'he_arms', 0)
    command('advance_zero', 0x97, 10, 0, expect_stall=True)
    command('advance_too_far', 0x97, 10, 1201, expect_stall=True)
    for distance in (200, 1200):
        before = run_status()[2]
        command('advance_dry', 0x97, 10 | 0x400, distance, budget=60000000)
        for _ in range(300):
            st = run_status()
            if st[2] > before: break
        assert st[2] == before + 1 and st[1] == 0 and st[11] == 0, st
    assert advance_calls == [[200, 6, 0, 0x20], [1200, 6, 0, 0x20]], advance_calls
    e.present = False
    before = run_status()[2]
    command('advance_no_paper', 0x97, 10 | 0x800, 200)
    for _ in range(100):
        st = run_status()
        if st[2] > before: break
    assert st[2] == before + 1 and st[1] == 0xbada and len(advance_calls) == 2, st
    assert getattr(e, 'he_arms', 0) == arms_before, 'paper step armed head work'
    e.uc.hook_del(ah)
    results['advance'] = {'stock_requests': advance_calls, 'no_paper_result': hex(st[1]),
                          'head_arms': 0, 'physical_travel_modeled': False}
    print('PASS forward step: bounded lengths, stock profile 6/direction 0, paper guard, no head work')
    # Physical panel path: the hybrid's default map must call the stock LF
    # step, once per press, without homing the carriage or firing the head.
    panel_key = 0xc0c8d028
    assert struct.unpack('<I', bytes(e.uc.mem_read(int(manifest['symbols']['application']['button_map'], 16), 4)))[0] == 2
    advance_calls.clear()
    ah = e.uc.hook_add(UC_HOOK_CODE, advance_request, begin=0xd614c, end=0xd614c)
    e.present = True; e.forward = 0
    e.regs[panel_key] = 0
    for _ in range(12): run_status()
    before = run_status()[2]
    e.regs[panel_key] = 0x400
    for _ in range(400):
        st = run_status()
        if st[2] > before: break
    assert st[2] == before + 1 and st[1] == 0 and st[11] == 0, st
    assert advance_calls == [[400, 6, 0, 0x20]], advance_calls
    assert not any(row[0] == 'CR_MOVE' for row in trace())
    for _ in range(30): run_status()       # holding the key must not repeat
    assert run_status()[2] == before + 1 and len(advance_calls) == 1
    e.regs[panel_key] = 0
    for _ in range(12): run_status()

    # A second physical press starts a new step; inject Cancel after its
    # first servo tick. Stock timer stop and neutral/disable must win.
    cancelled = []
    def cancel_during_step(cpu, address, size, user):
        if not cancelled:
            cancelled.append(True); e.regs[panel_key] = 0x20
    ch = e.uc.hook_add(UC_HOOK_CODE, cancel_during_step, begin=0xdbf0c, end=0xdbf0c)
    e.present = True; e.forward = 0  # independent held-sheet fixture for cancellation
    before = run_status()[2]
    e.regs[panel_key] = 0x400
    for _ in range(400):
        st = run_status()
        if st[2] > before: break
    assert cancelled and st[2] == before + 1 and st[1] == 0xbadb and st[11] == 0, st
    assert advance_calls == [[400, 6, 0, 0x20]] * 2, advance_calls
    assert e.regs.get(0xc0c52000) == 0 and e.regs.get(0xc0c52008) == 2000
    assert bytes(e.uc.mem_read(0x180c6ede, 2)) == b'\0\0'
    assert not any(row[0] == 'CR_MOVE' for row in trace())
    assert getattr(e, 'he_arms', 0) == arms_before
    e.uc.hook_del(ch); e.uc.hook_del(ah); e.regs[panel_key] = 0
    results['panel_advance'] = {'default_map': 2, 'stock_requests': advance_calls,
                               'held_key_repeats': 0, 'cancel_result': hex(st[1]),
                               'carriage_requests': 0, 'head_arms': 0}
    print('PASS panel: Black Copy advances once, held key does not repeat, Cancel stops stock LF; no carriage request or head work')
    assert bytes(e.uc.mem_read(0, 0x200000)) == image, 'flash changed'
    (BUILD / 'validation_hybrid.json').write_text(json.dumps({
        'info': [hex(v) for v in info], 'calls': r[3], 'thresholds': thresholds, 'shim': results,
        'summary': e.summary(), 'manifest_package_sha256': manifest['package_sha256']}, indent=1, default=str))
    print('PASS hybrid')


if __name__ == '__main__':
    main()
