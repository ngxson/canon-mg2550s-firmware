#!/usr/bin/env python3
"""Trace the logical-field API during boot-to-idle.

Hooks the stock field GETTER (0x111eea) and SETTER (0x111ed4) and logs every
(field_id, value, caller) seen while the stock-as-app build boots to its idle
loop. A one-shot hook on each getter's return address captures the value the
getter returns (r0), so we can see the *actual* ink-level / ink-low values the
firmware reads -- and, crucially, which callers read them.

Purpose: decide whether the reported ink status (CTK / MarkerSupplyAttention)
is produced through this field API (-> extend app/patch's ink clamp) or through
a path that never touches 0x111eea (-> different patch needed). Reference-first:
no patch is changed until this says which path the status uses.

Run in Docker (unicorn needs the image; sandbox SIGBUSes its JIT):
  docker run --rm --network none -v "$PWD:/project:rw" -w /project \
    --entrypoint bash mg2550s-emu -c \
    "timeout 600 python3 app/patch/trace_fields.py \
       --candidate emu/output/stockapp/patch_base/candidate.bin"
"""
import argparse, json, struct, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'emu' / 'stockapp'))
sys.path.insert(0, str(ROOT / 'emu'))
from probe import Boot  # noqa: E402
from unicorn import UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_LR)  # noqa: E402

BOOT_BUILD = ROOT / 'bootloader' / 'build' / 'vectors'
EEPROM = ROOT / 'emu' / 'inputs' / 'eeprom.bin'
GETTER = 0x111eea
SETTER = 0x111ed4

# inkLevel fields = module 2 / group 0x1c (the family app/patch currently clamps)
def is_inklevel(fid):
    return ((fid >> 16) & 0xff) == 0x1c and ((fid >> 24) & 0x3f) == 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--candidate', type=Path, required=True)
    ap.add_argument('--msgs', type=int, default=1200)
    ap.add_argument('--instrs', type=int, default=80_000_000)
    ap.add_argument('--output', type=Path)
    args = ap.parse_args()

    emu = Boot(candidate=args.candidate, from_reset=True,
               boot_build=BOOT_BUILD, eeprom=EEPROM)
    uc = emu.uc

    gets = []            # (id, value, caller)
    sets = []            # (id, value, caller)
    get_calls = Counter()
    pending = {}         # return_pc -> (handle, id)

    def finish(cpu, address, size, user):
        info = pending.pop(address, None)
        if info is None:
            return
        handle, fid = info
        val = cpu.reg_read(UC_ARM_REG_R0)
        gets.append((fid, val))
        try:
            uc.hook_del(handle)
        except Exception:
            pass

    def on_getter(cpu, address, size, user):
        fid = cpu.reg_read(UC_ARM_REG_R0)
        lr = cpu.reg_read(UC_ARM_REG_LR) & ~1
        get_calls[(lr, fid)] += 1
        # only capture return value for a bounded number of distinct ids
        if lr not in pending and len(pending) < 64:
            h = uc.hook_add(UC_HOOK_CODE, finish, begin=lr, end=lr)
            pending[lr] = (h, fid)

    def on_setter(cpu, address, size, user):
        fid = cpu.reg_read(UC_ARM_REG_R0)
        val = cpu.reg_read(UC_ARM_REG_R1)
        lr = cpu.reg_read(UC_ARM_REG_LR) & ~1
        sets.append((fid, val, lr))

    uc.hook_add(UC_HOOK_CODE, on_getter, begin=GETTER, end=GETTER)
    uc.hook_add(UC_HOOK_CODE, on_setter, begin=SETTER, end=SETTER)

    run_err = None
    try:
        emu.boot()
        emu.run_boot(args.msgs, args.instrs)
    except Exception as exc:
        run_err = f'{type(exc).__name__}: {str(exc)[:200]}'

    # distinct ids seen through the getter (with one sampled value)
    val_by_id = {}
    for fid, val in gets:
        val_by_id.setdefault(fid, val)
    distinct = sorted(val_by_id)

    ink = [(fid, val_by_id[fid]) for fid in distinct if is_inklevel(fid)]
    # callers grouped by id, so we can see WHERE ink ids are read
    callers_by_id = defaultdict(set)
    for (lr, fid), n in get_calls.items():
        callers_by_id[fid].add(lr)

    report = {
        'candidate': str(args.candidate),
        'run_error': run_err,
        'getter_total_calls': sum(get_calls.values()),
        'distinct_ids': len(distinct),
        'inklevel_fields_seen': [[hex(f), hex(v)] for f, v in ink],
        'inklevel_callers': {hex(f): [hex(c) for c in sorted(callers_by_id[f])]
                             for f, _ in ink},
        'all_ids_sample': [[hex(f), hex(val_by_id[f])] for f in distinct[:120]],
        'setter_calls': len(sets),
        'setter_sample': [[hex(f), hex(v), hex(c)] for f, v, c in sets[:60]],
        'setter_ink': [[hex(f), hex(v), hex(c)] for f, v, c in sets if is_inklevel(f)],
    }
    print(json.dumps(report, indent=2))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
