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
APP = ['src/usb.c', 'src/service.c', 'src/debug_log.c', 'src/scanner.c', 'src/scanner_motor.c', 'src/motor_control.c', 'src/scan_page.c', 'src/print.c', 'src/buttons.c', 'src/app_main.c']
LOAD_ADDRESS = 0x20020000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def package(payload, address):
    head = struct.pack('<7I', 0x5041474d, 2, len(payload), address, 0, zlib.crc32(payload), 0)
    return head + struct.pack('<I', zlib.crc32(head)) + payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build')
    args = parser.parse_args()
    output = args.output.resolve()
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
    for relative in REUSED + APP:
        source = (BOOT if relative in REUSED else ROOT) / relative
        inputs.add(source)
        obj = output / ('app_' + relative.replace('/', '_') + '.o')
        run('gcc', *FLAGS, *includes, '-DAPPLICATION_BUILD=1', '-c', source, '-o', obj)
        objects.append(obj)

    elf = output / 'application.elf'
    run('gcc', '-mcpu=arm946e-s', '-marm', '-nostdlib', '-Wl,--build-id=none',
        '-Wl,-T,' + str(ROOT / 'app.ld'), '-Wl,-Map,' + str(output / 'application.map'),
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

    app = package(raw.read_bytes(), LOAD_ADDRESS)
    assert len(app) <= 0x1f0000, 'application exceeds the main-app slot'
    (output / 'application.bin').write_bytes(app)

    inputs.update(ROOT.glob('include/*.h'))
    inputs.update(ROOT.glob('src/*.h'))
    inputs.update(BOOT.glob('include/*.h'))
    manifest = {
        'compiler': run('gcc', '--version').decode().splitlines()[0],
        'layout': {'version': 2, 'application': [0x10000, 0x200000], 'load_address': LOAD_ADDRESS},
        'artifacts': {'application.bin': {'bytes': len(app), 'sha256': sha(app)}},
        'symbols': {'application': symbols},
        'source_sha256': {str(p): sha(p.read_bytes()) for p in sorted(inputs)},
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest['artifacts'], indent=2))


if __name__ == '__main__':
    main()
