#!/usr/bin/env python3
"""Build the frozen first stage, USB/I2C bootloader and cooperative demo app.

No firmware dump, extracted binary, network service or RE workspace is read.
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
# Deliberate, user-authorized vector-ABI candidate revision (not yet installed).
# Previous physical baseline: 2b68ecfc23016d708256318cff7d20338d2351d001b142743ac5b2d6f79b7995.
PINNED_STAGE0_SHA = 'b5383aa702982e413601684c9982257ab4aced095e83769799740aaa145c30e8'
FLAGS = ['-mcpu=arm946e-s', '-marm', '-Os', '-std=c11', '-ffreestanding',
         '-fno-builtin', '-fno-unwind-tables', '-fno-asynchronous-unwind-tables',
         '-fno-stack-protector', '-fno-delete-null-pointer-checks',
         '-ffunction-sections', '-fdata-sections', '-Wall', '-Wextra', '-Werror', '-g']


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
        if compiler:
            prefix = compiler[:-3]
        else:
            prefix = str(Path.home() / '.platformio/packages/toolchain-gccarmnoneeabi/bin/arm-none-eabi-')

    def run(tool, *arguments):
        return subprocess.check_output([prefix + tool, *map(str, arguments)], cwd=ROOT)

    inputs = {Path(__file__).resolve()}
    symbols = {}

    def link(name, sources, script, application=False):
        objects = []
        inputs.add(ROOT / script)
        for relative in sources:
            source = ROOT / relative
            inputs.add(source)
            defines = ['-DAPPLICATION_BUILD=1'] if application else []
            if name == 'stage0': defines += ['-DSTAGE0_BUILD=1']
            if source.name == 'flash.c':
                defines += ['-Dflash_erase=raw_flash_erase', '-Dflash_program=raw_flash_program']
            elif source.name == 'update.c':
                defines += ['-Dflash_erase=app_flash_erase', '-Dflash_program=app_flash_program']
            obj = output / (name + '_' + relative.replace('/', '_') + '.o')
            run('gcc', *FLAGS, '-Iinclude', '-Istage0', *defines, '-c', source, '-o', obj)
            objects.append(obj)
        elf = output / (name + '.elf')
        run('gcc', '-mcpu=arm946e-s', '-marm', '-nostdlib', '-Wl,--build-id=none',
            '-Wl,-T,' + script, '-Wl,-Map,' + str(output / (name + '.map')), *objects, '-o', elf)
        assert not run('nm', '-u', elf).strip()
        binary = output / (name + '.raw.bin')
        run('objcopy', '-O', 'binary', '--gap-fill', '0xff', elf, binary)
        (output / (name + '.dis')).write_bytes(run('objdump', '-d', elf))
        symbols[name] = {}
        for line in run('nm', '-n', elf).decode().splitlines():
            fields = line.split()
            if len(fields) == 3:
                symbols[name][fields[2]] = hex(int(fields[0], 16))
        return binary.read_bytes()

    # Keep first-stage bytes pinned independently of normal app development.
    stage0 = link('stage0', [
        'stage0/panel.c', 'src/flash.c', 'stage0/flash_guard.c', 'stage0/integrity.c',
        'src/boot.c', 'src/cpu.c', 'src/vectors.c', 'stage0/loader.c', 'stage0/i2c.c', 'stage0/memory.c',
    ], 'stage0/stage0.ld').ljust(0x4000, b'\xff')
    if len(stage0) != 0x4000 or sha(stage0) != PINNED_STAGE0_SHA:
        raise SystemExit('Pinned recovery stage changed: '+sha(stage0))
    common = ['src/panel.c', 'src/integrity.c', 'src/runtime.c', 'src/usb.c',
              'src/entry.c', 'src/service.c', 'src/i2c.c', 'src/mailbox.c', 'src/update.c']
    loader = package(link('bootloader', common + ['src/flash.c', 'src/flash_guard.c'],
                          'bootloader.ld'), 0x20010000)
    app = package(link('application', common, 'example/app.ld', application=True), 0x20020000)
    assert len(loader) <= 0xc000 and len(app) <= 0x1f0000
    boot_region = stage0 + loader.ljust(0xc000, b'\xff')
    factory = (boot_region + app).ljust(0x200000, b'\xff')
    artifacts = {'stage0.bin': stage0, 'bootloader.bin': loader,
                 'boot_region.bin': boot_region, 'application.bin': app, 'factory.bin': factory}
    for name, data in artifacts.items():
        (output / name).write_bytes(data)
    inputs.update(ROOT.glob('include/*.h'))
    inputs.add(ROOT / 'stage0/protocol.h')
    manifest = {
        'compiler': run('gcc', '--version').decode().splitlines()[0],
        'stock_bytes_used': 0,
        'vector_abi': {'version': 1, 'address': '0x40', 'state': '0xe0000880',
                       'status': 'candidate; hardware validation pending'},
        'layout': {'version': 2, 'stage0': [0, 0x4000],
                   'bootloader': [0x4000, 0x10000], 'application': [0x10000, 0x200000]},
        'artifacts': {name: {'bytes': len(data), 'sha256': sha(data)} for name, data in artifacts.items()},
        'symbols': symbols,
        'source_sha256': {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in sorted(inputs)},
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest['artifacts'], indent=2))


if __name__ == '__main__':
    main()
