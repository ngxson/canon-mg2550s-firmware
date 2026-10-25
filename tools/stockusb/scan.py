"""PIXMA generation-5 scan flow, matching components/scanner/pixma_scan.c."""
from dataclasses import dataclass
import math
import struct
import time
import warnings
from .transport import ProtocolError

XML_PREFIX = '<?xml version="1.0" encoding="utf-8" ?><cmd xmlns:ivec="http://www.canon.com/ns/cmd/2008/07/common/"'
XML_START = (XML_PREFIX+'><ivec:contents><ivec:operation>StartJob</ivec:operation>'
    '<ivec:param_set servicetype="scan"><ivec:jobID>00000001</ivec:jobID>'
    '<ivec:bidi>1</ivec:bidi></ivec:param_set></ivec:contents></cmd>').encode()
XML_MODE = (XML_PREFIX+' xmlns:vcn="http://www.canon.com/ns/cmd/2008/07/canon/">'
    '<ivec:contents><ivec:operation>VendorCmd</ivec:operation>'
    '<ivec:param_set servicetype="scan"><ivec:jobID>00000001</ivec:jobID>'
    '<vcn:ijoperation>ModeShift</vcn:ijoperation><vcn:ijmode>1</vcn:ijmode>'
    '</ivec:param_set></ivec:contents></cmd>').encode()
XML_END = (XML_PREFIX+'><ivec:contents><ivec:operation>EndJob</ivec:operation>'
    '<ivec:param_set servicetype="scan"><ivec:jobID>00000001</ivec:jobID>'
    '</ivec:param_set></ivec:contents></cmd>').encode()


class StatusError(ProtocolError):
    def __init__(self, status):
        self.status=status
        super().__init__(f'PIXMA status 0x{status:04x}')


@dataclass(frozen=True)
class Params:
    dpi: int = 150
    x: int = 0
    y: int = 0
    width: int = 1240
    height: int = 1754
    calibrate: bool = True

    def validate(self):
        if self.dpi not in (75,150,300,600) or min(self.x,self.y)<0 or min(self.width,self.height)<=0:
            raise ValueError('Invalid scan resolution or rectangle')
        if self.x+self.width>638*self.dpi//75 or self.y+self.height>877*self.dpi//75:
            raise ValueError('Scan rectangle exceeds the platen')

    @property
    def raw_width(self): return (self.width+self.x%32+31)&~31


def command(code, payload=b'', incoming=0):
    payload=bytearray(payload)
    if payload: payload[-1]=(-sum(payload[:-1]))&255
    return struct.pack('>H10xI',code,len(payload)+incoming)+payload


def gamma_command():
    payload=bytearray(2056);payload[0]=0x10
    struct.pack_into('>H',payload,2,0x804)
    for i in range(1024):
        struct.pack_into('<H',payload,4+2*i,int(65535*math.pow(i/1023,1/2.2)+.5))
    return command(0xee20,payload)


def parameter_command(p):
    p.validate()
    data=bytearray(0x38)
    for offset,value in {0:1,1:1,2:1,5:int(p.calibrate),0x1c:8,0x1d:24,0x1f:1,
                         0x20:255,0x21:0x81,0x23:2,0x24:1,0x30:1}.items(): data[offset]=value
    struct.pack_into('>HH4I',data,8,p.dpi|0x8000,p.dpi|0x8000,p.x-p.x%32,p.y,p.raw_width,p.height)
    return command(0xd820,data)


