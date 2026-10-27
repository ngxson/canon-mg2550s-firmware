#!/usr/bin/env python3
"""Build the hybrid (shape B) package: the open application inside a hole of
the stock image, stock routines left in place for calls.

Layout (see LAYOUT.md): the MGAP header at 0x10000 covers the whole remainder
of flash as its payload, so the layout-2 bootloader copies it unchanged to
0x20020000 and jumps there. Flash byte F lands in RAM at RAM_OFFSET + F. The
entry word at 0x10020 branches to the app, which is stored at HOLE and linked
at RAM_OFFSET + HOLE. The three stock delay helpers displaced by the header are
relocated exactly as emu/stockapp/build.py does, so stock code that calls
them keeps working. The bootloader is read-only input; the package is the app
region only.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import zlib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'emu'))
from stock.inputs import load_firmware  # noqa: E402

APP = 0x10000
RAM = 0x20020000
RAM_OFFSET = RAM - (APP + 32)          # flash F -> RAM RAM_OFFSET + F
DELAY_COPY = 0x10d00                   # erased gap in stock, as the stock package uses
HOLE = 0x199000                        # largest region untouched by boot + maintenance fixtures
HOLE_END = 0x1d8000
STOCK_END = 0x1f2000                   # last stock byte is below this; tail is erased
CANDIDATES = ROOT / 'emu/evidence/stockapp/maintenance/flash_free_candidates.json'
SNAPSHOT = ROOT / 'emu/output/fixture/stock_ram_snapshot'   # .json/.bin from emu/stockapp/ram_snapshot.py
SNAPSHOT_FLASH = HOLE + 0x21000        # app must end below this (132 KiB); table then blob (the blob ends ~3 KiB below HOLE_END)
SNAPSHOT_MAGIC = 0x50414e53            # 'SNAP'


def check(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def arm_branch(source, target):
    displacement = target - source - 8
    check(displacement % 4 == 0 and -(1 << 25) <= displacement < (1 << 25), 'ARM branch out of range')
    return struct.pack('<I', 0xea000000 | ((displacement >> 2) & 0xffffff))


# Stock kernel entry points replaced by veneers into the shim (brief 16 §3,
# brief 17 §1, fixture). Thumb functions; the veneer clobbers ip only.
VENEERS = {
    # Policy, not kernel: the paper loader's conditional pre-load maintenance
    # (0xc7126, result unused by its caller 0xdcd80) is ours to schedule; its
    # positioned head work needs the stock carriage task's work arming.
    0xc7126: 'shim_noop',
    # The loader's AdjustAngleSlit measurement (0xd7380 -> 0xd80f6, result
    # unused at 0xdcdd2): it arms LF event bits (0xc0c53400 = 0x20, 0xc0c52100 = 1)
    # that only stock's interrupt path consumes; skipped for test feeds.
    0xd7380: 'shim_noop',
    0x106d16: 'shim_event_wait', 0x106bf0: 'shim_event_set', 0x106cce: 'shim_event_clear',
    0x106720: 'shim_sem_wait', 0x106672: 'shim_sem_release',
    0x895c4: 'shim_mbx_send', 0x107170: 'shim_mbx_send_raw', 0x107286: 'shim_mbx_receive',
    0x89490: 'shim_pool_alloc', 0x107d42: 'shim_pool_alloc_wait', 0x107e28: 'shim_pool_free',
    0x89544: 'shim_free_wrapper', 0x1085be: 'shim_delay', 0x105dd2: 'shim_kquery',
    0x105da2: 'shim_task_id', 0x106e7e: 'shim_kernel_unknown', 0x10736e: 'shim_kernel_unknown',
    # status-change notification sender (builds a 0x14-byte record, dispatches
    # through a constructor-initialised singleton we do not create): logged no-op
    0x97f7c: 'shim_notify',
}
# Stock singletons whose first word (vtable pointer) stock constructors would
# set; the shim writes its own vtable there at init instead (constructor
# replacement, no flash patch): NVM managers 0x1878ff34 and 0x1878ff3c.


# ARM-mode kernel helpers replaced by 8-byte ARM veneers (`ldr pc, [pc, #-4]`
# + literal): the privilege raise/drop and cache-maintenance syscall bodies
# used around uncached allocations. With caches off and stock running in
# System mode they are no-ops; the drop (0x1500d8, `msr cpsr_c, #0x90`) would
# otherwise leave the CPU in user mode under the shim.
ARM_VENEERS = {0x150084: 'shim_noop', 0x150094: 'shim_noop', 0x1500a4: 'shim_noop',
               0x1500b4: 'shim_noop', 0x1500d8: 'shim_noop'}


def arm_veneer(target):
    check(target % 4 == 0, 'ARM veneer target alignment')
    return struct.pack('<II', 0xe51ff004, target)


def thumb_veneer(address, target):
    """push {r0}; ldr r0, [pc, #imm]; mov ip, r0; pop {r0}; bx ip; (nop); .word target"""
    check(address % 2 == 0 and target % 4 == 0, 'veneer alignment')
    literal = (address + 10 + 3) & ~3
    base = (address + 6) & ~3                 # PC of the ldr is address+2, aligned down
    imm = literal - base
    check(0 <= imm <= 1020 and imm % 4 == 0, 'veneer literal offset')
    code = struct.pack('<5H', 0xb401, 0x4800 | (imm >> 2), 0x4684, 0xbc01, 0x4760)
    code += b'\xc0\x46' * ((literal - address - 10) // 2)
    return code + struct.pack('<I', target)


def patch_veneers(image, stock, symbols):
    patches = []
    for address, symbol in sorted(VENEERS.items()):
        target = int(symbols[symbol], 16)
        code = thumb_veneer(address, target)
        # Every site is a whole Thumb function at least 14 bytes long; all but
        # the leaf 0x105da2 (ldr r2,[pc]; ldr r1,[pc] ...) begin with a push.
        check(stock[address + 1] in (0xb4, 0xb5) or address == 0x105da2, f'veneer site {address:#x} is not a Thumb push prologue')
        image[address:address + len(code)] = code
        patches.append(dict(address=hex(address), symbol=symbol, target=hex(target), bytes=len(code),
                            original=stock[address:address + len(code)].hex()))
    for address, symbol in sorted(ARM_VENEERS.items()):
        target = int(symbols[symbol], 16)
        code = arm_veneer(target)
        check(address % 4 == 0, 'ARM veneer site alignment')
        image[address:address + len(code)] = code
        patches.append(dict(address=hex(address), symbol=symbol, target=hex(target), bytes=len(code), mode='arm',
                            original=stock[address:address + len(code)].hex()))
    return patches


def relocate_delay_helpers(image, stock):
    """Copy the 0x94 bytes of straight-line helpers overlapped by the header
    to DELAY_COPY and retarget their Thumb->ARM veneers (same rule as the
    stock package builder)."""
    image[DELAY_COPY:DELAY_COPY + 0x94] = stock[APP:APP + 0x94]
    patches = []
    for pc in range(0x11000, len(stock) - 4, 4):
        word = struct.unpack_from('<I', stock, pc)[0]
        if word >> 24 != 0xea or stock[pc - 4:pc] != bytes.fromhex('7847c046'):
            continue
        imm = word & 0xffffff
        if imm & 0x800000:
            imm -= 1 << 24
        target = pc + 8 + (imm << 2)
        if APP <= target < APP + 0x94:
            replacement = DELAY_COPY + target - APP
            image[pc:pc + 4] = arm_branch(pc, replacement)
            patches.append(dict(pc=hex(pc), original_target=hex(target), target=hex(replacement)))
    check(any(p['pc'] == '0x1509c8' for p in patches), 'short-delay veneer missing')
    return patches


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, default=HERE / 'build')
    parser.add_argument('--boot-dir', type=Path, default=ROOT / 'bootloader/build', help='read-only bootloader build (for the emulator image only)')
    parser.add_argument('--firmware', type=Path, default=ROOT / 'dumps/mg2550s_w25q16cv_full.bin')
    parser.add_argument('--variant', default='scanner', choices=('scanner', 'dummy'))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    _, stock = load_firmware(args.firmware)
    boot = (args.boot_dir / 'boot_region.bin').read_bytes()
    check(len(boot) == APP, 'boot region must be exactly 64 KiB')

    # The hole must be inside a region the coverage runs never touched.
    candidates = json.loads(CANDIDATES.read_text())['untouched_by_boot_and_calls_sectors_4k']
    check(any(int(a, 16) <= HOLE and HOLE_END <= int(b, 16) for a, b in candidates),
          'hole is not inside an untouched candidate run; rerun the coverage fixtures')

    # Build the app linked at its RAM-copy address.
    app_out = output / 'app'
    subprocess.run([sys.executable, str(ROOT / 'app/build.py'), '--output', str(app_out),
                    '--load-address', hex(RAM_OFFSET + HOLE), '--ram-end', '0x20420000',
                    '--variants', args.variant], check=True, cwd=ROOT / 'app')
    app_dir = app_out / ('dummy' if args.variant == 'dummy' else '')
    raw = (app_dir / 'application.raw.bin').read_bytes()
    app_manifest = json.loads((app_dir / 'manifest.json').read_text())
    check(len(raw) <= HOLE_END - HOLE, f'application ({len(raw)} bytes) exceeds the hole')
    check(int(app_manifest['symbols']['application']['app_entry'], 16) == RAM_OFFSET + HOLE, 'app_entry is not at the hole base')

    image = bytearray(stock)
    image[:APP] = boot
    check(stock[0x10110:0x11000] == b'\xff' * (0x11000 - 0x10110), 'stock gap 0x10110..0x11000 must be erased')
    patches = relocate_delay_helpers(image, stock)
    veneers = patch_veneers(image, stock, app_manifest['symbols']['application'])
    image[HOLE:HOLE + len(raw)] = raw
    check(HOLE + len(raw) <= SNAPSHOT_FLASH, 'application overlaps the RAM snapshot slot')
    # Boot-state RAM snapshot (stock object graph): table of (address, flash offset, length) + blob.
    snapshot = None
    if SNAPSHOT.with_suffix('.json').exists():
        meta = json.loads(SNAPSHOT.with_suffix('.json').read_text()); blob = SNAPSHOT.with_suffix('.bin').read_bytes()
        check(hashlib.sha256(blob).hexdigest() == meta['blob_sha256'], 'snapshot blob does not match its index')
        check(meta['boot']['firmware_sha256'] == sha(stock), 'snapshot taken from a different stock image')
        entries = [e for e in meta['pages'] if not e.get('zero')]
        table = struct.pack('<4I', SNAPSHOT_MAGIC, len(entries), len(blob), 0)
        blob_flash = SNAPSHOT_FLASH + 16 + 12 * len(entries)
        blob_flash = (blob_flash + 15) & ~15
        for e in entries:
            table += struct.pack('<III', int(e['address'], 16), blob_flash + e['offset'], e['length'])
        check(blob_flash + len(blob) <= HOLE_END, f'snapshot ({len(blob)} bytes) exceeds the hole')
        image[SNAPSHOT_FLASH:SNAPSHOT_FLASH + len(table)] = table
        image[blob_flash:blob_flash + len(blob)] = blob
        snapshot = dict(flash=hex(SNAPSHOT_FLASH), blob_flash=hex(blob_flash), pages=len(entries), zero_pages=len(meta['pages']) - len(entries),
                        bytes=len(blob), blob_sha256=meta['blob_sha256'], boot=meta['boot'])
    # Entry word: ARM branch from RAM 0x20020000 to the app in its RAM copy.
    image[APP + 32:APP + 36] = arm_branch(RAM, RAM_OFFSET + HOLE)
    payload = bytes(image[APP + 32:])
    fields = struct.pack('<7I', 0x5041474d, 2, len(payload), RAM, 0, zlib.crc32(payload), 0)
    image[APP:APP + 32] = fields + struct.pack('<I', zlib.crc32(fields))
    check(bytes(image[:APP]) == boot, 'protected boot region changed')
    check(len(image) == 0x200000, 'image size changed')

    (output / 'candidate.bin').write_bytes(image)          # full flash image for the emulator
    (output / 'application.bin').write_bytes(image[APP:])  # the package fwctl flashes
    symbols = app_manifest['symbols']['application']
    manifest = dict(
        status='Hybrid shape B: open app hosts; stock image left in place for in-flash calls. Emulator-validated only until recorded otherwise.',
        layout={'version': 2, 'application': [APP, 0x200000], 'load_address': RAM,
                'hole': [hex(HOLE), hex(HOLE + len(raw))], 'hole_limit': hex(HOLE_END), 'snapshot_flash': hex(SNAPSHOT_FLASH), 'app_ram': hex(RAM_OFFSET + HOLE),
                'ram_offset': hex(RAM_OFFSET), 'delay_copy': hex(DELAY_COPY)},
        stock_sha256=sha(stock), boot_region_sha256=sha(boot), candidate_sha256=sha(bytes(image)),
        package_sha256=sha(bytes(image[APP:])), payload_length=len(payload), app_bytes=len(raw),
        app_variant=args.variant, app_source_sha256=app_manifest['source_sha256'],
        helper_veneer_patches=patches, kernel_veneers=veneers, ram_snapshot=snapshot,
        symbols={'application': symbols},
        stock_dependencies=['stock image retained in flash outside the hole; routines called in place on explicit USB request 0x95/0x97 only',
                            'stock kernel entry points replaced by veneers into src/stock_shim.c (see kernel_veneers)',
                            'stock short delay helpers copied into the erased gap at 0x10d00'],
    )
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({k: manifest[k] for k in ('layout', 'package_sha256', 'app_bytes', 'payload_length')}, indent=1))


if __name__ == '__main__':
    main()
