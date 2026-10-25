#!/usr/bin/env python3
"""Check and collect the vector candidate's build/regression/stock evidence."""
import difflib
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
BUILD=ROOT/'bootloader/build/vectors'
STOCK=ROOT/'emu/output/stockapp'
DEST=ROOT/'bootloader/evidence/vectors'


def check(value,message):
    if not value:raise ValueError(message)


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def norm(v):
    if isinstance(v,dict):return {k:norm(x) for k,x in v.items() if k!='instruction'}
    if isinstance(v,list):return [norm(x) for x in v]
    return v


def main():
    manifest=json.loads((BUILD/'manifest.json').read_text())
    for name,meta in manifest['artifacts'].items():
        check(sha(BUILD/name)==meta['sha256'],name+' build changed')
        check((BUILD/name).read_bytes()==(BUILD/'reproduce'/name).read_bytes(),name+' not reproducible')
    for relative,digest in manifest['source_sha256'].items():
        check(sha(ROOT/'bootloader'/relative)==digest,'source changed: '+relative)
    summary=dict(status='Candidate only: emulated, not flashed or physically validated',
        stage0_sha256=manifest['artifacts']['stage0.bin']['sha256'],
        boot_region_sha256=manifest['artifacts']['boot_region.bin']['sha256'],
        install_image_sha256=sha(BUILD/'install.bin'),reproducible=True,
        bootloader_suites={},stock={},source_sha256={})
    count=0
    for name in ('validation.json','dangerous_validation.json','vectors_validation.json','migration_validation.json'):
        r=json.loads((BUILD/name).read_text())
        check(r['all_assertions_passed'],name+' failed')
        summary['bootloader_suites'][name]=dict(cases=len(r['cases']),
            names=list(r['cases']),sha256=sha(BUILD/name))
        count+=len(r['cases'])
    summary['bootloader_checks']=count
    m=json.loads((BUILD/'migration_validation.json').read_text())
    check(m['target_sha256']==summary['install_image_sha256'],'migration image mismatch')
    current=(ROOT/'app/build/application.bin').read_bytes()
    check((BUILD/'install.bin').read_bytes()==
          ((BUILD/'boot_region.bin').read_bytes()+current).ljust(0x200000,b'\xff'),
          'install image does not retain current source application')
    summary['install_application_sha256']=hashlib.sha256(current).hexdigest()
    for scenario in ('startup','boot','home','reset-startup','reset-boot','reset-home'):
        path=STOCK/'registered'/(scenario+'.json')
        r=json.loads(path.read_text())
        check(r['assertions_passed'],scenario+' failed')
        a=r['audit'];check(a['registered_vectors'] and a['vector_model']=='flash',scenario+' used overlay')
        check(a['protected_prefix_unchanged'] and a['full_image_unchanged'],scenario+' changed flash')
        check(not a['spi_writes'] and not a['cpu_flash_stores'],scenario+' wrote flash/SPI')
        result=r['result'];state=result.get('result',result)
        out=dict(passed=True,pc=state['pc'],stop=state['stop_reason'],
            svcs=state['SVC_exceptions'],raw_report_sha256=sha(path),
            registration_intact=a.get('registration_intact'),loader_handoff=a.get('loader_handoff'))
        base=scenario.removeprefix('reset-')
        if base!='startup':
            native=json.loads((STOCK/(base+'-stock.json')).read_text())['result']
            fields=['transfers','context_restores']
            if base=='home':fields+=['moves','modeled_inputs']
            if not scenario.startswith('reset-'):
                fields+=['mmio_writes','motor_writes']
                if base=='home':fields+=['capture_writes']
            out['matched_records']={}
            for field in fields:
                check(norm(result[field])==norm(native[field]),scenario+': '+field+' differs')
                out['matched_records'][field]=len(result[field])
            out['ticks']=result['ticks'];out['irqs']=len(result['dispatches'])
            # Full reset retains initialized clocks/timers/USB registers. Record
            # differences rather than declaring all writes identical to the
            # intentionally zero-initialized native C-runtime fixture.
            if scenario.startswith('reset-'):
                check(a['registration_intact'] is True,'reserved SRAM clobbered')
                keys=('pc','address','value','size','task','tick','phase')
                left=[tuple(w.get(k) for k in keys) for w in native['mmio_writes']]
                right=[tuple(w.get(k) for k in keys) for w in result['mmio_writes']]
                diff=[]
                for tag,i,j,k,l in difflib.SequenceMatcher(a=left,b=right,autojunk=False).get_opcodes():
                    if tag!='equal':diff.append(dict(change=tag,native=left[i:j],from_reset=right[k:l]))
                out['mmio_diff_keys']=keys;out['mmio_differences']=diff
                controller=lambda rows:[norm(w) for w in rows
                    if 0xc0c56000<=int(w['address'],16)<0xc0c57408]
                check(controller(native['mmio_writes'])==controller(result['mmio_writes']),
                      'motor controller writes differ after reset')
                out['motor_controller_writes_identical']=True
        summary['stock'][scenario]=out
    summary['all_assertions_passed']=True
    physical=DEST/'audit'
    if (physical/'physical_read.json').exists() and (physical/'physical_restore.json').exists():
        readback=json.loads((physical/'physical_read.json').read_text())
        restored=json.loads((physical/'physical_restore.json').read_text())
        check(readback['result']=='boot_region_read_and_compared','physical boot read failed')
        check(restored['result']=='original_app_restored','physical original-app restoration failed')
        check((physical/'boot_read_1.bin').read_bytes()==(physical/'boot_read_2.bin').read_bytes()==
              (BUILD/'boot_region.bin').read_bytes(),'physical boot bytes differ from candidate')
        check(any(e['event']=='app_readback_verified_by_updater' and
                  e['sha256']==summary['install_application_sha256'] for e in restored['events']),
              'restored app differs from original')
        summary['status']='Installed: complete boot region read twice and verified; original app restored. Independent I2C recovery and exception forwarding remain physically unverified.'
        summary['physical_verification']={
            'boot_read_sha256':sha(physical/'boot_read_1.bin'),
            'read_report_sha256':sha(physical/'physical_read.json'),
            'restore_report_sha256':sha(physical/'physical_restore.json'),
            'original_app_restored':True,'record':'vectors/PHYSICAL.md'}
    files=list((ROOT/'bootloader/tests').glob('*.py'))+list((ROOT/'emu/stockapp').glob('*.py'))
    files+=list((ROOT/'app/stockapp').glob('*'))+list((ROOT/'emu/stock').glob('*.py'))
    for p in sorted(files):
        if p.is_file():summary['source_sha256'][str(p.relative_to(ROOT))]=sha(p)
    DEST.mkdir(parents=True,exist_ok=True)
    for name in ('manifest.json','vectors_validation.json','migration_validation.json'):
        (DEST/name).write_bytes((BUILD/name).read_bytes())
    (DEST/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(dict(bootloader_checks=count,stock_cases=len(summary['stock']),
        all_assertions_passed=True,install_image_sha256=summary['install_image_sha256']),indent=2))


if __name__=='__main__':main()
