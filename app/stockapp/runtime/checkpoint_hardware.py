#!/usr/bin/env python3
"""Protected checkpoint experiment, followed by the previously tested reader.

The reader is left running for inspection/next iteration. No boot update,
automatic retry or power cycle occurs here. All evidence is append-once.
"""
import argparse,datetime,hashlib,importlib.util,json,struct,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools'))
import fwctl
import usb.util
spec=importlib.util.spec_from_file_location('upload_helper',ROOT/'app/stockapp/smoke/hardware.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
def sha(x):return hashlib.sha256(x).hexdigest()
def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args()
    out=a.directory.resolve();m=json.loads((out/'manifest.json').read_text())
    v=json.loads((out/'checkpoint_validation.json').read_text())
    data=(out/'candidate.mgap').read_bytes()
    assert v['passed'] and sha(data)==m['package_sha256']==v['package_sha256']
    for name,digest in m['source_sha256'].items():assert sha((ROOT/name).read_bytes())==digest,name
    assert m['protected_prefix_sha256']=='f22df711f7afd8c3804c43e2d491b8029af59a49f6190c2cafe1097a702e4b92'
    assert json.loads((ROOT/'bootloader/evidence/vectors/stage0_physical.json').read_text())['passed']
    reader=(ROOT/'app/build/stock-record/application.bin').read_bytes()
    rv=json.loads((ROOT/'app/build/stock-record/validation.json').read_text())
    assert rv['all_assertions_passed'] and sha(reader)==rv['audit_sha256']=='dcabb4a78c4f945ff231be00a4c58af33f33f67da867cc7f5f9b8af609f112d3'
    for b in (data,reader):fwctl.validate_image(b)
    dest=ROOT/'emu/evidence/stockapp/bringup'/out.name;dest.mkdir(parents=True,exist_ok=True)
    report_path=dest/'physical.json'
    assert not report_path.exists(),'Refusing to overwrite evidence'
    report={'package_sha256':sha(data),'checkpoint':m['checkpoint'],'result':'started','events':[]}
    def event(name,**details):
        row={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'event':name,**details}
        report['events'].append(row);report_path.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(row),flush=True)
    dev=None
    try:
        dev,status=fwctl.wait_device()
        assert not status['busy'] and not status['reboot_blocked']
        assert (status['mode']=='bootloader' and status['held']) or (dev.product in ('MG2550s App','MG2550s Stock Record') and status['app_valid'])
        event('starting_app',product=dev.product,info=status)
        with fwctl.Bridge('/dev/cu.usbmodem1201') as bridge:event('fresh_wiring',result=bridge.detect())
        if status['mode']=='application':
            fwctl.request_reboot(dev,1);dev=None
            dev,_=fwctl.wait_device('bootloader')
        h.upload(dev,data,event,timeout=240)
        event('starting_checkpoint');fwctl.request_reboot(dev,2);dev=None;time.sleep(.5)
        dev,status=fwctl.wait_device('bootloader',timeout=180)
        assert status['held'] and status['app_valid'] and not status['busy'] and status['status']=='80',status
        event('returned_to_held_rescue',info=status)
        h.upload(dev,reader,event)
        fwctl.request_reboot(dev,2);dev=None
        dev,status=fwctl.wait_device('application',timeout=45)
        assert dev.product=='MG2550s Stock Record' and status['app_valid']
        reads=[bytes(dev.ctrl_transfer(0xc0,0x96,0,0,32,timeout=5000)) for _ in range(2)]
        assert reads[0]==reads[1] and len(reads[0])==32
        record=list(struct.unpack('<8I',reads[0]))
        reason=0xfc if m.get('irq_snapshot') else 0xfe
        reached=record[:6]==[0x31504d53,0xceafb2ac,2,0xfffffffd,reason,reason^0xffffffff]
        if m['checkpoint']:reached=reached and record[6]==int(m['checkpoint']['address'],16)+2
        else:
            report['usb_snapshot']={'phy':hex(record[7]&0x1ffff),'irq_count':m['irq_snapshot']}
            if m.get('irq_snapshot_record_version')==2:
                report['usb_snapshot'].update(dctl=hex((record[7]>>17)&0x7f),error=record[7]>>24,
                    state=list(record[6].to_bytes(4,'little')))
            else:report['usb_snapshot'].update(dctl=hex(record[7]>>17),interrupted_lr=hex(record[6]))
        report['record']=[hex(x) for x in record];report['checkpoint_reached']=reached
        event('record_read',record=report['record'],checkpoint_reached=reached,reader_left_running=True)
        report['result']='checkpoint_passed_reader_running' if reached else 'unexpected_return_reader_running'
    except BaseException as error:
        report['result']='error';report['error']=str(error);raise
    finally:
        if dev is not None:usb.util.dispose_resources(dev)
        report_path.write_text(json.dumps(report,indent=2)+'\n')
        (dest/'manifest.json').write_text(json.dumps(m,indent=2)+'\n')
        (dest/'validation.json').write_text(json.dumps(v,indent=2)+'\n')
        print(json.dumps({'result':report['result'],'report':str(report_path)}),flush=True)
if __name__=='__main__':main()
