#!/usr/bin/env python3
"""Compare every stock-written scanner C/P field at the first home arm.
Also expose wider clock/shared/power differences instead of hiding them in a
hand-picked register checklist. Last-written values do not prove silicon state.
"""
import argparse,hashlib,json,sys
from pathlib import Path
APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP/'tests'))
from validate_home import HomeModel,BOOT,C,P
from model import setup

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stock-trace',required=True,type=Path);args=p.parse_args()
    trace=json.loads(args.stock_trace.read_text());stock={};origins={}
    for w in trace['mmio_writes']:
        a=int(w['address'],16);v=int(w['value'],16);stock[a]=v;origins[a]=w['pc']
        if a==C and v==0x102:break
    boot=(BOOT/'build/boot_region.bin').read_bytes();app=(APP/'build/application.bin').read_bytes()
    meta={'layout':{'version':2,'application':[0x10000,0x200000]},'symbols':{
        'bootloader':json.loads((BOOT/'build/manifest.json').read_text())['symbols']['bootloader'],
        'application':json.loads((APP/'build/manifest.json').read_text())['symbols']['application']}}
    e=HomeModel(image=(boot+app).ljust(0x200000,b'\xff'),metadata=meta)
    e.run(lambda e:bool(e.info()) and e.info()[2]==2)
    e.requests.appendleft(('home',setup(0x40,0x74)))
    e.run(lambda e:e.first_arm is not None,resume=True)
    comparisons=[];differences=[]
    for a,v in sorted(stock.items()):
        motor=(C<=a<C+0x108 or P<=a<P+0x408)
        wider=(0xc0101000<=a<0xc0102008 or 0xc0c40000<=a<0xc0c58000 or 0xc0c81000<=a<0xc0c85700)
        if not wider:continue
        entry={'address':hex(a),'stock_value':v,'stock_pc':origins[a],
               'app_value':e.first_arm.get(a,0),'app_written':a in e.first_arm,
               'scanner_motor_local':motor}
        comparisons.append(entry)
        if v!=entry['app_value']:differences.append(entry)
    assert not any(d['scanner_motor_local'] for d in differences),differences
    report={'assumptions':__doc__,'app_sha256':hashlib.sha256(app).hexdigest(),
            'stock_sha256':trace['firmware_sha256'],'comparisons':comparisons,
            'wider_differences':differences,'all_scanner_motor_local_fields_match':True}
    (APP/'build/validation_home_setup.json').write_text(json.dumps(report,indent=2)+'\n')
    print('PASS',sum(c['scanner_motor_local'] for c in comparisons),'stock-written scanner motor fields')
    print('Wider differences:',json.dumps(differences,indent=2))
if __name__=='__main__':main()
