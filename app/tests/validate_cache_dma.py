#!/usr/bin/env python3
"""Check compiled MPU transitions and USB DMA placement, without hardware.

Unicorn does not simulate stale cache lines. A small CP15 observer records
the actual MCR operands and supplies control-register reads; these assertions
check the memory contract, not physical cache coherence or USB timing.
"""
import argparse
import struct
from pathlib import Path

from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC
import validate_escl as v
from validate_colour_continuous import ColourHost, call


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build', type=Path, required=True)
    args = p.parse_args()
    factory, _, meta = v.factory_and_metadata(args.build)
    meta['build'] = str(args.build)
    e = ColourHost(image=factory, metadata=meta)
    e.run(v.app_mode)
    syms = {k: int(n, 16) for k, n in meta['symbols']['application'].items()}
    lo, hi = syms['__usb_dma_start'], syms['__usb_dma_end']
    assert 0x20400000 <= lo < hi <= 0x20420000
    assert syms['__bss_start'] <= lo and syms['__bss_end'] >= hi
    assert syms['__ordinary_bss_end'] <= lo
    for name, length in [('setup_buffer', 64), ('control_buffer', 256),
                         ('bulk_buffer', 512), ('packets', 1024), ('tx_buffers', 32768)]:
        address = syms[name]
        assert address % 32 == 0 and lo <= address < address + length <= hi, name
    print('PASS all mutable USB DMA buffers outside cached RAM and inside app reservation', flush=True)

    cp = {(1, 0, 0): 0x78}
    writes = []
    registers = [UC_ARM_REG_R0 + n for n in range(13)] + [UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC]

    def full_access(address, instruction):
        if not cp.get((1, 0, 0), 0) & 1:
            return True
        for n in reversed(range(8)):
            value = cp.get((6, n, 0), 0)
            if not value & 1:
                continue
            size = 1 << (((value >> 1) & 31) + 1)
            base = value & ~(size - 1)
            if base <= address < base + size:
                return (cp.get((5, 0, 3 if instruction else 2), 0) >> (4 * n)) & 15 == 3
        return False

    def observe(cpu, address, size, _):
        ins = struct.unpack('<I', cpu.mem_read(address, 4))[0]
        if ins & 0x0f000f10 != 0x0e000f10:
            return
        key = ((ins >> 16) & 15, ins & 15, (ins >> 5) & 7)
        reg = registers[(ins >> 12) & 15]
        if ins & (1 << 20):
            assert key == (1, 0, 0), key
            cpu.reg_write(reg, cp[key])
        else:
            value = cpu.reg_read(reg)
            cp[key] = value
            writes.append((key, value))
            # MPU permission changes are live even with D-cache disabled.
            # Never enable an overriding region before granting its access:
            # the next instruction fetch or stack access could fault midway
            # through setup (Unicorn itself does not enforce this MPU).
            assert full_access(address + size, True), ('code lost access during MPU setup', key, hex(address))
            assert full_access(cpu.reg_read(UC_ARM_REG_SP), False), ('stack lost access during MPU setup', key)
        cpu.reg_write(UC_ARM_REG_PC, address + size)

    # Hook only cache functions, not every pixel/motor instruction.
    ordered = sorted(set(syms.values()))
    for name in ('cpu_icache_set', 'cpu_dcache_set'):
        start = syms[name]
        end = next(n for n in ordered if n > start)
        e.uc.hook_add(UC_HOOK_CODE, observe, begin=start, end=end - 1)

    # Simulate leftover regions across a warm application restart.
    for n in range(1, 8):
        cp[6, n, 0] = 0x2000002b
    for cycle in range(3):
        assert call(e, 'cpu_icache_set', 1)
        assert all(cp[6, n, 0] == 0 for n in range(1, 8))
        assert cp[5, 0, 2] == cp[5, 0, 3] == 3
        assert call(e, 'cpu_dcache_set', 1)
        assert cp[1, 0, 0] & 0x1005 == 0x1005
        assert cp[2, 0, 0] == 6 and cp[3, 0, 0] == 0
        assert cp[5, 0, 2] == cp[5, 0, 3] == 0x333
        for region in (1, 2):
            val = cp[6, region, 0]
            size = 1 << (((val >> 1) & 31) + 1)
            base = val & ~(size - 1)
            assert hi <= base or lo >= base + size, (region, hex(base), size)
        before = len(writes)
        assert call(e, 'cpu_dcache_set', 1)
        assert len(writes) == before, 'enabling active cache discarded live lines'
        assert not call(e, 'cpu_icache_set', 0), 'I-cache switch allowed during data caching'
        for c in (0, 1):
            for path in ('ScannerCapabilities', 'ScannerStatus'):
                status, _, body, _ = v.http(e, c, 'GET', '/eSCL/' + path)
                assert status == 200 and body
        assert call(e, 'cpu_dcache_set', 0)
        assert not cp[1, 0, 0] & 4
        assert call(e, 'cpu_icache_set', 0)
        assert not cp[1, 0, 0] & 0x1001
    assert ((7, 10, 4), 0) in writes, 'missing write-buffer drain'
    assert not e.erases and not e.programs
    print('PASS repeated cache transitions, inherited regions, and both HTTP interfaces', flush=True)


if __name__ == '__main__':
    main()
