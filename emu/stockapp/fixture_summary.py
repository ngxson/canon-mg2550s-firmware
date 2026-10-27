#!/usr/bin/env python3
"""Print a readable summary of a fixture.py report: per case, the ordered
service calls (decoded mailbox commands and request words), the MMIO writes
and the polling-loop evidence."""
import argparse
import json


def service_line(e):
    s = e['service']
    a = e['args']
    if s in ('mailbox_send', 'kernel_send'):
        extra = f"id={a['id']} cmd={e.get('command')} {e.get('decoded') or e.get('request_words')}"
    elif s == 'mailbox_receive':
        extra = f"id={a['id']} reply={e.get('reply')}"
    elif s == 'event_wait':
        extra = f"id={a['id']} mask={a['mask']} mode={a['mode']}"
    elif s == 'task_delay':
        extra = f"ticks={a['ticks']}"
    elif s == 'pool_alloc':
        extra = f"pool={a['pool']} size={a['size']}"
    else:
        extra = str(a)
    return f"  {e['instruction']:>9} {s:17} lr={e['lr']:9} {extra}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('report')
    ap.add_argument('--case', action='append', default=[])
    ap.add_argument('--mmio', action='store_true', help='list MMIO writes')
    ap.add_argument('--collapse', action='store_true', help='collapse consecutive identical service lines')
    opts = ap.parse_args()
    r = json.load(open(opts.report))
    print('boot', r['boot'])
    for c in r['cases']:
        if opts.case and c['name'] not in opts.case:
            continue
        print(f"\n== {c['name']} call={c['call']} args={c['args']} stub={c['stub']} cpsr={r['conventions']['cpsr']}")
        print(f"   {c['stop_reason']}; r0={c['r0']} ({hex(c['r0'])}) instr={c['instructions']} "
              f"mmio_writes={len(c['mmio_writes'])} services={len(c['services'])} problems={c['problems']}")
        if c['windows_changed']:
            for k, v in c['windows_changed'].items():
                print(f"   window {k}: {v['before']} -> {v['after']}")
        last, count = None, 0
        for e in c['services']:
            line = service_line(e)
            key = line[12:]
            if opts.collapse and key == last:
                count += 1
                continue
            if count:
                print(f'            ... repeated {count} more times')
            last, count = key, 0
            print(line)
        if count:
            print(f'            ... repeated {count} more times')
        if c.get('loop_window_reads') and 'loop' in c['stop_reason']:
            print('   loop window RAM reads (pc, address, count):')
            for row in c['loop_window_reads'][:12]:
                print('     ', row)
        if opts.mmio and c['mmio_writes']:
            print('   MMIO writes (instruction, pc, address, value):')
            for w in c['mmio_writes']:
                print(f"     {w['instruction']:>9} {w['pc']:9} {w['address']} = {w['value']}")
            print('   read tail:', [(t['pc'], t['address'], t['value']) for t in c['mmio_read_tail'][-6:]])


if __name__ == '__main__':
    main()
