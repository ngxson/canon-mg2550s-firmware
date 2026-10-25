#!/usr/bin/env python3
"""Independent C-reference tests; no USB devices are opened.

--reference points to the user's hobby-printer-esp32 checkout. Its portable C
fake scanner and actual encoder are compiled into a temporary shared library.
"""
import argparse
import ctypes as C
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import warnings
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
from stockusb.scan import Params,Scanner,command,XML_END,StatusError
from stockusb.raster import Page,job_stream,packbits,START,MODE,END
from stockusb.printing import print_page
from stockusb.transport import ProtocolError


def bind(lib,name,result,args):
    f=getattr(lib,name);f.restype=result;f.argtypes=args;return f


class Fake:
    def __init__(self,lib,busy=0,fail=0):
        self.lib=lib;self.p=lib.fake_new();assert self.p
        lib.fake_busy(self.p,busy);lib.fake_fail(self.p,fail)
        self.commands=[];self.inject=[];self.corrupt=False
    def write(self,data,timeout):
        data=bytes(data);self.commands.append(data)
        return self.lib.fake_write(self.p,data,len(data),timeout)
    def read(self,n,timeout):
        if self.inject:
            value=self.inject.pop(0)
            if isinstance(value,Exception): raise value
            return value
        b=C.create_string_buffer(n);got=self.lib.fake_read(self.p,b,n,timeout)
        if got==-2: raise TimeoutError('Synthetic USB timeout')
        if got<0: raise RuntimeError(got)
        raw=b.raw[:got]
        if self.corrupt and self.commands[-1][:2]==b'\xda\x20' and got==16:
            self.corrupt=False;raw=raw[:-1]+bytes([raw[-1]^1])
        return raw
    def check(self):
        assert self.lib.fake_errors(self.p)==0,self.lib.fake_error(self.p)
        assert self.lib.fake_closed(self.p)
    def close(self): self.lib.fake_delete(self.p)


