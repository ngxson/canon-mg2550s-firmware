#!/usr/bin/env python3
"""Protected stock-startup observation, bounded readout, exact app restoration.

Never invokes dangerous-write. Update failures stop without a reboot/retry.
Prerequisites include emulator reports and physical first-stage I2C recovery.
"""
import datetime
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
STOCK=ROOT/'emu/output/stockapp/smoke'
READER=ROOT/'app/build/stock-record'
DEST=ROOT/'emu/evidence/stockapp/physical_smoke'
sys.path.insert(0,str(ROOT/'tools'))
import fwctl
import usb.util

def sha(data): return hashlib.sha256(data).hexdigest()

def info(dev):
    class StatusDevice:
        def ctrl_transfer(self,*args,**kwargs):
            kwargs['timeout']=10000
            return dev.ctrl_transfer(*args,**kwargs)
    return fwctl.get_info(StatusDevice())

def preflight():
    smoke=(STOCK/'candidate.mgap').read_bytes()
    reader=(READER/'application.bin').read_bytes()
    original=(READER/'original_application.bin').read_bytes()
    sm=json.loads((STOCK/'manifest.json').read_text())
    sv=json.loads((STOCK/'validation.json').read_text())
    rm=json.loads((READER/'manifest.json').read_text())
    rv=json.loads((READER/'validation.json').read_text())
    uv=json.loads((STOCK/'upload_validation.json').read_text())
    recovery=json.loads((ROOT/'bootloader/evidence/vectors/stage0_physical.json').read_text())
    assert recovery['passed'] and recovery['stage0_result']=='C0'
    assert sm['startup_smoke'] and sm['registered_vectors']
    assert sv['all_assertions_passed'] and rv['all_assertions_passed'] and uv['passed']
    assert sha(smoke)==sm['package_sha256']==sv['package_sha256']==uv['package_sha256']
    assert sha(reader)==rm['artifacts']['application.bin']['sha256']==rv['audit_sha256']
    assert sha(original)==rv['restore_sha256']=='47f078c2a3cc75dac1d67dd02c0531e9c85e47f67b0cfe0edfa471d9e582b15a'
    for manifest in (sm,rm):
        for filename,digest in manifest['source_sha256'].items():
            assert sha((ROOT/filename).read_bytes())==digest,filename
    assert sha((ROOT/'bootloader/build/vectors/boot_region.bin').read_bytes())==sm['protected_prefix_sha256']
    for data in (smoke,reader,original): fwctl.validate_image(data)
    return smoke,reader,original

