#!/usr/bin/env python3
"""Flash only the guarded runtime MGAP and identify running stock firmware.

Separate verify-rescue phase sends only the guarded BJL command, then restarts
the same app. No whole-chip writer or automatic update retry is available.
"""
import argparse,datetime,hashlib,importlib.util,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'emu/output/stockapp/runtime'
DEST=ROOT/'emu/evidence/stockapp/runtime'
STARTUP_PROOF=ROOT/'emu/evidence/stockapp/bringup/auto_power_state'
sys.path.insert(0,str(ROOT/'tools'))
import fwctl
import usb.core,usb.util
spec=importlib.util.spec_from_file_location('smoke_host',ROOT/'app/stockapp/smoke/hardware.py')
smoke_host=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke_host)
# ESC [ K, little-endian length 2, mode bytes 00 1f selects BJL on
# the real printer stream (Gutenprint print-canon.c: canon_control_cmd).
# The parser fixture enters BJL directly and therefore omits this envelope.
BJL=b'\x1b[K\x02\x00\x00\x1fBJLSTART\n@FROMUPMODE\nBJLEND\n'

def sha(data):return hashlib.sha256(data).hexdigest()

def preflight():
    m=json.loads((OUT/'manifest.json').read_text());data=(OUT/'candidate.mgap').read_bytes()
    assert m['runtime'] and not m['startup_smoke'] and sha(data)==m['package_sha256']
    if m.get('native_startup') and m.get('auto_power'):
        v=json.loads((OUT/'startup_validation.json').read_text())
        assert v['passed'] and v['candidate_sha256']==m['candidate_sha256'] and v['package_sha256']==sha(data)
        assert v['test_source_sha256']==sha((ROOT/'app/stockapp/runtime/startup_validation.py').read_bytes())
        assert v['native_usb_initializations']==1 and v['native_usb_attachments']==1
        assert v['boot_unchanged'] and v['no_flash_or_spi_writes']
        # Physical bounded run of this same power-on patch must have initialized
        # and attached USB without a native error before the unbounded run.
        physical=json.loads((STARTUP_PROOF/'physical.json').read_text())
        pm=json.loads((STARTUP_PROOF/'manifest.json').read_text())
        assert physical['package_sha256']==pm['package_sha256']
        assert pm['source_sha256']==m['source_sha256']
        assert pm.get('telemetry',False)==m.get('telemetry',False)
        assert all(pm[k]==m[k] for k in ('native_startup','auto_power','stay_awake','stock_sha256','protected_prefix_sha256'))
        assert physical['checkpoint_reached'] and physical['result']=='checkpoint_passed_reader_running'
        state=physical['usb_snapshot']
        assert state['error']==0 and state['state'][2:]==[1,1]
        assert int(state['phy'],16)&0x10000 and not int(state['dctl'],16)&2
    else:
        for name in ('reset-boot','reset-home'):
            v=json.loads((OUT/(name+'.json')).read_text())
            assert v['assertions_passed'] and v['audit']['execution_image_sha256']==m['candidate_sha256']
        assert json.loads((OUT/'differential.json').read_text())['passed']
    v=json.loads((OUT/'guard_validation.json').read_text())
    assert v['passed'] and v['package_sha256']==sha(data)
    if m.get('telemetry'):
        v=json.loads((OUT/'telemetry_validation.json').read_text())
        assert v['passed'] and v['package_sha256']==sha(data)
        assert v['test_source_sha256']==sha((ROOT/'app/stockapp/runtime/validate_telemetry.py').read_bytes())
        assert v['host_source_sha256']==sha((ROOT/'tools/stocktrace.py').read_bytes())
    assert json.loads((ROOT/'bootloader/evidence/vectors/stage0_physical.json').read_text())['passed']
    for name,digest in m['source_sha256'].items():assert sha((ROOT/name).read_bytes())==digest,name
    assert sha((ROOT/'emu/evidence/stockapp/physical_smoke/boot_after.bin').read_bytes())==m['protected_prefix_sha256']
    fwctl.validate_image(data);return data

def wait_stock(event,timeout=300):
    deadline=time.monotonic()+timeout;last_error=None;last_progress=time.monotonic()
    while time.monotonic()<deadline:
        devices=list(usb.core.find(find_all=True,idVendor=0x04a9))
        if len(devices)>1:raise RuntimeError('Ambiguous Canon USB devices')
        for dev in devices:
            try:
                cfg=dev.get_active_configuration()
                for interface in cfg:
                    if (interface.bInterfaceClass,interface.bInterfaceSubClass,interface.bInterfaceProtocol)!=(7,1,2):continue
                    raw=bytes(dev.ctrl_transfer(0xa1,0,0,interface.bInterfaceNumber,255,timeout=3000))
                    if len(raw)<2 or int.from_bytes(raw[:2],'big')!=len(raw):continue
                    body=raw[2:]
                    if body.startswith(b'MFG:Canon;') and b'VER:2.050;' in body:
                        endpoints=[ep.bEndpointAddress for ep in interface if ep.bmAttributes&3==2 and not ep.bEndpointAddress&0x80]
                        assert len(endpoints)==1
                        details={'pid':hex(dev.idProduct),'bcdDevice':hex(dev.bcdDevice),
                                 'interface':interface.bInterfaceNumber,'out_endpoint':endpoints[0],
                                 'device_id':body.decode(errors='replace')}
                        event('stock_usb_responding',**details);return dev,details
            except usb.core.USBError as error:last_error=str(error)
            usb.util.dispose_resources(dev)
        if time.monotonic()-last_progress>=30:
            event('waiting_for_stock_usb',last_error=last_error);last_progress=time.monotonic()
        time.sleep(.25)
    raise TimeoutError('Stock USB identity not observed; no automatic reset/reflash')

