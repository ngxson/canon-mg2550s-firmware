#!/usr/bin/env python3
"""Execute the explicit whole-flash writer, including rejection/failure paths."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib
from model import Model, ROOT, setup

sys.path.insert(0, str(ROOT.parent/'tools'))
from fwctl import dangerous_packet

STAY = ('stay', setup(0x40, 0x40))
def begin(data): return ('begin', setup(0x40, 0x44, len(data)&65535, len(data)>>16))
def status(e, code): return bool(e.info()) and e.info()[5] == code
def packet(image): return dangerous_packet(image, hashlib.sha256(image).hexdigest())
def reheader(data):
    data = bytearray(data)
    struct.pack_into('<I', data, 28, zlib.crc32(data[:28]))
    return bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock', type=Path, help='Optional restoration fixture; never a build input')
    args = parser.parse_args()
    image = (ROOT/'build/factory.bin').read_bytes()
    # Exercise every flash address, including non-FF bytes at the far end.
    target = image[:-4096] + bytes(range(256))*16
    data = packet(target)
    report = {'factory_sha256': hashlib.sha256(image).hexdigest(), 'cases': {}}
    def record(name, e):
        report['cases'][name] = e.summary(); print('PASS', name, flush=True)

    for speed in (True, False):
        e = Model(actions=[[STAY, begin(data)]], bulk=data, dangerous=True, speed=speed)
        e.run(lambda x: status(x, 0xc0), budget=800000000)
        assert bytes(e.uc.mem_read(0, 0x200000)) == target
        assert e.erases == list(range(0x10000, 0x200000, 0x10000)) + [0]
        assert len(e.programs) == 512 and e.programs[-1] == 0
        assert e.info()[3]&8 and not e.info()[3]&16
        e.requests.appendleft(('reboot_boot', setup(0x40, 0x41, 1)))
        e.run(lambda x: x.resets == 2 and bool(x.info()) and x.info()[2] == 1 and x.info()[3]&1, resume=True)
        record('whole_flash_then_usb_reboot_'+('high_speed' if speed else 'full_speed'), e)

    for length in (0, 32, 0x200000, 0x20001f, 0x200021):
        e = Model(actions=[[STAY, ('bad_length', setup(0x40,0x44,length&65535,length>>16))]])
        e.run(lambda x: 'bad_length' in x.stalls)
        assert not e.erases
        record('reject_raw_length_'+str(length), e)

    for offset, value, name in [(0,0,'magic'), (4,2,'version'), (8,0x10000,'image_size'),
                                 (12,0x10000,'address'), (16,0,'acknowledgement'), (24,1,'reserved')]:
        bad = bytearray(data); struct.pack_into('<I', bad, offset, value); bad = reheader(bad)
        e = Model(actions=[[STAY,begin(bad)]],bulk=bad)
        e.run(lambda x: status(x,0x90),budget=150000000)
        assert not e.erases and not e.programs
        record('reject_raw_'+name,e)

    for name, offset in [('header_crc',28),('payload_crc',len(data)-1)]:
        bad=bytearray(data); bad[offset]^=1
        e=Model(actions=[[STAY,begin(bad)]],bulk=bytes(bad))
        e.run(lambda x: status(x,0x90),budget=400000000)
        assert not e.erases
        record('reject_raw_'+name,e)

    e=Model(actions=[[STAY,begin(data)]],bulk=data[:64])
    e.run(lambda x: status(x,0x91)); assert not e.erases
    record('raw_partial_upload_times_out_without_erasing',e)

    e=Model(); e.run(lambda x: bool(x.info()) and x.info()[2]==2)
    e.requests.appendleft(('raw_in_app',begin(data)[1]))
    e.run(lambda x: 'raw_in_app' in x.stalls,resume=True)
    assert not e.erases and not e.info()[3]&8
    record('demo_cannot_accept_raw_write',e)

    e=Model(actions=[[STAY,begin(data)]],bulk=data,dangerous=True,corrupt=True)
    e.run(lambda x: status(x,0xa3),budget=400000000)
    assert e.info()[3]&16 and e.erases==[0x10000]
    e.requests.extend([('reboot_blocked',setup(0x40,0x41)),('class_reset',setup(0x21,2,0,1)),
                       ('normal_begin_blocked',setup(0x40,0x43,64))])
    e.run(lambda x: 'normal_begin_blocked' in x.stalls,resume=True)
    assert 'reboot_blocked' in e.stalls and e.resets==1
    assert bytes(e.uc.mem_read(0,0x10000))==image[:0x10000]
    record('failed_raw_write_blocks_reboot_and_normal_updates',e)
    e.corrupt=False; e.bulk_position=0; e.requests.append(begin(data))
    e.run(lambda x: status(x,0xc0),resume=True,budget=800000000)
    assert bytes(e.uc.mem_read(0,0x200000))==target and not e.info()[3]&16
    record('explicit_retry_from_ram_recovers_after_raw_failure',e)

    e=Model(actions=[[STAY,begin(data)]],bulk=data,dangerous=True,cut_after=1)
    e.run(lambda x: False,budget=400000000)
    assert bytes(e.uc.mem_read(0,0x10000))==image[:0x10000]
    record('early_interruption_leaves_original_boot_block',e)
    e=Model(actions=[[STAY,begin(data)]],bulk=data,dangerous=True,cut_after=511)
    e.run(lambda x: False,budget=500000000)
    assert bytes(e.uc.mem_read(0,0x1000))==b'\xff'*4096
    assert e.programs[-1]==0xf000
    record('interruption_during_boot_commit_requires_external_recovery',e)

    if args.stock:
        stock=args.stock.read_bytes(); wire=packet(stock)
        e=Model(actions=[[STAY,begin(wire)]],bulk=wire,dangerous=True)
        e.run(lambda x: status(x,0xc0),budget=800000000)
        assert bytes(e.uc.mem_read(0,0x200000))==stock
        record('stock_dump_restored_byte_exact_no_boot_claim',e)
        report['stock_sha256']=hashlib.sha256(stock).hexdigest()

    report['all_assertions_passed']=True
    (ROOT/'build/dangerous_validation.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__': main()
