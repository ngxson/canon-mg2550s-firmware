#!/usr/bin/env python3
"""Verify differential reports and save compact evidence without EEPROM bytes."""
import hashlib
import json
from pathlib import Path
import unicorn

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'emu/output/stockapp'
EVIDENCE = ROOT/'emu/evidence/stockapp'


def check(value, message):
    if not value:
        raise ValueError(message)


def normalized(value):
    # Shim/vector/veneer instruction counts differ. Retain every other field,
    # including PC, order, task, tick, register address and value.
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k != 'instruction'}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def main():
    names = ['startup-frozen', 'startup-missing-helpers', 'startup-overlay',
             'boot-stock', 'boot-overlay', 'home-stock', 'home-overlay']
    reports = {name: json.loads((OUT/f'{name}.json').read_text()) for name in names}
    summary = dict(status='Offline feasibility evidence; NOT hardware-qualified firmware',
        emulator={'engine': 'Unicorn', 'version': unicorn.__version__, 'cpu': 'UC_CPU_ARM_946'},
        all_assertions_passed=True, build=json.loads((OUT/'manifest.json').read_text()),
        cases={}, comparisons={}, source_sha256={})
    for name, report in reports.items():
        check(report['assertions_passed'], f'{name} failed')
        audit = report['audit']
        check(audit['protected_prefix_unchanged'] and audit['full_image_unchanged'], name)
        check(not audit['cpu_flash_stores'] and not audit['spi_writes'], name)
        # This is the bounded evidence for an overlay surviving these paths;
        # no stock CP15/cache operations or low flash data dependencies occurred.
        check(not audit['cp15'], name)
        if audit['candidate']:
            check(not audit['low_reads'], name)
        else:
            # Native stock's low vectors use a literal pool. The proposed
            # instruction-only overlay deliberately uses direct B opcodes.
            check(all((r['pc'], r['address'], r['size']) in
                      (('0x8', '0x28', 4), ('0x18', '0x34', 4))
                      for r in audit['low_reads']), name)
        result = report['result']
        state = result.get('result', result)
        summary['cases'][name] = dict(
            expected=report['expected'], passed=True, pc=state['pc'],
            stop=state['stop_reason'], error=state['error'],
            svcs=state['SVC_exceptions'], vector_fetches=audit['vectors'],
            low_pcs=audit['low_pcs'], low_reads=audit['low_reads'],
            protected_prefix_unchanged=True,
            raw_report_sha256=hashlib.sha256((OUT/f'{name}.json').read_bytes()).hexdigest())
    for scenario in ('boot', 'home'):
        stock = reports[f'{scenario}-stock']['result']
        app = reports[f'{scenario}-overlay']['result']
        fields = ['mmio_writes', 'motor_writes', 'transfers', 'context_restores']
        if scenario == 'home':
            fields += ['capture_writes', 'moves', 'modeled_inputs']
        compared = {}
        for field in fields:
            left, right = normalized(stock[field]), normalized(app[field])
            check(left == right, f'{scenario}: {field} differs')
            compared[field] = dict(count=len(left), identical=True, sha256=digest(left))
        # Check all RAM exposed to the stock trace via outcome contracts above,
        # not a fabricated claim of bit-identical complete CPU/hardware state.
        summary['comparisons'][scenario] = dict(ticks=app['ticks'],
            irq_dispatches=len(app['dispatches']), fields=compared)
    loader = json.loads((OUT/'loader.json').read_text())
    check(loader['assertions_passed'], 'loader validation failed')
    check(loader['candidate_sha256'] == summary['build']['candidate_sha256'],
          'loader used a different candidate')
    summary['loader'] = loader
    for path in sorted((ROOT/'emu/stockapp').glob('*')):
        if path.suffix in ('.py', '.S'):
            summary['source_sha256'][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted((ROOT/'emu/stock').glob('*.py')):
        summary['source_sha256'][str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE/'summary.json'
    target.write_text(json.dumps(summary, indent=2)+'\n')
    print(target)


if __name__ == '__main__':
    main()
