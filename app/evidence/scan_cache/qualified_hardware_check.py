import sys,time,json,struct,argparse
from pathlib import Path
R=Path('/Users/ngxson/canon-eeprom/canon-mg2550s-firmware')
sys.path[:0]=[str(R/'tools'),str(R/'app/tools')]
import usb.core,usb.util
from fwctl import wait_device
from escl_usb import Connection,settings
from ctl import drain
from PIL import Image
p=argparse.ArgumentParser();p.add_argument('--probe-only',action='store_true');args=p.parse_args()
out=Path('/tmp/canon-scan-cache/qualified_hardware');out.mkdir(exist_ok=True)
report={'cases':[]}; d=None;conn=None
class Bounded(Connection):
 def read_more(self,timeout):
  try:super().read_more(min(timeout,1000))
  except usb.core.USBTimeoutError:pass

def reg(address):
 d.ctrl_transfer(0x40,0x90,address&65535,address>>16,b'',timeout=2000)
 return int.from_bytes(bytes(d.ctrl_transfer(0xc0,0x91,0,0,4,timeout=2000)),'little')
try:
 d,report['management_before']=wait_device('application',timeout=100)
 (out/'boot.log').write_text(drain(d))
 for it in reversed([i for i in d.get_active_configuration() if (i.bInterfaceClass,i.bInterfaceSubClass,i.bInterfaceProtocol)==(7,1,4)]):
  try:conn=Bounded(d,it);break
  except usb.core.USBError as ex:print('claim',it.bInterfaceNumber,ex,flush=True)
 if conn is None:raise RuntimeError('No eSCL interface available')
 for cycle in range(5):
  for path in ('ScannerCapabilities','ScannerStatus'):
   status,_,body=conn.request('GET','/eSCL/'+path,timeout=5000)
   assert status==200,(status,body)
  print('HTTP probe',cycle,'OK',flush=True)
 (out/'probe.log').write_text(drain(d))
 if not args.probe_only:
  for label,dpi,height in [('full75',75,3507),('full150',150,3507),('full300',300,3507),('repeat75',75,3507)]:
   status,h,b=conn.request('POST','/eSCL/ScanJobs',settings('RGB24',dpi,2480,height),'text/xml',timeout=15000)
   assert status==201,(status,b)
   job='/eSCL/'+h['location'].split('/eSCL/',1)[1];print(label,'created',job,flush=True)
   started=time.monotonic();status,_,b=conn.request('GET',job+'/NextDocument',timeout=120000)
   assert status==200,status
   image_path=out/f'{label}.png';image_path.write_bytes(b)
   im=Image.open(image_path);im.load()
   case={'name':label,'dpi':dpi,'download_seconds':time.monotonic()-started,'pixels':im.size,'bytes':len(b),'mode':im.mode};report['cases'].append(case)
   print(case,flush=True)
   deadline=time.monotonic()+40
   while time.monotonic()<deadline:
    state=struct.unpack('<16I',bytes(d.ctrl_transfer(0xc0,0x78,0,0,64,timeout=2000)))
    if state[2] in (7,8,9):break
    time.sleep(.5)
   case['total_seconds']=time.monotonic()-started;case['page_status']=state
   case['power']=reg(0xc0c85614);case['motor']=reg(0xc0c56000);case['gate']=reg(0xc0c57140)
   (out/f'{label}.log').write_text(drain(d))
   print(label,'idle audit',case['power'],case['motor'],case['gate'],'phase',state[2],flush=True)
   assert state[2]==7 and not (case['power'] or case['motor'] or case['gate']),case
   assert im.size==(2480*dpi//300,height*dpi//300) and im.mode=='RGB'
   status,_,_=conn.request('GET','/eSCL/ScannerStatus',timeout=5000);assert status==200
 report['passed']=True
except BaseException as ex:
 report['error']=repr(ex);print('FAILED',repr(ex),flush=True)
finally:
 if d is not None:
  if not report.get('passed'):
   try:
    d.ctrl_transfer(0x40,0x76,0,0,b'',timeout=2000);report['emergency_stop']='acknowledged'
    (out/'failure.log').write_text(drain(d))
   except Exception as ex:report['emergency_stop']=repr(ex)
  if conn:
   try:usb.util.release_interface(d,conn.number)
   except Exception:pass
  usb.util.dispose_resources(d)
 (out/('probe_result.json' if args.probe_only else 'result.json')).write_text(json.dumps(report,indent=2)+'\n')
if not report.get('passed'):raise SystemExit(1)
