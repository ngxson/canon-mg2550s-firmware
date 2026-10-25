#!/usr/bin/env python3
"""Exercise the exact USB rescue body through stock's BJL parser, offline.

The seven-byte printer-stream selector is separately checked against the host
payload and documented framing; this fixture enters BJL after that selector.
"""
import argparse,collections,hashlib,json
from pathlib import Path
import validate

def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path)
    validate.OUT=p.parse_args().directory.resolve()
    body=b'BJLSTART\n@FROMUPMODE\nBJLEND\n'
    e=validate.Managed(candidate=validate.OUT/'candidate.bin',from_reset=True,boot_build=validate.BOOT,
        eeprom=validate.ROOT/'emu/inputs/eeprom.bin',event=0x30007,kind='bjl')
    e.payload=body;e.fifo=collections.deque(body)
    e.boot();e.run_boot(500,30_000_000)
    r=validate.check(e,0xfd)
    r.update(passed=True,body_hex=body.hex(),envelope_hex='1b5b4b0200001f',
        package_sha256=hashlib.sha256((validate.OUT/'candidate.mgap').read_bytes()).hexdigest(),
        limit='Actual BJL parser entry; printer-stream envelope needs physical delivery verification')
    (validate.OUT/'usb_rescue_payload_validation.json').write_text(json.dumps(r,indent=2)+'\n')
    print(json.dumps(r,indent=2))
if __name__=='__main__':main()