class Scanner:
    def __init__(self, io, sleep=time.sleep, clock=time.monotonic, trace=None, timeout=300):
        self.io,self.sleep,self.clock=io,sleep,clock
        self.trace=trace or (lambda *a,**k: None)
        self.timeout=timeout
        self.deadline=float('inf')

    def check_time(self):
        if self.clock()>self.deadline: raise TimeoutError('Scan deadline exceeded')

    def write(self,data):
        self.check_time()
        n=self.io.write(data,1000)
        if n!=len(data): raise ProtocolError('Short scanner command write; not resent')

    def reply(self,n):
        timeouts=zlp=0
        while True:
            self.check_time()
            try: data=self.io.read(n,1000)
            except TimeoutError:
                timeouts+=1
                if timeouts>=8: raise
                continue
            if len(data)>n: raise ProtocolError('Oversized scan reply')
            if data: return data
            zlp+=1
            if zlp>4: raise ProtocolError('Too many empty USB packets')

    def execute(self,data,incoming=0):
        code=int.from_bytes(data[:2],'big')
        self.trace('scan_command',code=f'{code:04x}',out_bytes=len(data),in_bytes=8+incoming)
        self.write(data)
        reply=self.reply(8+incoming)
        if len(reply)<8: raise ProtocolError('Truncated PIXMA status header')
        status=int.from_bytes(reply[:2],'big')
        if len(reply) not in (8,8+incoming) or sum(reply[8:])&255:
            raise ProtocolError('Invalid PIXMA reply length/checksum')
        if status!=0x606: raise StatusError(status)
        if len(reply)!=8+incoming: raise ProtocolError('Successful PIXMA reply missing data')
        return reply[8:]

    def xml(self,body,label):
        self.trace('scan_xml',operation=label)
        self.write(body)
        response=self.reply(1024)
        if b'<ivec:response>OK</ivec:response>' not in response:
            raise ProtocolError(f'Scanner refused {label}')

    def drain(self):
        # Bounded cleanup, unlike an unbounded timeout/retry loop.
        end=self.clock()+2
        for _ in range(128):
            if self.clock()>end: break
            try: self.io.read(8192,100)
            except TimeoutError: break

    def start_session(self):
        for attempt in range(11):
            try:
                self.execute(command(0xdb20));return
            except StatusError as e:
                if e.status!=0x1414: raise
                if attempt<10: self.sleep(1)
            except TimeoutError: break
        self.drain()
        self.execute(command(0xef20));self.sleep(.5)
        self.execute(command(0xdb20))

    def scan(self,p,line):
        """Deliver cropped RGB rows; always attempt AbortSession/EndJob cleanup."""
        p.validate()
        self.deadline=self.clock()+self.timeout
        job=session=False
        error=None
        rows=0
        try:
            # Do not close somebody else's job after a refused StartJob.
            self.xml(XML_START,'StartJob');job=True
            self.xml(XML_MODE,'ModeShift')
            session=True;self.start_session()
            for _ in range(3): self.execute(gamma_command())
            self.execute(parameter_command(p));self.execute(command(0xd920))
            ready_end=min(self.deadline,self.clock()+120)
            for _ in range(481):
                if self.execute(command(0xda20,incoming=8),8)[0]&3: break
                if self.clock()>=ready_end: raise TimeoutError('Scanner calibration timed out')
                self.sleep(.25)
            else: raise TimeoutError('Scanner calibration timed out')
            self.sleep(1)
            pending=bytearray();flags=0;total=0
            expected=p.raw_width*3*p.height
            while flags&0x28!=0x28:
                self.check_time()
                request=32 if flags&0x20 else 512*1024
                self.trace('scan_image_request',maximum=request)
                self.write(command(0xd420,incoming=request+8))
                data=self.reply(8192)
                if len(data)<8: raise ProtocolError('Missing image status header')
                status=int.from_bytes(data[:2],'big')
                if status!=0x606:
                    if status==0x1515: self.execute(command(0xff20,incoming=16),16)
                    raise StatusError(status)
                if len(data)<16: raise ProtocolError('Missing image block header')
                flags=data[8]
                length=int.from_bytes(data[12:16],'big')
                if flags&~0x38 or length>request or length>expected-total:
                    raise ProtocolError('Invalid image flags or block length')
                chunk=data[16:];left=length
                if len(chunk)>left or left>len(chunk) and len(data)<8192:
                    raise ProtocolError('Truncated/oversized initial image block')
                while True:
                    pending.extend(chunk);left-=len(chunk);total+=len(chunk)
                    stride=p.raw_width*3
                    while len(pending)>=stride:
                        row=bytes(pending[3*(p.x%32):3*(p.x%32+p.width)])
                        if line(rows,row): raise InterruptedError('Scan consumer canceled')
                        rows+=1;del pending[:stride]
                    if not left: break
                    n=min(left,8192);chunk=self.reply(n)
                    if len(chunk)<n: raise ProtocolError('Image transfer ended early')
                if not length: self.sleep(.01)
            if total!=expected or pending or rows!=p.height:
                raise ProtocolError(f'Short image: {rows}/{p.height} rows')
            self.trace('scan_image_complete',rows=rows,raw_bytes=total)
        except BaseException as e:
            error=e
            raise
        finally:
            # Cleanup gets its own bounded deadline even after timeout/cancel.
            self.deadline=self.clock()+15
            cleanup=[]
            if session:
                if error is not None:
                    try: self.drain()
                    except Exception as e: cleanup.append(e)
                try: self.execute(command(0xef20))
                except Exception as e: cleanup.append(e)
            if job:
                try: self.xml(XML_END,'EndJob')
                except Exception as e: cleanup.append(e)
            if cleanup:
                message='Scanner cleanup failed: '+str(cleanup[0])
                if error is None: raise ProtocolError(message)
                warnings.warn(message)
        return {'rows':rows,'width':p.width,'dpi':p.dpi,'session_closed':True}
