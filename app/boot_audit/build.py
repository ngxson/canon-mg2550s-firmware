#!/usr/bin/env python3
"""Build a temporary current-app variant with bounded, read-only boot readback.

Production app sources/build outputs and bootloader artifacts are not modified.
The extra request is control-IN 0x95: offset=value|(index<<16), 4..256 bytes,
word-aligned, entirely inside flash [0, 0x10000). There is no new write path.
"""
import hashlib
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
APP = HERE.parent
BOOT = APP.parent / 'bootloader'
OUT = APP / 'build/boot-audit'
spec = importlib.util.spec_from_file_location('app_builder', APP / 'build.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    global OUT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock-record',action='store_true',help='Also read the fixed stock-smoke SRAM record; isolated output')
    args=parser.parse_args()
    if args.stock_record: OUT=APP/'build/stock-record'
    OUT.mkdir(parents=True, exist_ok=True)
    original = (APP / 'build/application.bin').read_bytes()
    manifest = json.loads((APP / 'build/manifest.json').read_text())
    assert sha(original) == manifest['artifacts']['application.bin']['sha256']
    for name, digest in manifest['source_sha256'].items():
        assert sha(Path(name).read_bytes()) == digest, 'Original source changed: ' + name
    saved = OUT / 'original_application.bin'
    if saved.exists():
        assert saved.read_bytes() == original, 'Do not overwrite a different restore image'
    else:
        saved.write_bytes(original)

    usb = (APP / 'src/usb.c').read_text()
    anchor = '    } else if(type==0xc0 && request==REQUEST_INFO && !value && !index) {'
    assert usb.count(anchor) == 1
    branch = '''    } else if(type==0xc0 && request==0x95) {
        uint32_t offset=(uint32_t)value|((uint32_t)index<<16);
        unsigned n=length;
        if(!n || n>sizeof(control_buffer) || (n&3u) || (offset&3u) ||
           offset>=0x10000u || n>0x10000u-offset) { stall(); }
        else {
            for(unsigned i=0;i<n;i+=4) put32(control_buffer+i,rd(offset+i));
            reply(n,length);
        }
'''
    usb = usb.replace(anchor, branch + anchor)
    if args.stock_record:
        branch='''    } else if(type==0xc0 && request==0x96) {
        if(value || index || length!=32) { stall(); }
        else {
            for(unsigned i=0;i<32;i+=4) put32(control_buffer+i,rd(0xe00008d0u+i));
            reply(32,length);
        }
'''
        usb=usb.replace(anchor,branch+anchor)
    assert usb.count('"MG2550s App"') == 1
    usb = usb.replace('"MG2550s App"', '"MG2550s Stock Record"' if args.stock_record else '"MG2550s Boot Audit"')
    generated = OUT / 'usb_audit.c'
    generated.write_text(usb)

    compiler = shutil.which('arm-none-eabi-gcc')
    prefix = os.environ.get('ARM_PREFIX') or (compiler[:-3] if compiler else str(
        Path.home() / '.platformio/packages/toolchain-gccarmnoneeabi/bin/arm-none-eabi-'))

    def run(tool, *args):
        return subprocess.check_output([prefix + tool, *map(str, args)], cwd=APP)

    inputs = {Path(__file__).resolve(), APP / 'build.py', APP / 'app.ld'}
    objects = []
    for relative in builder.REUSED + builder.APP + [builder.SOURCES['scanner']]:
        source = (BOOT if relative in builder.REUSED else APP) / relative
        inputs.add(source)
        if relative == 'src/usb.c':
            source = generated
        obj = OUT / ('audit_' + relative.replace('/', '_') + '.o')
        run('gcc', *builder.FLAGS, '-I' + str(APP / 'include'), '-I' + str(BOOT / 'include'),
            '-DAPPLICATION_BUILD=1', '-c', source, '-o', obj)
        objects.append(obj)
    elf = OUT / 'application.elf'
    run('gcc', '-mcpu=arm946e-s', '-marm', '-nostdlib', '-Wl,--build-id=none',
        '-Wl,-T,' + str(APP / 'app.ld'), '-Wl,-Map,' + str(OUT / 'application.map'),
        *objects, '-o', elf)
    assert not run('nm', '-u', elf).strip()
    raw = OUT / 'application.raw.bin'
    run('objcopy', '-O', 'binary', '--gap-fill', '0xff', elf, raw)
    (OUT / 'application.dis').write_bytes(run('objdump', '-d', elf))
    data = builder.package(raw.read_bytes(), builder.LOAD_ADDRESS)
    assert len(data) <= 0x1f0000
    (OUT / 'application.bin').write_bytes(data)
    symbols = {}
    for line in run('nm', '-n', elf).decode().splitlines():
        fields = line.split()
        if len(fields) == 3:
            symbols[fields[2]] = hex(int(fields[0], 16))
    inputs.update(APP.glob('include/*.h'))
    inputs.update(APP.glob('src/*.h'))
    inputs.update(BOOT.glob('include/*.h'))
    result = {'purpose': 'Temporary boot-region reader; restore original app after use',
              'stock_record_reader':args.stock_record,
              'layout': {'version': 2, 'application': [0x10000, 0x200000]},
              'artifacts': {'application.bin': {'bytes': len(data), 'sha256': sha(data)}},
              'original_application_sha256': sha(original),
              'generated_usb_sha256': sha(generated.read_bytes()),
              'symbols': {'application': symbols},
              'source_sha256': {str(p): sha(p.read_bytes()) for p in sorted(inputs)}}
    (OUT / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result['artifacts'], indent=2))


if __name__ == '__main__':
    main()