def frames(path):
    data=path.read_bytes();result=[]
    while data:
        n=int.from_bytes(data[:4],'big');result.append(data[4:4+n]);data=data[4+n:]
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True)
    args=p.parse_args();ref=args.reference.resolve()
    sources=[ref/'components/scanner/fake_scanner.c',ref/'components/scanner/pixma_scan.c',
             ref/'components/canon_bjl/canon_bjl.c',ref/'components/canon_bjl/bjl_render.c']
    headers=[ref/'components/scanner/include/fake_scanner.h',ref/'components/scanner/include/pixma_scan.h',
             ref/'components/canon_bjl/include/canon_bjl.h',ref/'components/canon_bjl/include/bjl_render.h']
    report={'hardware_tested':False,'reference':str(ref),'cases':{},
            'reference_sha256':{str(f.relative_to(ref)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sources+headers}}
    with tempfile.TemporaryDirectory(prefix='stock-usb-test-') as temporary:
        tmp=Path(temporary);so=tmp/'reference.so'
        subprocess.run(['cc','-shared','-fPIC','-O2','-ffp-contract=off','-Wall','-Wextra',
            '-I'+str(ref/'components/scanner/include'),'-I'+str(ref/'components/canon_bjl/include'),
            str(Path(__file__).with_name('stock_reference.c')),*map(str,sources),'-lm','-o',str(so)],check=True)
        lib=C.CDLL(str(so));ptr=C.c_void_p;integer=C.c_int;size=C.c_size_t;string=C.c_char_p
        for name,result,inputs in [('fake_new',ptr,[]),('fake_delete',None,[ptr]),
            ('fake_write',integer,[ptr,string,size,integer]),('fake_read',integer,[ptr,ptr,size,integer]),
            ('fake_busy',None,[ptr,integer]),('fake_fail',None,[ptr,integer]),('fake_errors',integer,[ptr]),
            ('fake_error',string,[ptr]),('fake_closed',integer,[ptr]),('fake_pixels',None,[integer]*4+[ptr]),
            ('reference_scan',integer,[integer]*7+[string,string]),
            ('reference_print',integer,[integer,integer,string,integer,string]),
            ('bjl_packbits',size,[string,size,ptr])]: bind(lib,name,result,inputs)
        cases=[Params(dpi,3,79,137,91,True) for dpi in (75,150,300,600)]
        cases += [Params(150,1273,100,3,123,False),Params(75,0,0,638,877,True),Params(75,0,0,32,32,False)]
        for index,params in enumerate(cases):
            busy=2 if index==0 else 11 if index==6 else 0
            io=Fake(lib,busy=busy);pixels=[]
            result=Scanner(io,sleep=lambda s:None).scan(params,lambda y,row:pixels.append(row))
            io.check();raw=b''.join(pixels)
            # Compare every crop pixel with the independent C fake's page.
            for y,row in enumerate(pixels):
                expected=C.create_string_buffer(params.width*3)
                lib.fake_pixels(params.dpi,params.x,params.y+y,params.width,expected)
                assert row==expected.raw
            cmds=tmp/'commands';rgb=tmp/'image'
            assert lib.reference_scan(params.dpi,params.x,params.y,params.width,params.height,
                params.calibrate,busy,str(cmds).encode(),str(rgb).encode())==0
            assert raw==rgb.read_bytes()
            expected=frames(cmds)
            assert io.commands==expected,[(i,a[:16].hex(),b[:16].hex()) for i,(a,b) in enumerate(zip(io.commands,expected)) if a!=b]
            report['cases'][f'scan_{index}']={'dpi':params.dpi,'rows':result['rows'],'commands':len(expected),
                                           'all_pixels_and_commands_match_C':True}
            io.close()
        for kind in ('failed_block','cancel','bad_checksum','timeout_retry','zlp_retry','short_image'):
            io=Fake(lib,fail=1 if kind=='failed_block' else 0)
            if kind=='bad_checksum': io.corrupt=True
            if kind=='timeout_retry': io.inject=[TimeoutError('test')]*2
            if kind=='zlp_retry': io.inject=[b'']*4
            if kind=='short_image':
                original=io.read
                def shortened(n,timeout):
                    data=original(n,timeout)
                    if data and io.commands[-1][:2]==b'\xd4\x20': return data[:8]+b'\x28'+b'\0'*7
                    return data
                io.read=shortened
            thrown=None
            try:
                Scanner(io,sleep=lambda s:None).scan(Params(75,0,0,127,111),lambda y,row:kind=='cancel')
            except (ProtocolError,InterruptedError) as e: thrown=e
            assert (thrown is None)==(kind in ('timeout_retry','zlp_retry')),(kind,thrown)
            io.check()
            assert io.commands[-2:]==[command(0xef20),XML_END]
            report['cases'][kind]={'cleanup_closed':True,'expected_result':True}
            io.close()
        # Bounded timeout/empty-packet loops and invalid parameters.
        io=Fake(lib);io.inject=[b'']*5
        try: Scanner(io).reply(64);raise AssertionError('Empty packet flood accepted')
        except ProtocolError: pass
        before=len(io.commands)
        try: Scanner(io).scan(Params(120),lambda *a:None);raise AssertionError('Bad DPI accepted')
        except ValueError: pass
        assert len(io.commands)==before;io.close()
        report['cases']['input_bounds']={'passed':True}
        rng=np.random.default_rng(2550)
        for length in (0,1,2,3,127,128,129,255,1024,1198):
            for data in (bytes(length),b'\xff'*length,rng.integers(0,256,length,dtype=np.uint8).tobytes()):
                buf=C.create_string_buffer(length*2+20)
                n=lib.bjl_packbits(data,len(data),buf)
                assert packbits(data)==buf.raw[:n]
        report['cases']['packbits']={'C_comparisons':30}
        spec=importlib.util.spec_from_file_location('reference_decoder',ref/'tools/bjl_decode.py')
        decoder=importlib.util.module_from_spec(spec);spec.loader.exec_module(decoder)
        for dpi in (300,600):
            for paper_id,paper in enumerate(('A4','Letter')):
                page=Page(dpi,paper)
                rows=rng.integers(0,256,(19,page.width,3),dtype=np.uint8)
                rows[0]=255;rows[1]=0;rows[2,:,0:2]=255;rows[3,::2]=255
                source=tmp/'rgb';source.write_bytes(rows.tobytes());dest=tmp/'reference.prn'
                assert lib.reference_print(dpi,paper_id,str(source).encode(),len(rows),str(dest).encode())==0
                actual=b''.join(job_stream(page,rows));expected=dest.read_bytes()
                if actual!=expected:
                    first=next((i for i,(a,b) in enumerate(zip(actual,expected)) if a!=b),min(len(actual),len(expected)))
                    raise AssertionError(f'{dpi} {paper} differs at {first}: {actual[first:first+16].hex()} / {expected[first:first+16].hex()}')
                d=decoder.Decoder(actual,verbose=False);d.run()
                assert d.ff_count==1 and len(d.pages)==1
                report['cases'][f'print_{dpi}_{paper}']={'bytes':len(actual),'byte_identical_to_C':True,
                                                     'decoded_pages':1,'sha256':hashlib.sha256(actual).hexdigest()}
        # Print request ordering, concurrent reply drain and failed-write cancellation.
        import queue
        class Printer:
            def __init__(self,fail=False): self.input=queue.Queue();self.writes=[];self.reset=False;self.fail=fail
            def write(self,data,timeout):
                self.writes.append(data)
                if self.fail and len(self.writes)>2: raise TimeoutError('partial OUT')
                for body,name in ((START,'StartJob'),(MODE,'ModeShift'),(END,'EndJob')):
                    if data==body:
                        reply=(f'<cmd><ivec:operation>{name}Response</ivec:operation>'
                               '<ivec:response>OK</ivec:response></cmd>').encode()
                        self.input.put(reply[:19]);self.input.put(reply[19:])
                return len(data)
            def write_stalling(self,data,deadline,on_stall=None): return self.write(data,None)
            def read(self,n,timeout):
                try: return self.input.get(timeout=timeout/1000)
                except queue.Empty: raise TimeoutError('empty')
            def soft_reset_print(self): self.reset=True
        printer=Printer();page=Page()
        result=print_page(printer,page,[],ack_timeout=1,finish_timeout=1)
        assert result['end_job_acknowledged'] and not printer.reset
        assert b''.join(printer.writes)==b''.join(job_stream(page,[]))
        printer=Printer(fail=True)
        try: print_page(printer,page,[],ack_timeout=1,finish_timeout=1);raise AssertionError('failed write accepted')
        except TimeoutError: pass
        assert printer.reset and len(printer.writes)==3
        # The real printer answers every IN read with a fresh status; the
        # drain must poll like the ESP32 (250 ms), not back-to-back.
        class Chatty(Printer):
            def __init__(self): super().__init__();self.reads=0
            def read(self,n,timeout):
                self.reads+=1
                status=b'<cmd><ivec:operation>GetStatusResponse</ivec:operation><ivec:status>idle</ivec:status></cmd>'
                try: return self.input.get_nowait()
                except queue.Empty: return status
        chatty=Chatty();t0=time.monotonic()
        result=print_page(chatty,page,[],ack_timeout=1,finish_timeout=1)
        elapsed=time.monotonic()-t0
        assert result['end_job_acknowledged'] and not chatty.reset
        assert chatty.reads<=elapsed/0.25+8,(chatty.reads,elapsed)
        # Stall-tolerant OUT: libusb slices time out with exact progress; the
        # bytes are continued, never resent, and the deadline is enforced.
        from stockusb.transport import USB,ProtocolError as TransportError
        class Nak:
            def __init__(self,plan): self.plan=list(plan);self.got=bytearray();self.calls=0
            def __call__(self,ep,buf,n,moved,timeout):
                self.calls+=1
                accept,ret=self.plan.pop(0) if self.plan else (n,0)
                accept=min(accept,n);self.got+=bytes(buf[:accept]);moved._obj.value=accept
                if ret==-7: time.sleep(timeout/1000)   # a NAKing printer holds the slice until it times out
                return ret
        def transport(plan):
            io=object.__new__(USB);io.trace=lambda *a,**k: None;io.interface=1
            io.output=type('E',(),{'bEndpointAddress':1})();nak=Nak(plan)
            io._bulk_transfer=lambda: nak;io.port_status=lambda: 0x18
            return io,nak
        payload=bytes(range(256))*40
        io,nak=transport([(3000,0),(1000,-7),(0,-7),(0,-7),(2500,-7)])
        assert io.write_stalling(payload,time.monotonic()+5)==len(payload)
        assert bytes(nak.got)==payload and nak.calls==6,(nak.calls,)
        io,nak=transport([(1000,-7)]+[(0,-7)]*100)
        try: io.write_stalling(payload,time.monotonic()+0.3,slice_ms=50);raise AssertionError('deadline ignored')
        except TimeoutError: pass
        assert bytes(nak.got)==payload[:1000] and 3<=nak.calls<=10,(nak.calls,)
        io,nak=transport([(512,-9)])
        try: io.write_stalling(payload,time.monotonic()+5);raise AssertionError('pipe error accepted')
        except TransportError: pass
        assert bytes(nak.got)==payload[:512]
        report['cases']['print_transport']={'fragmented_acks':True,'no_resend_after_timeout':True,'cancel_on_error':True,
            'backchannel_polls_per_second_max':4,'chatty_reads':chatty.reads,'stall_resume_exact':True,'stall_deadline':True}
    report['passed']=True
    paths=list((ROOT/'tools/stockusb').glob('*.py'))+[ROOT/'tools/stock_scan.py',ROOT/'tools/stock_print.py',Path(__file__),Path(__file__).with_name('stock_reference.c')]
    report['source_sha256']={str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in paths}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
