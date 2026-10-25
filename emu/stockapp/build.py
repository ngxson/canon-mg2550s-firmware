#!/usr/bin/env python3
"""Build an OFFLINE stock-as-app dependency experiment, never a flash command.

The production boot region is read-only input. The candidate intentionally lacks
a hardware exception-vector mechanism: probe.py tests that dependency explicitly.
No bootloader files or ordinary application artifacts are changed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'emu'))
from stock.inputs import DEFAULT_FIRMWARE, load_firmware

STAGE0_SHA = '2b68ecfc23016d708256318cff7d20338d2351d001b142743ac5b2d6f79b7995'
APP = 0x10000
RAM = 0x20020000
SHIM_FLASH = 0x10110
DELAY_COPY = 0x10d00


def check(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def branch(source, target):
    displacement = target - source - 8
    check(displacement % 4 == 0 and -(1 << 25) <= displacement < (1 << 25),
          'ARM branch out of range/alignment')
    return struct.pack('<I', 0xea000000 | ((displacement >> 2) & 0xffffff))

def thumb_bl(source,target):
    displacement=(target&~1)-source-4
    check(displacement%2==0 and -(1<<22)<=displacement<(1<<22),'Thumb BL out of range')
    return struct.pack('<HH',0xf000|((displacement>>12)&0x7ff),0xf800|((displacement>>1)&0x7ff))


def build(output, firmware=DEFAULT_FIRMWARE, boot_directory=None, register_vectors=False,
          startup_smoke=False, runtime=False, native_cpu=False, stop_at=None, native_startup=False, irq_snapshot=0, stay_awake=False, auto_power=False, telemetry=False):
    check(not startup_smoke or register_vectors,'startup smoke requires registered vectors')
    check(not runtime or (register_vectors and not startup_smoke),'runtime requires registered vectors, without startup stop')
    check(not native_cpu or runtime,'native CPU setup requires guarded runtime')
    check(not native_startup or (runtime and not native_cpu),'native startup requires guarded runtime and includes CPU setup')
    check(stop_at is None or runtime,'checkpoint requires guarded runtime')
    check(not irq_snapshot or (runtime and stop_at is None and 1<=irq_snapshot<=100000),'invalid IRQ snapshot')
    check(not stay_awake or runtime,'stay-awake requires guarded runtime')
    check(not auto_power or runtime,'auto-power requires guarded runtime')
    check(not telemetry or (runtime and native_startup),'telemetry requires native guarded runtime')
    _, stock = load_firmware(firmware)
    boot_path = (boot_directory or ROOT/'bootloader/build')/'boot_region.bin'
    boot = boot_path.read_bytes()
    boot_manifest = json.loads((boot_path.parent / 'manifest.json').read_text())
    check(len(boot) == APP, 'boot region must be exactly 64 KiB')
    if register_vectors:
        check(struct.unpack_from('<4I',boot,0x40)==(0x3158564d,1,24,0xe0000880),
              'registered candidate requires boot vector ABI v1')
        check(sha(boot[:0x4000]) == boot_manifest['artifacts']['stage0.bin']['sha256'],
              'first stage differs from supplied build manifest')
    else:
        check(sha(boot[:0x4000]) == STAGE0_SHA, 'requires production frozen stage 0')
    check(sha(boot) == boot_manifest['artifacts']['boot_region.bin']['sha256'],
          'boot region does not match its manifest')
    output.mkdir(parents=True, exist_ok=True)
    prefix = os.environ.get('ARM_PREFIX')
    if not prefix:
        gcc = shutil.which('arm-none-eabi-gcc')
        prefix = gcc[:-3] if gcc else str(Path.home() /
            '.platformio/packages/toolchain-gccarmnoneeabi/bin/arm-none-eabi-')
    shim_ram = RAM + SHIM_FLASH - APP - 32
    source_files=[HERE/'build.py']
    if register_vectors:
        app=ROOT/'app/stockapp'/('smoke' if startup_smoke or runtime else '')
        control=app/('control.c' if startup_smoke or runtime else 'register.c')
        source_files += [app/'entry.S',control,app/'shim.ld',ROOT/'bootloader/include/vectors.h']
        extra=[]
        if runtime:
            additions=[ROOT/'app/stockapp/runtime/hook.S',ROOT/'app/stockapp/runtime/management.c']
            source_files+=additions
            extra=['-DSTOCK_RUNTIME=1',*map(str,additions)]
            if telemetry:
                additions=[ROOT/'app/stockapp/runtime/telemetry.c',ROOT/'app/stockapp/runtime/telemetry_hook.S']
                source_files+=additions
                extra+=list(map(str,additions))
            if irq_snapshot:extra+=['-DSTOCK_IRQ_SNAPSHOT='+str(irq_snapshot)]
            if auto_power:
                native=ROOT/'app/stockapp/runtime/auto_power.c'
                source_files.append(native)
                extra+=['-DSTOCK_AUTO_POWER=1',str(native)]
            if native_cpu:
                native=ROOT/'app/stockapp/runtime/native_cpu.c'
                source_files.append(native)
                extra+=['-DSTOCK_NATIVE_CPU=1',str(native)]
            if native_startup:
                native=ROOT/'app/stockapp/runtime/native_startup.c'
                source_files.append(native)
                extra+=['-DSTOCK_NATIVE_STARTUP=1',str(native)]
        subprocess.run([prefix+'gcc','-mcpu=arm946e-s','-marm','-nostdlib',
            '-ffreestanding','-fno-builtin','-Os','-Wall','-Wextra','-Werror',
            '-Wl,--build-id=none','-Wl,-T,'+str(app/'shim.ld'),
            '-I'+str(ROOT/'bootloader/include'),str(app/'entry.S'),str(control),
            *extra,
            '-o',str(output/'shim.elf')],check=True)
    else:
        source_files += [HERE/'shim.S']
        subprocess.run([prefix+'gcc', '-mcpu=arm946e-s', '-marm', '-nostdlib',
            '-Wl,--build-id=none', f'-Wl,-Ttext={shim_ram:#x}', '-Wl,-e,shim_entry',
            str(HERE/'shim.S'), '-o', str(output/'shim.elf')], check=True)
    subprocess.run([prefix+'objcopy', '-O', 'binary', str(output/'shim.elf'),
                    str(output/'shim.bin')], check=True)
    shim = (output/'shim.bin').read_bytes()
    shim_symbols={}
    for line in subprocess.check_output([prefix+'nm','-n',str(output/'shim.elf')]).decode().splitlines():
        fields=line.split()
        if len(fields)==3:shim_symbols[fields[2]]=hex(int(fields[0],16))
    check(len(shim) < DELAY_COPY-SHIM_FLASH, 'shim overlaps relocated helpers')
    check(stock[SHIM_FLASH:0x11000] == b'\xff'*(0x11000-SHIM_FLASH),
          'shim/copy area must be unused in pinned stock')
    image = bytearray(stock)
    image[:APP] = boot
    image[APP+32:APP+36] = branch(APP+32, SHIM_FLASH)
    image[SHIM_FLASH:SHIM_FLASH+len(shim)] = shim
    # Preserve all three straight-line NOP-delay helpers overlapped by header
    # or entry branch. Their only instructions are NOP and BX LR; relocation
    # does not change any PC-relative operands.
    image[DELAY_COPY:DELAY_COPY+0x94] = stock[APP:APP+0x94]
    patches = []
    runtime_patches=[]
    if runtime:
        def replace(address,original,replacement,purpose):
            original=bytes.fromhex(original)
            check(stock[address:address+len(original)]==original,'runtime patch source mismatch at '+hex(address))
            check(len(replacement)==len(original),'runtime patch size mismatch')
            image[address:address+len(original)]=replacement
            runtime_patches.append(dict(address=hex(address),original=original.hex(),replacement=replacement.hex(),purpose=purpose))
        replace(0x126b2,'1400069a',thumb_bl(0x126b2,int(shim_symbols['stockapp_event_hook'],16)),
                'Stock USB request 0x30007 enters held rescue before queuing or EEPROM latch writes')
        for address in (0x154146,0x154162,0x15417e):
            replace(address,'10b53820',bytes.fromhex('07dffee7'),
                    'Intercept reset before timer shutdown and calls into replaced boot code')
        replace(0x11004,'190000eb',struct.pack('<I',0xef000008),
                'Direct native app restart entry also enters our held rescue')
        if native_startup:
            replace(0x11020,'7dcc03fa',struct.pack('<I',0xe1a00000),
                    'Our MGAP and bootloader checks replace the incompatible native first-64-KiB checksum')
        if stay_awake:
            replace(0x113f16,'10b50400',bytes.fromhex('01207047'),
                    'Observation mode reports a wake request at the standby eligibility check, using the native abort-standby branch before clocks and IRQs change')
        if auto_power:
            replace(0x12c7e2,'15490020896a',thumb_bl(0x12c7e2,int(shim_symbols['stockapp_power_key'],16))+bytes.fromhex('0020'),
                    'Read real panel keys with one startup Power pulse, then replay displaced r0=0; stock handles debounce and power-on')
        if telemetry:
            replace(0x51bf6,'fff7d1fe',thumb_bl(0x51bf6,int(shim_symbols['stockapp_telemetry_hook'],16)),
                    'Recognize read-only telemetry in vendor SETUP dispatch; other requests retain native lookup and all transfers use stock EP0')
    if startup_smoke:
        check(stock[0x135768:0x13576c]==bytes.fromhex('0c4910b5'),
              'panel startup stop-site differs from pinned stock')
        # Thumb SVC 0xfe enters our privileged callback; the following branch
        # cannot fall through if a future callback incorrectly returns.
        image[0x135768:0x13576c]=bytes.fromhex('fedffee7')
    checkpoint=None
    if stop_at is not None:
        # Reviewed Thumb instruction boundaries; no arbitrary patch addresses.
        sites={0x135768:'0c4910b5',0x49a60:'f8b51d4d',0x49ad2:'4068f8bd',
               0x11bae0:'70bd70b5'}
        check(stop_at in sites,'unreviewed checkpoint')
        expected=bytes.fromhex(sites[stop_at])
        check(bytes(image[stop_at:stop_at+4])==expected,'checkpoint source mismatch')
        image[stop_at:stop_at+4]=bytes.fromhex('fedffee7')
        checkpoint={'address':hex(stop_at),'original':expected.hex(),'replacement':'fedffee7'}
    # Exact Thumb->ARM veneers, not a blind disassembly of literal pools.
    for pc in range(0x11000, len(stock)-4, 4):
        word = struct.unpack_from('<I', stock, pc)[0]
        if word >> 24 != 0xea or stock[pc-4:pc] != bytes.fromhex('7847c046'):
            continue
        imm = word & 0xffffff
        if imm & 0x800000:
            imm -= 1 << 24
        target = pc + 8 + (imm << 2)
        if APP <= target < APP+0x94:
            replacement = DELAY_COPY + target-APP
            image[pc:pc+4] = branch(pc, replacement)
            patches.append(dict(pc=hex(pc), original_target=hex(target),
                                target=hex(replacement)))
    check(any(p['pc'] == '0x1509c8' for p in patches), 'short-delay veneer missing')
    payload = bytes(image[APP+32:])
    fields = struct.pack('<7I', 0x5041474d, 2, len(payload), RAM, 0,
                         zlib.crc32(payload), 0)
    image[APP:APP+32] = fields + struct.pack('<I', zlib.crc32(fields))
    check(bytes(image[:APP]) == boot, 'protected boot region changed')
    check(len(image) == 0x200000, 'candidate size changed')
    (output/'candidate.bin').write_bytes(image)
    (output/'candidate.mgap').write_bytes(image[APP:])
    manifest = dict(status=('Running stock experiment with guarded restart/update and stock USB management path'
                            if runtime else 'OFFLINE startup smoke: returns before panel/USB startup; physical recovery prerequisite'
                            if startup_smoke else 'Experimental registered vectors; management/restart integration incomplete'
                            if register_vectors else 'EMULATOR ONLY: no hardware exception-vector mechanism'),
        startup_smoke=startup_smoke,
        runtime=runtime, runtime_patches=runtime_patches,
        native_cpu=native_cpu, native_startup=native_startup, checkpoint=checkpoint,irq_snapshot=irq_snapshot,stay_awake=stay_awake,
        irq_snapshot_record_version=2 if irq_snapshot else None,
        auto_power=auto_power, telemetry=telemetry,
        shim_symbols=shim_symbols,
        smoke_stop_patch=({'address':'0x135768','original':'0c4910b5','replacement':'fedffee7'}
                          if startup_smoke else None),
        registered_vectors=register_vectors,
        stock_sha256=sha(stock), protected_prefix_sha256=sha(boot),
        stage0_sha256=sha(boot[:0x4000]), candidate_sha256=sha(image),
        package_sha256=sha(image[APP:]), payload_length=len(payload),
        payload_ram_end=hex(RAM+len(payload)), shim_flash=hex(SHIM_FLASH),
        shim_ram=hex(shim_ram), shim_bytes=len(shim), helper_veneer_patches=patches,
        stock_dependencies=['stock runtime, RTOS, handlers and device drivers',
                            'stock short delay helpers copied into app-slot padding'],
        source_sha256={str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in source_files})
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--boot-dir', type=Path, help='Read-only bootloader build directory')
    parser.add_argument('--register-vectors', action='store_true',help='Use the real boot vector ABI')
    parser.add_argument('--startup-smoke', action='store_true',help='Bound startup before panel/USB and redirect SVC 7/8 to held rescue')
    parser.add_argument('--runtime',action='store_true',help='Run beyond panel startup; guard native restart/update and add stock USB rescue request')
    parser.add_argument('--native-cpu',action='store_true',help='Restore native MPU/cache setup before stock C runtime')
    parser.add_argument('--native-startup',action='store_true',help='Run native app startup, including RAM preparation, with only its incompatible boot checksum bypassed')
    parser.add_argument('--irq-snapshot',type=int,default=0,help='Return a hardware USB-state snapshot after this many real IRQ entries')
    parser.add_argument('--stay-awake',action='store_true',help='Skip native clock-down and WFI idle transition for observation')
    parser.add_argument('--auto-power',action='store_true',help='One startup Power pulse through the stock panel handler')
    parser.add_argument('--telemetry',action='store_true',help='Bounded read-only USB snapshots of stock USB software state; no MMIO access')
    parser.add_argument('--stop-at',type=lambda v:int(v,0),help='Reviewed Thumb checkpoint returning to source rescue')
    parser.add_argument('--firmware', type=Path, default=DEFAULT_FIRMWARE)
    args = parser.parse_args()
    output=args.output or ROOT/('emu/output/stockapp/runtime' if args.runtime else 'emu/output/stockapp/smoke' if args.startup_smoke else
                               'emu/output/stockapp/registered' if args.register_vectors else 'emu/output/stockapp')
    build(output.resolve(), args.firmware, args.boot_dir, args.register_vectors,args.startup_smoke,args.runtime,args.native_cpu,args.stop_at,args.native_startup,args.irq_snapshot,args.stay_awake,args.auto_power,args.telemetry)
