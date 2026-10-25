#!/usr/bin/env python3
"""Execute protected install, boot readback and exact app restoration in emulation."""
import hashlib
import argparse
import json
from pathlib import Path
import sys
import struct

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'app/build/boot-audit'
sys.path.insert(0, str(ROOT / 'bootloader/tests'))
sys.path.insert(0, str(ROOT / 'app/tests'))
from model import setup
from model_printer import PrinterModel


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    global OUT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock-record',action='store_true')
    args=parser.parse_args()
    if args.stock_record: OUT=ROOT/'app/build/stock-record'
    audit = (OUT / 'application.bin').read_bytes()
    original = (OUT / 'original_application.bin').read_bytes()
    report = {'audit_sha256': sha(audit), 'restore_sha256': sha(original), 'cases': {}}
    am = json.loads((OUT / 'manifest.json').read_text())
    om = json.loads((ROOT / 'app/build/manifest.json').read_text())
    for name, directory, speed in [('baseline_high_speed', 'build', True),
                                   ('candidate_high_speed', 'build/vectors', True),
                                   ('candidate_full_speed', 'build/vectors', False)]:
        boot = ROOT / 'bootloader' / directory
        prefix = (boot / 'boot_region.bin').read_bytes()
        bm = json.loads((boot / 'manifest.json').read_text())
        md = {'layout': am['layout'], 'symbols': {
            'bootloader': bm['symbols']['bootloader'],
            'original': om['symbols']['application'], 'audit': am['symbols']['application']}}
        e = PrinterModel(image=(prefix + original).ljust(0x200000, b'\xff'), metadata=md, speed=speed)
        record=struct.pack('<8I',0x31504d53,0xceafb2ac,2,0xfffffffd,0xfe,0xffffff01,0x13576a,0x30)
        if args.stock_record: e.uc.mem_write(0xe00008d0,record)
        def app(x): return bool(x.info()) and x.info()[2] == 2
        def held(x): return bool(x.info()) and x.info()[2] == 1 and x.info()[3] & 1
        def upload(data):
            e.bulk = data; e.bulk_position = 0; e.bulk_started = False
            e.requests.appendleft(('begin', setup(0x40, 0x43, len(data) & 65535, len(data) >> 16)))
            old = len(e.responses)
            e.run(lambda x: len(x.responses) > old and bool(x.info()) and x.info()[5] == 0xc0,
                  resume=True, budget=150_000_000)
            assert bytes(e.uc.mem_read(0x10000, len(data))) == data
            assert bytes(e.uc.mem_read(0, 0x10000)) == prefix
        def reboot(mode, predicate):
            old = e.resets
            e.requests.appendleft(('reboot_audit', setup(0x40, 0x41, mode)))
            e.run(lambda x: x.resets > old and predicate(x), resume=True, budget=150_000_000)
        def read(offset, length, rejected=False, kind=0xc0, request=0x95):
            old = len(e.responses); stalls = len(e.stalls)
            e.requests.appendleft(('boot_read', setup(kind, request, offset & 65535, offset >> 16, length)))
            e.run(lambda x: any(n == 'boot_read' for _, n, _ in x.responses[old:]) or len(x.stalls) > stalls,
                  resume=True)
            assert (len(e.stalls) > stalls) == rejected, (hex(offset), length, e.stalls)
            return None if rejected else next(d for _, n, d in e.responses[old:] if n == 'boot_read')

        e.run(app, budget=150_000_000)
        reboot(1, held)
        upload(audit)
        reboot(2, app)
        before = len(e.programs)
        for iteration in range(2):
            dump = b''.join(read(offset, 256) for offset in range(0, 0x10000, 256))
            assert dump == prefix, name + ' boot dump differs'
        for offset, length in [(0, 0), (0, 1), (0, 257), (0, 65535), (2, 4),
                               (0xfffc, 8), (0x10000, 4), (0x20020000, 4), (0xfffffffc, 4)]:
            read(offset, length, rejected=True)
        read(0, 0, rejected=True, kind=0x40)
        assert read(0xfffc, 4) == prefix[-4:]
        assert len(e.programs) == before
        if args.stock_record:
            assert read(0,32,request=0x96)==record
            assert read(0,32,request=0x96)==record
            for offset,length in [(0,0),(0,4),(0,31),(0,33),(1,32),(0x10000,32)]:
                read(offset,length,rejected=True,request=0x96)
            read(0,0,rejected=True,kind=0x40,request=0x96)
            assert len(e.programs)==before
        reboot(1, held)
        upload(original)
        reboot(2, app)
        assert bytes(e.uc.mem_read(0, 0x10000)) == prefix
        assert bytes(e.uc.mem_read(0x10000, len(original))) == original
        assert e.info()[3] & 4
        report['cases'][name] = {'boot_sha256': sha(prefix), 'read_passes': 2,
            'bytes_per_pass': 65536, 'invalid_requests_rejected': 10,
            'boot_region_unchanged': True, 'original_app_restored': True,
            'erases': e.erases, 'program_count': len(e.programs), 'resets': e.resets}
        print('PASS', name, flush=True)
    report['all_assertions_passed'] = True
    if args.stock_record: report['record_tests']={'two_reads_match':True,'invalid_requests_rejected_per_case':7,'survived_install_and_reset':True}
    (OUT / 'validation.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
