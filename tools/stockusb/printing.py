"""IVEC print exchange with a continuously drained bulk-IN backchannel."""
import threading
import time
import warnings
from .transport import ProtocolError
from .raster import START,MODE,END,page_stream


# The stock printer answers every bulk-IN read with a fresh ~1 KB
# GetStatusResponse and spends CPU on each one. The ESP32 therefore polls the
# back channel every 250 ms during a job (IN_POLL_JOB_MS) instead of reading
# back-to-back; a tight loop pulled ~1,500 replies/s from the real printer.
POLL_INTERVAL=0.25


STATUS_TAGS=('ivec:status','ivec:status_detail','ivec:current_support_code')
STATUS_KEYS=('BST','DJS','DBS','DOC','HRC','CTK','PID')


def parse_status(message):
    """Printer state fields from a GetStatusResponse; never document data."""
    if b'GetStatusResponse' not in message: return None
    text=message.decode('utf8','replace');out={}
    for tag in STATUS_TAGS:
        a=text.find('<'+tag+'>')
        if a>=0:
            b=text.find('<',a+len(tag)+2)
            out[tag.split(':')[1]]=text[a+len(tag)+2:b]
    for key in STATUS_KEYS:
        a=text.find(key+':')
        if a>=0: out[key]=text[a+len(key)+1:text.find(';',a)]
    return out


class Backchannel:
    def __init__(self,io,trace,poll_interval=POLL_INTERVAL,on_status=None):
        self.io,self.trace=io,trace
        self.poll_interval=poll_interval
        self.on_status=on_status;self.last_status=None
        self.cv=threading.Condition();self.messages=[];self.error=None
        self.stop=threading.Event();self.thread=threading.Thread(target=self.run,daemon=True)

    def note_status(self,message):
        status=parse_status(message)
        if status is None or status==self.last_status: return
        self.last_status=status
        self.trace('printer_status',**status)
        if self.on_status: self.on_status(status)

    def run(self):
        pending=bytearray()
        try:
            while not self.stop.is_set():
                try: data=self.io.read(512,200)
                except TimeoutError: continue
                if not data: continue
                pending.extend(data)
                if len(pending)>65536: raise ProtocolError('Unbounded print backchannel reply')
                while b'</cmd>' in pending:
                    end=pending.index(b'</cmd>')+6
                    message=bytes(pending[:end]);del pending[:end]
                    self.note_status(message)
                    with self.cv:
                        self.messages.append(message)
                        self.messages=self.messages[-64:]
                        self.cv.notify_all()
                # Read on immediately only while a reply is still incomplete.
                if not pending.strip(): pending.clear();self.stop.wait(self.poll_interval)
        except BaseException as e:
            with self.cv: self.error=e;self.cv.notify_all()

    def wait(self,needle,timeout):
        end=time.monotonic()+timeout
        with self.cv:
            while True:
                if self.error: raise self.error
                for i,message in enumerate(self.messages):
                    if needle.encode() in message:
                        self.messages.pop(i)
                        if b'<ivec:response>NG</ivec:response>' in message:
                            raise ProtocolError(f'Printer refused {needle}')
                        self.trace('print_ack',operation=needle)
                        return True
                remaining=end-time.monotonic()
                if remaining<=0: return False
                self.cv.wait(remaining)

    def close(self):
        self.stop.set();self.thread.join(3)
        if self.thread.is_alive(): raise RuntimeError('Print backchannel did not stop')


OUT_CHUNK=16384   # the ESP32's OUT_XFER_SIZE


def print_page(io,page,rows,trace=None,ack_timeout=5,finish_timeout=180,stall_timeout=900,on_stall=None,on_status=None):
    """stall_timeout bounds how long the printer may refuse data (printing,
    waiting for paper); the ESP32 waits without bound. Nothing is resent."""
    trace=trace or (lambda *a,**k: None)
    back=Backchannel(io,trace,on_status=on_status);back.thread.start()
    total=0;started=False
    def send(data):
        nonlocal total
        for offset in range(0,len(data),OUT_CHUNK):
            if back.error: raise back.error
            chunk=data[offset:offset+OUT_CHUNK]
            n=io.write_stalling(chunk,time.monotonic()+stall_timeout,on_stall)
            if n!=len(chunk): raise ProtocolError('Short print write; not resent')
            total+=n
    try:
        started=True
        for data,operation in ((START,'StartJob'),(MODE,'ModeShift')):
            trace('print_command',operation=operation);send(data)
            if not back.wait(operation,ack_timeout):
                # Same policy as the ESP32: these optional acknowledgements
                # may be absent (the print wrapper uses bidi=0).
                warnings.warn(f'No {operation} acknowledgement; continuing as on ESP32')
        trace('print_raster_start',dpi=page.dpi,paper=page.paper)
        pending=bytearray()
        for data in page_stream(page,rows):
            pending.extend(data)
            while len(pending)>=OUT_CHUNK:
                send(bytes(pending[:OUT_CHUNK]));del pending[:OUT_CHUNK]
        if pending: send(bytes(pending))
        trace('print_command',operation='EndJob');send(END)
        completed=back.wait('EndJob',finish_timeout)
        trace('print_transfer_complete',bytes=total,end_job_acknowledged=completed)
        return {'bytes_sent':total,'end_job_acknowledged':completed}
    except BaseException:
        if started:
            # Matches the ESP32 cancellation path after failed bulk output.
            # Do not resend a partial raster or inject XML into its middle.
            try: io.soft_reset_print()
            except Exception as e: warnings.warn(f'Printer-class cancellation failed: {e}')
        raise
    finally:
        back.close()
