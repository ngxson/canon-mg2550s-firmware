#!/usr/bin/env python3
"""Compare compiled band preparation with stock's raster-feed initializer.

Runs stock 0xe5ba4 to its return, then the candidate's band_prepare up to
entry to the stock allocator. No motor/head behavior is modeled. This checks
the ordered MMIO writes, including zero writes, independently of the C source.
Run under the project's Linux emulator image (Unicorn).
"""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_PROT_ALL
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_LR

ROOT = Path(__file__).resolve().parents[2]
RETURN = 0x207ff000


def capture(image, entry, stop, app_payload=False):
    cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    cpu.mem_map(0, 0x200000)
    cpu.mem_write(0, image)
    shared = (ctypes.c_ubyte * 0x800000)()
    for base in (0x10000000, 0x18000000, 0x20000000):
        cpu.mem_map_ptr(base, 0x800000, UC_PROT_ALL, ctypes.addressof(shared))
    cpu.mem_map(0xe0000000, 0x10000)
    cpu.mem_map(0xc0000000, 0x1000000)
    if app_payload:
        # Layout-2 bootloader's payload copy. Only executable code/data matter
        # here; stop before allocation, which needs the full stock RAM state.
        cpu.mem_write(0x20020000, image[0x10020:])
    writes, reached = [], []

    def write(cpu, access, address, size, value, user):
        writes.append({'pc': hex(cpu.reg_read(UC_ARM_REG_PC)),
                       'address': hex(address), 'size': size, 'value': hex(value)})

    def finish(cpu, address, size, user):
        reached.append(address)
        cpu.emu_stop()

    cpu.hook_add(UC_HOOK_MEM_WRITE, write, begin=0xc0000000, end=0xc0ffffff)
    cpu.hook_add(UC_HOOK_CODE, finish, begin=stop, end=stop)
    cpu.reg_write(UC_ARM_REG_SP, RETURN - 0x1000)
    cpu.reg_write(UC_ARM_REG_LR, RETURN)
    cpu.emu_start(entry, 0xffffffff, count=100000)
    assert reached == [stop], f'failed to reach {stop:#x}: pc={cpu.reg_read(UC_ARM_REG_PC):#x}'
    return writes


def sequence(writes):
    return [(w['address'], w['size'], w['value']) for w in writes]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, default=Path(__file__).resolve().parent / 'build')
    parser.add_argument('--stock', type=Path, default=ROOT / 'dumps/mg2550s_w25q16cv_full.bin')
    parser.add_argument('--baseline', type=Path, help='optional old build: must lack the initializer')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    stock = args.stock.read_bytes()
    reference = capture(stock, 0xe5ba5, RETURN)
    assert len(reference) == 23, reference
    manifest = json.loads((args.build / 'manifest.json').read_text())
    image = (args.build / 'candidate.bin').read_bytes()
    entry = int(manifest['symbols']['application']['band_prepare'], 16)
    actual = capture(image, entry, 0xadc14, app_payload=True)
    assert sequence(actual) == sequence(reference), {'reference': reference, 'actual': actual}
    report = {'stock_sha256': hashlib.sha256(stock).hexdigest(),
              'package_sha256': manifest['package_sha256'],
              'stock_entry': '0xe5ba4', 'hybrid_entry': hex(entry),
              'stop_before': '0xadc14', 'stock': reference, 'hybrid': actual,
              'result': 'ordered MMIO writes match; no physical firing modeled'}
    if args.baseline:
        old_manifest = json.loads((args.baseline / 'manifest.json').read_text())
        old_entry = int(old_manifest['symbols']['application']['band_prepare'], 16)
        old = capture((args.baseline / 'candidate.bin').read_bytes(), old_entry, 0xadc14, app_payload=True)
        assert not old, old
        report['baseline'] = {'package_sha256': old_manifest['package_sha256'], 'writes': old}
        print('PASS negative control: old band_prepare emits none of the raster-feed defaults')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print('PASS: compiled C band preparation matches all 23 stock initializer writes, in order')


if __name__ == '__main__':
    main()
