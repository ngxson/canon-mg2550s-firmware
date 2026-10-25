#!/usr/bin/env python3
"""Watch for source bootloader USB and hold it; never reset, erase, or program.

Start this before an independently authorized power cycle. It only addresses
the exact source bootloader product, so stock's vendor parser is not probed.
"""
import argparse
import datetime
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'tools'))
import fwctl
import usb.core
import usb.util


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',type=Path,required=True)
    p.add_argument('--timeout',type=float,default=120)
    args=p.parse_args()
    args.report.parent.mkdir(parents=True,exist_ok=True)
    report={'result':'watching','events':[]}
    with args.report.open('x') as out: json.dump(report,out)
    def event(name,**fields):
        row={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'event':name,**fields}
        report['events'].append(row)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(row),flush=True)
    event('watcher_ready')
    end=time.monotonic()+args.timeout
    try:
        while time.monotonic()<end:
            devices=list(usb.core.find(find_all=True,idVendor=0x04a9,idProduct=0x176d))
            if len(devices)>1: raise RuntimeError('Ambiguous Canon USB devices')
            for dev in devices:
                try:
                    if dev.product!='MG2550s Bootloader': continue
                    status=fwctl.get_info(dev)
                    assert status['mode']=='bootloader' and status['layout_version']==2
                    assert not status['busy'] and not status['reboot_blocked']
                    dev.ctrl_transfer(0x40,0x40,0,0,b'',timeout=3000)
                    status=fwctl.get_info(dev)
                    assert status['held']
                    report['result']='held_rescue';event('bootloader_held',info=status)
                    return
                except (usb.core.USBError,ValueError):
                    # Enumeration can disappear/reappear while descriptors load.
                    pass
                finally: usb.util.dispose_resources(dev)
            time.sleep(.1)
        raise TimeoutError('Bootloader was not observed; no update was sent')
    except BaseException as e:
        report['result']='error';event('error',error=str(e));raise


if __name__=='__main__': main()
