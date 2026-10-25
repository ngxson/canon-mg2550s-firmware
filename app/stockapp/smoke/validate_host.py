#!/usr/bin/env python3
"""Simulate host phases, including failed upload and unexpected stock exit."""
import contextlib,io,json,struct,tempfile
from pathlib import Path
from unittest.mock import patch
import hardware as h

class Device:
    def __init__(self,payloads,reason=0xfe,fail=False,stuck=False):
        self.payloads=payloads;self.reason=reason;self.fail=fail;self.stuck=stuck
        self.product='MG2550s App';self.mode=2;self.status=0x80;self.uploads=[];self.resets=[]
        self.current=bytearray();self.target=None
    def ctrl_transfer(self,kind,request,value,index,payload,**kwargs):
        assert request!=0x44,'No whole-chip writer'
        if (kind,request)==(0xc0,0x42):
            return struct.pack('<8I',0x5746474d,2,self.mode,4|(9 if self.mode==1 else 0),0,self.status,0x10000,0x1f0000)
        if (kind,request)==(0xc0,0x96):
            assert (value,index,payload)==(0,0,32)
            return struct.pack('<8I',0x31504d53,0xceafb2ac,2,0xfffffffd,self.reason,self.reason^0xffffffff,0x13576a,0x30)
        if (kind,request)==(0xc0,0x95):
            assert index==0 and value<0x10000 and payload==256
            return (h.ROOT/'bootloader/build/vectors/boot_region.bin').read_bytes()[value:value+256]
        assert kind==0x40 and request in (0x40,0x43)
        if request==0x43:self.current=bytearray();self.expected=value|(index<<16);self.status=0x80
        return 0
    def write(self,endpoint,data,**kw):
        assert endpoint==1;self.current.extend(data)
        if len(self.current)==self.expected:
            raw=bytes(self.current);self.target=self.payloads.index(raw);self.uploads.append(self.target)
            self.status=0xa3 if self.fail else 0xc0
        return len(data)
    def reboot(self,dev,mode):
        if mode==2:assert self.status==0xc0
        self.resets.append(mode);self.status=0x80;self.mode=1
        if mode==2 and self.target==0:
            if self.stuck:self.mode=0
        elif mode==2:
            self.mode=2;self.product='MG2550s Stock Record' if self.target==1 else 'MG2550s App'
    def wait(self,mode=None,**kw):
        if self.mode==0:raise TimeoutError('simulated missing stock return')
        status=h.info(self);assert mode is None or mode==status['mode'];return self,status

class Bridge:
    def __init__(self,*a):pass
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def detect(self):return 'WIRING VERIFIED SDA4 SCL5 PROOF=REJECT90 FLOATING'

def main():
    payloads=h.preflight();cases={}
    for name,reason,fail,stuck in [('planned_stop',0xfe,False,False),('fault_exit_restored',0x13,False,False),
                                  ('upload_failed',0xfe,True,False),('stock_return_missing',0xfe,False,True)]:
        d=Device(payloads,reason,fail,stuck)
        with tempfile.TemporaryDirectory() as directory,contextlib.ExitStack() as stack:
            for target,value in [('DEST',Path(directory)),('fwctl.wait_device',d.wait),('fwctl.request_reboot',d.reboot),
                                 ('fwctl.Bridge',Bridge),('usb.util.dispose_resources',lambda d:None)]:
                stack.enter_context(patch('hardware.'+target,value))
            with contextlib.redirect_stdout(io.StringIO()):
                try:h.main();assert not (fail or stuck)
                except (RuntimeError,TimeoutError):assert fail or stuck
            r=json.loads((Path(directory)/'physical.json').read_text())
            if fail:assert d.uploads==[0] and d.resets==[1]
            elif stuck:assert d.uploads==[0] and d.resets==[1,2]
            else:
                assert d.uploads==[0,1,2] and d.resets==[1,2,2,1,2]
                assert r['planned_stop_confirmed']==(reason==0xfe)
                assert r['result'].endswith('original_restored')
            cases[name]={'passed':True,'uploads':d.uploads,'resets':d.resets}
        print('PASS',name)
    (h.STOCK/'host_validation.json').write_text(json.dumps({'passed':True,'cases':cases},indent=2)+'\n')

if __name__=='__main__':main()
