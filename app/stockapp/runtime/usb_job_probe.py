#!/usr/bin/env python3
"""Black-box USB enumeration/job probe on the stockapp startup harness.

Runs the native-startup telemetry build with a scripted USB host for more
ticks than startup_validation.py, optionally streaming a print job to the
printer bulk OUT endpoint, and dumps the same software state the hardware
telemetry exposes (USB state bytes, SETUP rings, message-ID ring) so the
emulated sequence can be diffed against physical captures.

Host/engine model rules (RE brief 9 answer, 2026-10-26, plus observations):
- SETUP raises EP0 OUT status bit 3 only; the DMA pointer is advanced by 8.
- EP0 IN: the host's IN demand is reported as 0x908 bit 13; stock then
  programs 0x910 (size) / 0x914 (buffer) and enables 0x900; the model moves
  that packet and reports 0x908 bit 0 (message f023).
- Core bit 7 acknowledges a global OUT-NAK request (DCTL 0x804 bit 9 sets
  status bit 3 and raises core bit 7; bit 10 releases it).
- Engine commands are 32-byte descriptors posted via 0xc0750080; completions
  are read once from 0xc0750084 as 0x80000000|descriptor|type and reported on
  VIC source 68; type 0 only for codes the decoder knows (0x3401/0x3402/
  0xf004/0xf005), otherwise type 1. 0x3401(ep,len,buf) receives bulk data.
- 0xc07e0600 is a liveness word stock polls; it must change while connected.
- Enumeration done is presented as core bit 13 with the mask preset
  (experiment; stock's own mask never enables bit 13).
- Core mask bits 18/19 are modeled as not software-clearable (observed:
  stock never re-enables them).
Experiment only; nothing is asserted and reaching a stage proves nothing.
"""
import argparse,json,struct,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'app/stockapp/runtime'),str(ROOT/'emu/stockapp'),str(ROOT/'emu'),str(ROOT/'tools')]
from startup_validation import StartupHost
from stock import startup
from stock.scheduler import TickScheduler, IDLE, ISR_TABLE
from stocktrace import REGIONS
from unicorn.arm_const import UC_ARM_REG_PC,UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3,UC_ARM_REG_R4,UC_ARM_REG_LR

CONTROL_OBJECT=0x180409f8
ENGINE_TYPE0={0x3401,0x3402,0xf004,0xf005}


