#!/usr/bin/env python3
"""Build the open replacement application as a flashable MGAP package.

The bootloader is treated as read-only: a few leaf drivers are reused from
../bootloader/src at build time, but no boot-region binary is produced or
touched. Output is app/build/application.bin (upload with `fwctl flash`).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import zlib

ROOT = Path(__file__).resolve().parent
BOOT = ROOT.parent / 'bootloader'
FLAGS = ['-mcpu=arm946e-s', '-marm', '-Os', '-std=c11', '-ffreestanding',
         '-fno-builtin', '-fno-unwind-tables', '-fno-asynchronous-unwind-tables',
         '-fno-stack-protector', '-fno-delete-null-pointer-checks',
         '-ffunction-sections', '-fdata-sections', '-Wall', '-Wextra', '-Werror', '-g']

# Reused unchanged from the bootloader (leaf drivers / entry), plus app sources.
REUSED = ['src/entry.c', 'src/runtime.c', 'src/panel.c', 'src/integrity.c', 'src/i2c.c', 'src/mailbox.c']
APP = ['src/usb.c', 'src/service.c', 'src/debug_log.c', 'src/scanner.c', 'src/scanner_motor.c', 'src/motor_control.c', 'src/scan_page.c', 'src/print.c', 'src/cartridge.c', 'src/head_fire.c', 'src/tunnel.c', 'src/print_mech.c', 'src/paper_feed.c', 'src/carriage.c', 'src/page_job.c', 'src/buttons.c', 'src/app_main.c',
       'src/ippusb.c', 'src/escl.c', 'src/ipp.c', 'src/png.c', 'src/jpeg.c', 'src/divide.c', 'src/stock_shim.c', 'src/cpu_cache.c', 'src/power.c', 'src/urf.c', 'src/print_engine.c']
# The eSCL image provider: the scanner for the flashed app; a synthetic chart
# for the protocol tests (app/tests/validate_escl.py), built into build/dummy.
SOURCES = {'scanner': 'src/scan_source_scanner.c', 'dummy': 'src/scan_source_dummy.c'}
LOAD_ADDRESS = 0x20020000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def package(payload, address):
    head = struct.pack('<7I', 0x5041474d, 2, len(payload), address, 0, zlib.crc32(payload), 0)
    return head + struct.pack('<I', zlib.crc32(head)) + payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build')
    parser.add_argument('--load-address', type=lambda v: int(v, 0), default=LOAD_ADDRESS,
                        help='RAM link/load address (hybrid builds link inside the bootloader RAM copy)')
    parser.add_argument('--ram-end', type=lambda v: int(v, 0), default=None, help='upper RAM bound asserted by the linker')
    parser.add_argument('--variants', default='scanner,dummy')
    args = parser.parse_args()
    for variant in args.variants.split(','):
        out = args.output.resolve() / ('dummy' if variant == 'dummy' else '')
        build(out, variant, args.load_address, args.ram_end)


def build(output, variant, load_address=LOAD_ADDRESS, ram_end=None):
    output.mkdir(parents=True, exist_ok=True)

    prefix = os.environ.get('ARM_PREFIX')
    if not prefix:
        compiler = shutil.which('arm-none-eabi-gcc')
        prefix = compiler[:-3] if compiler else str(
            Path.home() / '.platformio/packages/toolchain-gccarmnoneeabi/bin/arm-none-eabi-')

    def run(tool, *arguments):
        return subprocess.check_output([prefix + tool, *map(str, arguments)], cwd=ROOT)

    includes = ['-Iinclude', '-I' + str(BOOT / 'include')]
    objects, inputs = [], {Path(__file__).resolve(), ROOT / 'app.ld'}
    for relative in REUSED + APP + [SOURCES[variant]]:
        source = (BOOT if relative in REUSED else ROOT) / relative
        inputs.add(source)
        obj = output / ('app_' + relative.replace('/', '_') + '.o')
        run('gcc', *FLAGS, *includes, '-DAPPLICATION_BUILD=1', '-c', source, '-o', obj)
        objects.append(obj)

    elf = output / 'application.elf'
    run('gcc', '-mcpu=arm946e-s', '-marm', '-nostdlib', '-Wl,--build-id=none', '-Wl,--gc-sections',
        '-Wl,-T,' + str(ROOT / 'app.ld'), '-Wl,-Map,' + str(output / 'application.map'),
        f'-Wl,--defsym,APP_LOAD={load_address:#x}',
        f'-Wl,--defsym,APP_RAM_END={(ram_end or 0x20420000):#x}',
        *objects, '-o', elf)
    assert not run('nm', '-u', elf).strip(), 'application references undefined symbols'
    raw = output / 'application.raw.bin'
    run('objcopy', '-O', 'binary', '--gap-fill', '0xff', elf, raw)
    (output / 'application.dis').write_bytes(run('objdump', '-d', elf))

    symbols = {}
    for line in run('nm', '-n', elf).decode().splitlines():
        fields = line.split()
        if len(fields) == 3:
            symbols[fields[2]] = hex(int(fields[0], 16))

    app = package(raw.read_bytes(), load_address)
    assert len(app) <= 0x1f0000, 'application exceeds the main-app slot'
    (output / 'application.bin').write_bytes(app)

    inputs.update(ROOT.glob('include/*.h'))
    inputs.update(ROOT.glob('src/*.h'))
    inputs.update(BOOT.glob('include/*.h'))
    manifest = {
        'scan_source': variant,
        'compiler': run('gcc', '--version').decode().splitlines()[0],
        'layout': {'version': 2, 'application': [0x10000, 0x200000], 'load_address': load_address},
        'artifacts': {'application.bin': {'bytes': len(app), 'sha256': sha(app)}},
        'symbols': {'application': symbols},
        'source_sha256': {str(p): sha(p.read_bytes()) for p in sorted(inputs)},
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(variant, json.dumps(manifest['artifacts']))


if __name__ == '__main__':
    main()
