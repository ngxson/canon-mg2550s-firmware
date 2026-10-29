#!/usr/bin/env python3
"""Stage the ink / paper-count candidates for a single hardware session.

Builds several labelled app/patch candidates into emu/output/stockapp/stage/<name>/
so they can be flashed and queried (USB status + service report, no paper) one
after another tomorrow. Every candidate rides the known-good scanning base
(native_startup + runtime + stay_awake + auto_power + fake_door); they differ
only in the ink / paper patches under test. See app/patch/STAGING.md for the
per-candidate test and expected observable.

Run from the repo:
    python3 app/patch/stage_candidates.py            # build all
    python3 app/patch/stage_candidates.py ink_report # build one
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                       # canon-mg2550s-firmware
sys.path.insert(0, str(ROOT / 'emu' / 'stockapp'))
import build as stockbuild                   # noqa: E402

BOOT = ROOT / 'bootloader' / 'build' / 'vectors'
OUT = ROOT / 'emu' / 'output' / 'stockapp' / 'stage'

# Base flags shared by every candidate (the proven boot+USB+scan build).
# telemetry=True adds the read-only vendor SETUP hook (0x51bf6) that now also
# serves request 0xd2 = "dump EEPROM working cache" (tools/dump_eeprom.py).
BASE = dict(register_vectors=True, runtime=True, native_startup=True,
            stay_awake=True, auto_power=True, fake_door=True, telemetry=True)

# name -> extra flags on top of BASE. Mechanisms are the verified ones from
# BRIEF_counters.md; the old vtable-based ink_clamp/paper_count are superseded
# (reports read via the registry family, page writes go through 0x3be30).
CANDIDATES = {
    # 1) Report ink full: IL(...) shows 15 (max nibble), CTK:CL,LOW line gone.
    'ink_report': dict(ink_report=True),
    # 2) + drop the ink status-detail codes 6/7/8 (MarkerSupplyAttention).
    'ink_report_detail': dict(ink_report=True, ink_detail=True),
    # 3) Counter/diag build: report-full + stop usage-counter tracking
    #    (0x3be30 page/scan/dot, 0xcb7e8 cartridge) + clear-counters (0xd3) +
    #    dump-EEPROM (0xd2). Verify with dump_eeprom.py / clear_counters.py.
    'diag_counters': dict(ink_report=True, ink_detail=True,
                          stop_tracking=True, clear_counters=True,
                          ink_off=True),
    # 4) + dark ink lamp: the ink-warning blink pattern 0x19941e replaced with
    #    the default dark pattern (data-only patch at 0x19941e). Verify: the
    #    flashing CL/BK lamp goes DARK while everything else stays as in
    #    diag_counters (status clean, counters clearable, INK_OFF works).
    'diag_ink_lamp': dict(ink_report=True, ink_detail=True,
                          stop_tracking=True, clear_counters=True,
                          ink_off=True, ink_lamp_off=True,
                          ink_off_boot=True),
}


def main():
    wanted = sys.argv[1:] or list(CANDIDATES)
    for name in wanted:
        if name not in CANDIDATES:
            raise SystemExit(f'unknown candidate {name!r}; choose from {list(CANDIDATES)}')
    for name in wanted:
        out = (OUT / name).resolve()
        out.mkdir(parents=True, exist_ok=True)
        print(f'\n==== building candidate: {name} ====')
        stockbuild.build(out, boot_directory=BOOT, **BASE, **CANDIDATES[name])
        print(f'---- {name} -> {out}')


if __name__ == '__main__':
    main()
