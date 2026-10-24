#!/usr/bin/env python3
"""Start one bounded mechanical-home attempt; collect status/logs; cancel on timeout.

This physically commands the motor. The firmware's mechanical-reference result
does not certify visible scanner motion or optical strip calibration.
"""
import argparse,json,struct,sys,time
from pathlib import Path
APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP.parent/'tools'))
from fwctl import wait_device,get_info

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--status-only',action='store_true')
    args=p.parse_args()
    import usb.util
    dev,info=wait_device('application')
    started=time.monotonic();log=bytearray();samples=[];last_phase=None;sent=False
    def status():
        raw=bytes(dev.ctrl_transfer(0xc0,0x75,0,0,32,timeout=1000))
        words=struct.unpack('<8I',raw)
        assert words[:2]==(0x4d48474d,1),words
        return dict(result=words[2],phase=words[3],position=words[4],
                    reference=words[5],first_stop=words[6],backoff_end=words[7])
    try:
        before=status()
        if not args.status_only:
            dev.ctrl_transfer(0x40,0x74,0,0,b'',timeout=1000);sent=True
        while True:
            for _ in range(8):
                data=bytes(dev.ctrl_transfer(0xc0,0x50,0,0,256,timeout=1000))
                if not data:break
                log.extend(data);sys.stdout.write(data.decode(errors='replace'));sys.stdout.flush()
            s=status();s['host_elapsed_s']=round(time.monotonic()-started,3);samples.append(s)
            if s['phase']!=last_phase:print('STATUS',json.dumps(s),flush=True);last_phase=s['phase']
            if args.status_only or (s['result']!=1 and s['host_elapsed_s']>.1):break
            if s['host_elapsed_s']>30:raise TimeoutError('Host homing deadline; cancelling')
            time.sleep(.05)
        after=get_info(dev)
        result={'before':before,'samples':samples,'management_after':after,
                'home_request_sent':sent,'visible_motion_confirmed':False,
                'physical_observation':'Requires user observation; counters are not visual proof.'}
        args.output.write_text(json.dumps(result,indent=2)+'\n')
        args.output.with_suffix('.log').write_bytes(log)
        print('FINAL',json.dumps(samples[-1]),flush=True)
    finally:
        if sent:
            try:
                if status()['result']==1:dev.ctrl_transfer(0x40,0x76,0,0,b'',timeout=1000)
            except Exception as exc:print('Cancel attempt:',exc,file=sys.stderr)
        usb.util.dispose_resources(dev)
if __name__=='__main__':main()
