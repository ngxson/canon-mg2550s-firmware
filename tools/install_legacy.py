#!/usr/bin/env python3
"""One-time migration from the tested OpenFW 0.1 updater to the split bootloader.

Normal application updates use fwctl.py. This command accepts only the tested
legacy identity, an explicitly pinned factory hash, and unchanged first-stage
recovery bytes. Without --send it only validates/prepares the transfer.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import time
from fwctl import validate_image

STAGE0_SHA = '1199c8e3d98ca8cc6c8e4176a296f3c223099c7203df18617127c2316b4f9e79'
LEGACY_ID = b'MFG:OpenFW;MDL:MG2550s;CLS:PRINTER;DES:MG2550s OpenFW;VER:0.1;STA:80;'


def sha(data): return hashlib.sha256(data).hexdigest()


def record(kind, body):
    raw = bytes([len(body)+1])+body
    return b'S'+kind+(raw+bytes([~sum(raw)&255])).hex().upper().encode()+b'\r\n'


def make_stream(image):
    def control(command, data=b''): return record(b'F', b'\0\0'+bytes([command])+data)
    chunks = [control(0, b'W176D'), control(9, b'\x01'), control(2, struct.pack('>II',0,len(image)-1))]
    chunks += [record(b'3',struct.pack('>I',address)+image[address:address+64]) for address in range(0,len(image),64)]
    return b''.join(chunks)+control(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--validation', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    image = args.image.read_bytes()
    if len(image)!=0x200000 or sha(image)!=args.sha256 or sha(image[:0x10000])!=STAGE0_SHA:
        raise ValueError('Wrong factory hash, size, or first-stage recovery sector')
    for offset, maintenance in ((0x10000,True),(0x20000,False)):
        length=struct.unpack_from('<I',image,offset+8)[0]+32
        validate_image(image[offset:offset+length],maintenance=maintenance,legacy=True)
    validation=json.loads(args.validation.read_text())
    if not validation.get('all_assertions_passed') or validation.get('factory_sha256')!=args.sha256:
        raise ValueError('Missing matching successful emulator validation')
    stream=make_stream(image)
    report={'factory_sha256':args.sha256,'stream_sha256':sha(stream),'stream_bytes':len(stream),
            'bytes_sent':0,'status_reads':[],'result':'prepared'}
    if not args.send:
        print(json.dumps(report,indent=2)); return
    if args.report.exists(): raise FileExistsError('Report exists; refusing an automatic repeat')
    args.report.parent.mkdir(parents=True,exist_ok=True)
    def save(): args.report.write_text(json.dumps(report,indent=2)+'\n')
    import usb.core
    import usb.util
    devices=list(usb.core.find(find_all=True,idVendor=0x04a9,idProduct=0x176d))
    if len(devices)!=1: raise RuntimeError('Expected exactly one legacy updater')
    dev=devices[0]
    def status():
        raw=bytes(dev.ctrl_transfer(0xa1,0,0,1,255,timeout=5000))
        if len(raw)<2 or int.from_bytes(raw[:2],'big')!=len(raw): raise RuntimeError('Malformed device ID')
        body=raw[2:]
        if b'STA:' not in body: raise RuntimeError('Missing status')
        value=body.split(b'STA:')[1][:2].decode()
        report['status_reads'].append(value); save(); return body,value
    try:
        body, code=status()
        if body!=LEGACY_ID or dev.bcdDevice!=0x100 or code!='80':
            raise RuntimeError('Not the supported legacy OpenFW 0.1 updater')
        report['result']='sending'; save()
        last_status=0
        for offset in range(0,len(stream),16384):
            part=stream[offset:offset+16384]; count=dev.write(1,part,timeout=60000)
            report['bytes_sent']+=count; save()
            if count!=len(part): raise RuntimeError('Partial USB write: do not retry blindly')
            if report['bytes_sent']-last_status>=65536 and report['bytes_sent']<len(stream):
                if status()[1]!='80': raise RuntimeError('Legacy updater rejected image')
                last_status=report['bytes_sent']
            if offset%524288==0: print(f"Sent {report['bytes_sent']}/{len(stream)} bytes",flush=True)
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            code=status()[1]
            if code=='C0':
                report['result']='C0'; save(); print('C0: factory image verified; device restarting.',flush=True); return
            if code!='80': raise RuntimeError('Flash failure: '+code)
            time.sleep(0.2)
        raise TimeoutError('No terminal flash status')
    except BaseException as error:
        report['result']='failed_or_unconfirmed'; report['error']=str(error); save(); raise
    finally:
        usb.util.dispose_resources(dev)


if __name__=='__main__': main()