class JobHost(StartupHost):
    enum_mode='bit13'; job_interface='print'; trace_from=None; watch=(); window=250; mem_watch=()
    def __init__(self,**kw):
        super().__init__(**kw)
        self.engine_pending=[]; self.engine_done=[]; self.engine_word=0; self.engine_irq_due=False; self.engine_sources_cache=None
        self.in_demand_at=None; self.in_packets=0; self.dctl_status=0; self.outnak_at=None; self.engine_poll_word=0; self.engine_poll_at=0
        self.path=[]; self.watched=[]; self.engine_reads=[]; self.bulk_commands=[]
        self.uc.hook_add(__import__('unicorn').UC_HOOK_MEM_WRITE,self._shared_write,begin=0x10000000,end=0x1001ffff)
        self.shared_writes=[]; self.shared_reads=[]; self.mem_watch_log=[]
        for a in self.mem_watch:
            self.uc.hook_add(__import__('unicorn').UC_HOOK_MEM_WRITE,self._mem_watch,begin=a,end=a+3)
        self.uc.hook_add(__import__('unicorn').UC_HOOK_MEM_READ,self._shared_read,begin=0x10000000,end=0x1001ffff)

    # ----- bookkeeping hooks -----
    def _shared_read(self,cpu,access,address,size,value,user):
        if self.feed_started and self.current_task in (60,61,62,63,72) and len(self.shared_reads)<400 and not (self.feed_buffer<=address<self.feed_buffer+self.feed_length):
            self.shared_reads.append({'tick':self.ticks,'pc':hex(cpu.reg_read(UC_ARM_REG_PC)),'address':hex(address),'size':size,'value':hex(int.from_bytes(bytes(cpu.mem_read(address,size)),'little')),'task':self.current_task})
    feed_started=False; feed_buffer=0; feed_length=0; feed_done=False; status_token=None; status_irq_due=False
    def special_feed_step(self):
        """Experiment: once configured and idle, copy job bytes into the engine's special receive
        buffer announced by 0xf001(buffer,length,1) and raise status token 0xc0010000 (brief 9:
        selector 2, bit 16 -> event 0x31:0x200 -> message f03f, the one seen at print start)."""
        if self.feed_done or not self.configured or self.current or self.requests or not self.bjl: return
        f001=[c for c in self.engine_done if c['code']==0xf001]
        if not f001: return
        words=[int(w,16) for w in f001[-1]['words']]; self.feed_buffer,self.feed_length=words[2],words[3]
        n=min(self.feed_chunk,len(self.bjl)); self.uc.mem_write(self.feed_buffer,self.bjl[:n]); self.bulk_position=n
        self.feed_started=True; self.feed_done=True; self.status_token=0xc0010000; self.status_irq_due=True
        self.usb_events.append({'tick':self.ticks,'event':'special-path feed experiment','buffer':hex(self.feed_buffer),'bytes':n,'token':hex(self.status_token)})
    feed_chunk=512
    def _mem_watch(self,cpu,access,address,size,value,user):
        if len(self.mem_watch_log)<300: self.mem_watch_log.append({'tick':self.ticks,'pc':hex(cpu.reg_read(UC_ARM_REG_PC)),'address':hex(address),'size':size,'value':hex(value),'task':self.current_task,'lr':hex(cpu.reg_read(UC_ARM_REG_LR))})
    def _shared_write(self,cpu,access,address,size,value,user):
        if self.ticks>=self.window and len(self.shared_writes)<300 and not (0x1000c4f0<=address<0x1000c5b0):
            self.shared_writes.append({'tick':self.ticks,'pc':hex(cpu.reg_read(UC_ARM_REG_PC)),'address':hex(address),'size':size,'value':hex(value),'task':self.current_task})
    def code(self,cpu,address,size,user):
        super().code(cpu,address,size,user)
        if self.trace_from is not None and self.ticks>=self.trace_from and self.current_task in (60,61,62,63,72) and 0x49b00<=address<0x53000:
            if len(self.path)<8000 and (not self.path or self.path[-1]!=address): self.path.append(address)
        if address in self.watch and self.ticks>=(self.trace_from or self.window) and len(self.watched)<200:
            self.watched.append({'tick':self.ticks,'pc':hex(address),'task':self.current_task,'path_index':len(self.path),
                'r0-r4':[hex(cpu.reg_read(r)) for r in (UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3,UC_ARM_REG_R4)],'lr':hex(cpu.reg_read(UC_ARM_REG_LR))})
    def engine_sources(self):
        if self.engine_sources_cache is None:
            found={}
            for src in range(96):
                h=struct.unpack('<I',bytes(self.uc.mem_read(ISR_TABLE+8*src,4)))[0]
                if h in (0x51843,0x5184b,0x51853): found[h]=src
            self.engine_sources_cache=found
        return self.engine_sources_cache

    # ----- register model -----
    def read(self,cpu,off,size,user):
        address=startup.PERI+off
        if address==0xc07e0600 and self.connected: return (self.ticks*7+self.instructions//4096)&0xffffffff
        if address==0xc0750084:
            if self.engine_word: value=self.engine_word; self.engine_word=0; return value
            if self.engine_poll_word and self.ticks>=self.engine_poll_at:
                value=self.engine_poll_word; self.engine_poll_word=0
                self.usb_events.append({'tick':self.ticks,'event':'engine poll completion read','type':value&3}); return value
            return 0
        if address==0xc075008c and self.status_token is not None:
            value=self.status_token; self.status_token=None
            self.usb_events.append({'tick':self.ticks,'event':'status FIFO token read','token':hex(value)}); return value
        if address==0xc0780804: return (self.registers.get(address,0)&~0x8)|self.dctl_status
        value=super().read(cpu,off,size,user)
        if 0xc0750000<=address<0xc0760000 and self.ticks>=self.window and len(self.engine_reads)<400:
            self.engine_reads.append({'tick':self.ticks,'pc':hex(cpu.reg_read(UC_ARM_REG_PC)),'address':hex(address),'value':hex(value),'task':self.current_task})
        return value
    def write(self,cpu,off,size,value,user):
        address=startup.PERI+off
        if address==0xc0780018: value|=self.reg(0x18)&0xc0000
        if address==0xc0780804:
            if value&0x200: self.dctl_status=0x8; self.outnak_at=self.ticks+1
            if value&0x400: self.dctl_status=0
            value&=~0x600
        if address==0xc0750080:
            if value&0x3ffffff0: self.engine_submit(value)
            else:
                # Flags-only command (0x4b7d6 submits flags 3, 0x4b7fc flags 2) followed by a
                # polling wait for a completion of the same type (0x4b7e2 / 0x4b808): report
                # it by polling only, no interrupt (brief 9: types 1/2 raise no ISR event).
                self.engine_poll_word=0x80000000|(value&3); self.engine_poll_at=self.ticks+1
                self.usb_events.append({'tick':self.ticks,'event':'engine flags-only command','flags':value&3})
        super().write(cpu,off,size,value,user)
    def transfer(self,direction,number,data=b'',is_setup=False):
        data=super().transfer(direction,number,data,is_setup)
        if is_setup: self.setreg(self.endpoint(direction,number)+8,8); self.summarize()
        return data

    # ----- engine -----
    def engine_submit(self,value):
        addr=value&0x3ffffff0; raw=bytes(self.uc.mem_read(addr,32)); code=struct.unpack_from('<H',raw,0)[0]; words=struct.unpack('<8I',raw)
        cmd={'tick':self.ticks,'value':value,'addr':addr,'code':code,'words':[hex(w) for w in words]}
        self.engine_pending.append(cmd)
        self.usb_events.append({'tick':self.ticks,'event':'engine command','code':hex(code),'addr':hex(addr),'words':[hex(w) for w in words[2:5]]})
    def engine_step(self):
        if self.engine_word or not self.engine_pending or self.engine_pending[0]['tick']>=self.ticks: return
        cmd=self.engine_pending.pop(0); self.engine_done.append(cmd)
        if cmd['code']==0x3401:
            # Receive request (endpoint, length, buffer): feed the job stream into the buffer.
            words=[int(w,16) for w in cmd['words']]; ep,length,buffer=words[2],words[3],words[4]
            chunk=b''
            if ep==self.bulk_out_ep and self.configured and self.bulk_position<len(self.bjl):
                chunk=self.bjl[self.bulk_position:self.bulk_position+length]; self.bulk_position+=len(chunk)
                if chunk: self.uc.mem_write(buffer,chunk)
            self.bulk_commands.append({'tick':self.ticks,'ep':ep,'length':length,'buffer':hex(buffer),'delivered':len(chunk)})
            self.usb_events.append({'tick':self.ticks,'event':'engine receive filled','ep':ep,'length':length,'delivered':len(chunk)})
            if not chunk and ep==self.bulk_out_ep: self.engine_pending.insert(0,cmd); self.engine_done.pop(); return   # keep waiting for data
        ctype=0 if cmd['code'] in ENGINE_TYPE0 else 1
        self.engine_word=0x80000000|cmd['addr']|ctype; self.engine_irq_due=True
    def inject(self):
        self.engine_step(); self.special_feed_step()
        if self.status_irq_due and self.uc.reg_read(UC_ARM_REG_PC)==IDLE and self.vic_pending is None:
            src=self.engine_sources().get(0x51853)
            if src is not None:
                saved=(self.source,self.force); self.source,self.force=src,True
                TickScheduler.inject(self); self.timer_pending=False
                self.source,self.force=saved; self.status_irq_due=False
                self.usb_events.append({'tick':self.ticks,'event':'status FIFO IRQ','source':src}); return
        if self.engine_irq_due and self.uc.reg_read(UC_ARM_REG_PC)==IDLE and self.vic_pending is None:
            src=self.engine_sources().get(0x5184b)
            if src is not None:
                saved=(self.source,self.force); self.source,self.force=src,True
                TickScheduler.inject(self); self.timer_pending=False
                self.source,self.force=saved; self.engine_irq_due=False
                self.usb_events.append({'tick':self.ticks,'event':'engine completion IRQ','source':src,'code':hex(self.engine_done[-1]['code'])})
                return
        super().inject()

    # ----- host -----
    def parse_config(self,data):
        super().parse_config(data)
        eps=[(int(a,16),t) for a,t in self.descriptors.get('endpoints',[])]
        outs=[a for a,t in eps if t==2 and not a&0x80]
        if self.job_interface=='print' and len(outs)>=2: self.bulk_out_ep=outs[1]
        self.usb_events.append({'tick':self.ticks,'event':'bulk OUT endpoint selected','ep':self.bulk_out_ep})
    def attach(self):
        super().attach()
        from stock.usbhost import setup
        # Printer-class GET_DEVICE_ID addresses interface 1 (wIndex = interface<<8 | alternate),
        # as the Mac's SETUP ring showed (a1 00 0000 0100 0100); interface 0 is the scanner.
        self.requests=type(self.requests)([(n,pk) if n!='device_id' else ('device_id',setup(0xa1,0,0,0x0100,length=256)) for n,pk in self.requests])
    def ep0_in_step(self):
        """IN demand → stock programs a packet → move it → IN bit 0."""
        request=self.current
        if not request or request['phase']!='in': return False
        block=self.endpoint('in',0)
        if self.reg(block)&0x200000:
            self.stalls.append(request['name']); self.usb_events.append({'tick':self.ticks,'event':'stall','name':request['name']})
            self.current=None; self.in_demand_at=None; return True
        if ('in',0) in self.armed and not self.reg(block+8):
            size=self.reg(0x910); length=size&0x7ffff; buffer=self.reg(0x914)
            data=bytes(self.uc.mem_read(buffer,length)) if length and buffer else b''
            self.armed.discard(('in',0)); self.setreg(block,self.reg(block)&~0x80000000)
            self.setreg(0x910,size&~0x7ffff); self.setreg(0x914,buffer+length)
            self.setreg(block+8,self.reg(block+8)|1); self.in_packets+=1
            request['data'].extend(data)
            self.usb_events.append({'tick':self.ticks,'event':'EP0 IN packet','name':request['name'],'bytes':length})
            if not request['setup'][0]&0x80:
                self.finish()                                   # status ZLP of an OUT-type request
            elif len(request['data'])>=request['length'] or length<64:   # EP0 bMaxPacketSize0 = 64
                request['phase']='status'
            self.summarize(); return True
        if self.reg(0x840)&0x2000 and not self.reg(block+8) and not self.reg(block)&0x20000 and self.in_demand_at is None \
                and self.ticks>=getattr(self,'last_demand_tick',-10)+2:
            self.in_demand_at=self.ticks+1; self.last_demand_tick=self.ticks+1
        if self.in_demand_at is not None and self.ticks>=self.in_demand_at and not self.reg(block+8):
            self.in_demand_at=None; self.setreg(block+8,self.reg(block+8)|0x2000); self.summarize()
            self.usb_events.append({'tick':self.ticks,'event':'EP0 IN demand (bit 13)','name':request['name']}); return True
        return False
    force_mode=None; forced_mode=False
    def host_step(self):
        # Experiment: the printer session start (0x4e212) requires the machine mode mirrored
        # at ctx+0x14 (0x180409a0) to be 0x07000007 (ready); the emulated stock stays in the
        # power-on mode 0x0200000f because mechanical initialization is not modeled.
        if self.force_mode is not None and not self.forced_mode and self.bus_stage>=2 and self.ticks>=getattr(self,'stage2_tick',0)+2:
            self.uc.mem_write(0x180409a0,struct.pack('<I',self.force_mode)); self.forced_mode=True
            self.usb_events.append({'tick':self.ticks,'event':'machine mode forced at ctx+0x14 (experiment)','mode':hex(self.force_mode)})
        if self.outnak_at is not None and self.ticks>=self.outnak_at:
            self.outnak_at=None; self.setreg(0x14,self.reg(0x14)|0x80)
            self.usb_events.append({'tick':self.ticks,'event':'core bit 7: global OUT NAK effective'})
        if self.bus_stage==1 and self.connected and not self.reg(0x14):
            # Enumeration done is reported only after stock's reset continuation (OUT-NAK
            # acknowledgement, special-path cancel, endpoint re-init) has drained, so the
            # EP0 arming it triggers is not wiped by that continuation.
            if not hasattr(self,'reset_tick'): self.reset_tick=self.ticks
            if self.ticks<self.reset_tick+self.enum_delay or self.engine_pending or self.engine_word or self.outnak_at is not None: return
            if self.enum_mode=='bit13':
                self.setreg(0x18,self.reg(0x18)|0x2000); self.setreg(0x14,0x2000)
                self.usb_events.append({'tick':self.ticks,'event':'enumeration done as core bit 13 (mask preset)'})
            self.bus_stage=2; self.stage2_tick=self.ticks; return
        # USB reset recovery: a host waits at least 10 ms after a reset before the first
        # SETUP; stock's own reset continuation (OUT-NAK ack, special-path cancel,
        # endpoint re-init) also needs that time, otherwise it wipes SET_ADDRESS.
        if self.bus_stage==2 and self.current is None and self.requests and self.ticks<getattr(self,'stage2_tick',0)+self.setup_delay: return
        if self.current and self.current['phase']=='in':
            if self.ep0_in_step(): return
            return
        super().host_step()
    setup_delay=10; enum_delay=4

    def probe_report(self,r):
        u=self.uc; state=list(u.mem_read(0x1804099c,4)); err=bytes(u.mem_read(0x1803fcc4,8))
        low=bytes(u.mem_read(0x1803fccc,1024)); high=bytes(u.mem_read(0x180400cc,1024)); n=err[3]
        def ring(address,index,count=64):
            raw=bytes(u.mem_read(address,512)); ids=[int.from_bytes(raw[2*k:2*k+2],'little') for k in range(256)]
            return [f'{ids[(index-count+k)%256]:04x}' for k in range(count)]
        return {'ticks':r['ticks'],'stop':r['result'].get('stop_reason'),'error':self.error,'faults':r.get('faults'),
            'usb_state':state,'error_and_indices_hex':err.hex(),'setup_history':[(low[4*k:4*k+4]+high[4*k:4*k+4]).hex() for k in range(n)],
            'messages_all_last64':ring(0x180404ce,err[1]),'messages_filtered_last64':ring(0x180406ce,u.mem_read(0x180404cc,1)[0]),
            'usb_errors':self.usb_errors,'dcfg':hex(self.reg(0x800)),
            'usb':{k:r['usb'][k] for k in ('irqs','connected','bus_stage','configured','address_loaded','responses','stalls','bulk_bytes_sent','bulk_total','armed_now')},
            'descriptors':{k:v for k,v in r['usb']['descriptors'].items() if k=='endpoints'},
            'events':[x for x in r['usb']['events'] if x['event'] not in ('transfer','arm')][:200],
            'transfers':[x for x in r['usb']['events'] if x['event']=='transfer'][:80],
            'engine_done':self.engine_done,'engine_pending':self.engine_pending,'bulk_commands':self.bulk_commands,'engine_sources':{hex(k):v for k,v in self.engine_sources().items()},
            'control_object':bytes(u.mem_read(CONTROL_OBJECT,0x20)).hex(),'device_object':bytes(u.mem_read(0x18040b38,0x60)).hex(),
            'coordination_bytes_0x180409a5':bytes(u.mem_read(0x180409a5,3)).hex(),'printer_stop_flag_0x18040b83':u.mem_read(0x18040b83,1)[0],
            'printer_object_state_0x18040b57':u.mem_read(0x18040b38+0x1f,1)[0],'ctx_mode_word':hex(struct.unpack('<I',bytes(u.mem_read(0x180409a0,4)))[0]),'geometry_0x1806c974':bytes(u.mem_read(0x1806c974,28)).hex(),'ep1_object':hex(struct.unpack('<I',bytes(u.mem_read(0x1803f904,4)))[0]),'ep1_object_words':[hex(w) for w in struct.unpack('<16I',bytes(u.mem_read(struct.unpack('<I',bytes(u.mem_read(0x1803f904,4)))[0],64)))],
            'usb_reads_after':[{k:x[k] for k in ('tick','pc','address','value','task')} for x in self.usb_reads if x['tick']>=self.window][:300],
            'usb_writes_after':[{k:x[k] for k in ('tick','pc','address','value','task')} for x in self.mmio_writes if x['tick']>=self.window and 0xc0700000<=int(x['address'],16)<0xc0800000][:400],
            'engine_reads':self.engine_reads,'shared_ram_writes':self.shared_writes[:200],'mem_watch':self.mem_watch_log,'shared_ram_reads':self.shared_reads,'watched':self.watched,'task_path':[hex(x) for x in self.path]}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory',type=Path); p.add_argument('--ticks',type=int,default=1500)
    p.add_argument('--budget',type=int,default=60_000_000); p.add_argument('--bjl',type=Path)
    p.add_argument('--output',type=Path); p.add_argument('--enum',choices=('bit13','none'),default='bit13')
    p.add_argument('--interface',choices=('print','scan'),default='print')
    p.add_argument('--trace-from',type=int); p.add_argument('--window',type=int,default=250)
    p.add_argument('--watch',default='',help='comma-separated hex PCs to log registers at'); p.add_argument('--setup-delay',type=int,default=10); p.add_argument('--watch-mem',default='',help='comma-separated hex addresses whose writes are logged'); p.add_argument('--force-mode',type=lambda v:int(v,0))
    a=p.parse_args(); out=a.directory.resolve()
    JobHost.mem_watch=tuple(int(x,16) for x in a.watch_mem.split(',') if x)
    e=JobHost(candidate=out/'candidate.bin',from_reset=True,boot_build=ROOT/'bootloader/build/vectors',
              eeprom=ROOT/'emu/inputs/eeprom.bin',usb_source=67,bjl=a.bjl.read_bytes() if a.bjl else b'')
    e.enum_mode=a.enum; e.job_interface=a.interface; e.trace_from=a.trace_from; e.window=a.window
    e.watch=tuple(int(x,16) for x in a.watch.split(',') if x); e.setup_delay=a.setup_delay; e.force_mode=a.force_mode
    e.boot(); r=e.run_usb(a.ticks,a.budget)
    text=json.dumps(e.probe_report(r),indent=2)+'\n'
    if a.output: a.output.write_text(text)
    else: print(text)
if __name__=='__main__': main()