def main():
    global OUT,DEST,STARTUP_PROOF
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=('install','verify-rescue'))
    p.add_argument('--directory',type=Path,default=OUT)
    p.add_argument('--startup-proof',type=Path,default=STARTUP_PROOF,
                   help='Physical bounded startup proof built from exactly the same sources')
    p.add_argument('--from-held',action='store_true',help='Install from already held source rescue')
    args=p.parse_args()
    STARTUP_PROOF=args.startup_proof.resolve()
    if args.directory.resolve()!=OUT.resolve():
        OUT=args.directory.resolve();DEST=ROOT/'emu/evidence/stockapp/bringup'/OUT.name
    data=preflight()
    DEST.mkdir(parents=True,exist_ok=True)
    path=DEST/('physical_'+args.phase+'.json')
    if path.exists():raise SystemExit('Refusing to overwrite physical evidence or repeat an upload')
    report={'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'phase':args.phase,'package_sha256':sha(data),'result':'started','events':[]}
    def event(name,**details):
        row={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'event':name,**details}
        report['events'].append(row);path.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(row),flush=True)
    dev=None
    try:
        if args.phase=='install':
            dev,status=fwctl.wait_device('bootloader' if args.from_held else 'application')
            assert status['app_valid'] and not status['busy'] and not status['reboot_blocked']
            if args.from_held: assert status['held'] and status['layout_version']==2
            else: assert dev.product in ('MG2550s App','MG2550s Stock Record')
            event('held_rescue_ready' if args.from_held else 'original_app_ready',info=status)
            with fwctl.Bridge('/dev/cu.usbmodem1201') as bridge:event('wiring',result=bridge.detect())
            if not args.from_held:
                fwctl.request_reboot(dev,1);dev=None
                dev,_=fwctl.wait_device('bootloader')
            smoke_host.upload(dev,data,event,timeout=240)
            event('starting_running_stock_app');fwctl.request_reboot(dev,2);dev=None
            time.sleep(.5);dev,details=wait_stock(event)
            report['result']='stock_runtime_installed_and_responding'
            if json.loads((OUT/'manifest.json').read_text()).get('telemetry'):
                from stocktrace import Reader
                reader=Reader(lambda req,value,index,length:dev.ctrl_transfer(0xc0,req,value,index,length,timeout=3000))
                snapshots=[]
                for _ in range(20):
                    snapshot=reader.snapshot()
                    assert snapshot['usb_state']['initialized']==1 and snapshot['usb_state']['attached']==1,snapshot
                    snapshots.append(snapshot);time.sleep(.05)
                history=reader.snapshot(history=True)
                # Normal stock control request still works after our requests.
                identity=bytes(dev.ctrl_transfer(0xa1,0,0,details['interface'],255,timeout=3000))
                assert b'MDL:MG2500 series;' in identity and b'VER:2.050;' in identity
                report['telemetry']={'snapshots':snapshots,'history':history,
                                     'normal_identity_after_telemetry':True}
                event('live_telemetry_verified',snapshots=len(snapshots),history_bytes=2048,
                      last_state=snapshots[-1]['usb_state'])
                report['result']='stock_telemetry_installed_and_verified'
        else:
            previous=json.loads((DEST/'physical_install.json').read_text())
            assert previous['result']=='stock_runtime_installed_and_responding' and previous['package_sha256']==sha(data)
            # This is the exact body exercised by validate.py's existing BJL
            # case. Adding ControlMode=Common suppresses this service command
            # in stock, as a separate negative parser experiment established.
            assert BJL==bytes.fromhex('1b5b4b0200001f')+b'BJLSTART\n@FROMUPMODE\nBJLEND\n'
            payload_test=json.loads((OUT/'guard_validation.json').read_text())['cases']['bjl']
            assert payload_test['reason']=='0xfd' and payload_test['held_rescue_after_real_reset']
            dev,details=wait_stock(event,20)
            event('sending_guarded_stock_rescue_request',payload_hex=BJL.hex())
            if dev.write(details['out_endpoint'],BJL,timeout=10000)!=len(BJL):raise RuntimeError('Short rescue command write; no resend')
            usb.util.dispose_resources(dev);dev=None
            dev,status=fwctl.wait_device('bootloader',timeout=180)
            assert status['held'] and status['app_valid'] and not status['busy']
            event('stock_usb_command_reached_held_rescue',info=status)
            fwctl.request_reboot(dev,2);dev=None;time.sleep(.5)
            dev,details=wait_stock(event)
            report['result']='usb_rescue_verified_stock_running_again'
    except BaseException as error:
        report['result']='error';report['error']=str(error);raise
    finally:
        if dev is not None:usb.util.dispose_resources(dev)
        path.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'result':report['result'],'report':str(path)}),flush=True)

if __name__=='__main__':main()
