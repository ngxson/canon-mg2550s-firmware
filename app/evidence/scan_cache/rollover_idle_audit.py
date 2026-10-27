import sys,json,struct
from pathlib import Path
R=Path('/Users/ngxson/canon-eeprom/canon-mg2550s-firmware');sys.path[:0]=[str(R/'tools'),str(R/'app/tools')]
from fwctl import wait_device
from ctl import drain
import usb.util
m=json.loads(Path('/tmp/canon-scan-cache/rollover/manifest.json').read_text());syms=m['symbols']['application'];result={'package_sha256':m['package_sha256']}
d,result['management']=wait_device('application',timeout=10)
try:
 result['page_status']=struct.unpack('<16I',bytes(d.ctrl_transfer(0xc0,0x78,0,0,64,timeout=2000)))
 for name,address in [('scanner_power',0xc0c85614),('motor_drive',0xc0c56000),('acquisition_gate',0xc0c57140)]:
  d.ctrl_transfer(0x40,0x90,address&65535,address>>16,b'',timeout=2000)
  result[name]=int.from_bytes(bytes(d.ctrl_transfer(0xc0,0x91,0,0,4,timeout=2000)),'little')
  assert result[name]==0,(name,result[name])
 for name,base in [('canary_a',0xc0ffee00),('canary_b',0xdeadbe00)]:
  address=int(syms[name],16)
  data=b''.join(bytes(d.ctrl_transfer(0xc0,0x9d,(address+i)&65535,(address+i)>>16,64,timeout=2000)) for i in range(0,256,64))
  result[name]=list(struct.unpack('<64I',data));assert result[name]==list(range(base,base+64)),name
 d.ctrl_transfer(0x40,0x8c,1,0,b'',timeout=5000)
 Path('/tmp/canon-scan-cache/rollover_hardware/idle.log').write_text(drain(d))
 result['passed']=True
finally:
 usb.util.dispose_resources(d)
 Path('/tmp/canon-scan-cache/rollover_hardware/idle.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if not k.startswith('canary')}))