def upload(dev,data,event,timeout=120):
    status=info(dev)
    if status['mode']!='bootloader' or status['layout_version']!=2 or status['busy'] or status['reboot_blocked']:
        raise RuntimeError('Bootloader not ready for protected app update: '+str(status))
    dev.ctrl_transfer(0x40,0x40,0,0,b'',timeout=5000)
    event('protected_upload_begin',bytes=len(data),sha256=sha(data),request='0x43',flash_start='0x10000')
    dev.ctrl_transfer(0x40,0x43,len(data)&65535,len(data)>>16,b'',timeout=5000)
    for offset in range(0,len(data),16384):
        part=data[offset:offset+16384]
        if dev.write(1,part,timeout=60000)!=len(part):
            raise RuntimeError('Short USB write: no retry or reboot')
    event('payload_transferred',bytes=len(data))
    deadline=time.monotonic()+timeout
    previous=None
    while time.monotonic()<deadline:
        status=info(dev)
        if status!=previous: event('update_status',info=status);previous=status
        if status['status']=='C0' and not status['busy'] and not status['reboot_blocked'] and status['app_valid']:
            event('updater_verified_flash',sha256=sha(data));return
        if status['status']!='80': raise RuntimeError('Update failed; no reboot: '+str(status))
        time.sleep(.2)
    raise TimeoutError('No verified completion; no reboot')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume-readout',action='store_true',help='Continue only after a recorded startup wait timeout and observed held rescue')
    args=parser.parse_args()
    smoke,reader,original=preflight()
    DEST.mkdir(parents=True,exist_ok=True)
    report_path=DEST/('physical_resume.json' if args.resume_readout else 'physical.json')
    if report_path.exists(): raise SystemExit('Refusing to overwrite physical evidence')
    if args.resume_readout:
        prior=json.loads((DEST/'physical.json').read_text())
        assert prior['result']=='error' and prior['events'][-1]['event']=='starting_stock_smoke'
        assert any(row['event']=='updater_verified_flash' and row['sha256']==sha(smoke) for row in prior['events'])
    r={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'result':'started','events':[]}
    def event(name,**details):
        row={'event':name,**details};r['events'].append(row)
        report_path.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(row),flush=True)
    dev=None
    try:
        if not args.resume_readout:
            dev,status=fwctl.wait_device('application')
            assert dev.product=='MG2550s App' and status['app_valid'] and not status['busy']
            event('original_app_ready',info=status)
            with fwctl.Bridge('/dev/cu.usbmodem1201') as bridge:
                event('fresh_wiring_check',result=bridge.detect())
            fwctl.request_reboot(dev,1);dev=None
            dev,_=fwctl.wait_device('bootloader')
            upload(dev,smoke,event)
            event('starting_stock_smoke')
            fwctl.request_reboot(dev,2);dev=None
            # Reject the previous loader's held/C0 status as a false return.
            time.sleep(.5)
        # Full-slot app validation happens before USB attachment on each reset.
        # The first physical return exceeded the original 45-second allowance.
        dev,status=fwctl.wait_device('bootloader',timeout=180)
        if not status['held'] or not status['app_valid'] or status['status']!='80':
            raise RuntimeError('Stock did not return to newly initialized held rescue')
        event('stock_returned_to_held_rescue',info=status)
        upload(dev,reader,event)
        fwctl.request_reboot(dev,2);dev=None
        dev,status=fwctl.wait_device('application')
        assert dev.product=='MG2550s Stock Record' and status['app_valid']
        records=[bytes(dev.ctrl_transfer(0xc0,0x96,0,0,32,timeout=5000)) for _ in range(2)]
        r['records_identical']=records[0]==records[1]
        if len(records[0])==32:
            words=struct.unpack('<8I',records[0]);r['record']=[hex(v) for v in words]
            r['planned_stop_confirmed']=(r['records_identical'] and
                words[:6]==(0x31504d53,0xceafb2ac,2,0xfffffffd,0xfe,0xffffff01) and
                words[6]==0x13576a and bool(words[7]&0x20))
        else:r['planned_stop_confirmed']=False
        event('stock_record_read',record=r.get('record'),planned_stop_confirmed=r['planned_stop_confirmed'])
        boot=bytearray()
        for offset in range(0,0x10000,256):
            part=bytes(dev.ctrl_transfer(0xc0,0x95,offset,0,256,timeout=5000))
            if len(part)!=256: raise RuntimeError('Short boot-region read')
            boot.extend(part)
        (DEST/'boot_after.bin').write_bytes(boot)
        r['boot_region_unchanged']=boot==(ROOT/'bootloader/build/vectors/boot_region.bin').read_bytes()
        event('boot_region_readback',sha256=sha(boot),unchanged=r['boot_region_unchanged'])
        # Restore even if the observation reports an unexpected stock exit.
        fwctl.request_reboot(dev,1);dev=None
        dev,_=fwctl.wait_device('bootloader')
        upload(dev,original,event)
        fwctl.request_reboot(dev,2);dev=None
        dev,status=fwctl.wait_device('application')
        assert dev.product=='MG2550s App' and status['app_valid'] and not status['busy']
        event('original_app_restored',info=status,sha256=sha(original))
        r['result']='passed_original_restored' if r['planned_stop_confirmed'] and r['boot_region_unchanged'] else 'unexpected_observation_original_restored'
    except BaseException as error:
        r['result']='error';r['error']=str(error);raise
    finally:
        if dev is not None: usb.util.dispose_resources(dev)
        report_path.write_text(json.dumps(r,indent=2)+'\n')
        print(json.dumps({'result':r['result'],'report':str(report_path)}),flush=True)

if __name__=='__main__':main()
