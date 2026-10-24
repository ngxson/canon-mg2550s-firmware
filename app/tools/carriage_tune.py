#!/usr/bin/env python3
"""Bench tuning of the carriage speed controller: try parameter sets, score smoothness.

For each set: apply with USB 0x6f, home, move to --target, move back to 1024,
parse the firmware's periodic [cr] trace lines, and report cruise-speed mean
and spread, approach behaviour, result and duration. Physically moves the
carriage. Ends with a home so the heads are capped.
"""
import argparse, json, re, statistics, subprocess, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
TOOL = str(APP / 'tools' / 'carriage.py')
NAMES = {0: 'kff_div', 1: 'kp_div', 2: 'ki_div', 3: 'integral_max', 4: 'brake_max', 5: 'floor_speed',
         6: 'decel_counts', 7: 'kick', 8: 'kick_ticks', 9: 'window'}
DEFAULTS = {0: 150, 1: 1200, 2: 8000, 3: 400, 4: 0, 5: 10000, 6: 8000, 7: 600, 8: 150, 9: 32}
SETS = {
    'A_default': {},
    'B_softer': {1: 1200, 2: 8000, 4: 100},
    'C_nobrake': {1: 1200, 2: 8000, 4: 0},
    'D_slow_pi': {1: 2400, 2: 8000, 4: 0, 9: 48},
    'E_lowint': {1: 1200, 2: 16000, 3: 300, 4: 0},
    'F_ff150': {0: 150, 1: 1200, 2: 8000, 4: 0},
    'G_win48': {0: 150, 1: 1200, 2: 8000, 4: 0, 9: 48},
    'H_win64': {0: 150, 1: 1200, 2: 8000, 4: 0, 9: 64},
    'I_kp1600': {0: 150, 1: 1600, 2: 8000, 4: 0, 9: 48},
    'J_kp2400': {0: 150, 1: 2400, 2: 6000, 4: 0, 9: 48},
    'K_ki12k': {0: 150, 1: 1600, 2: 12000, 3: 300, 4: 0, 9: 48},
}
TRACE = re.compile(r'\[cr\] t=(\d+) x=(\d+) speed=(\d+) wanted=(\d+) cmd=(\d+)')


def run(args):
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, timeout=60).stdout


def score(out, cruise):
    rows = [tuple(map(int, m)) for m in TRACE.findall(out)]
    final = json.loads(out.split('FINAL', 1)[1].strip()) if 'FINAL' in out else {}
    cruise_speeds = [s for _, _, s, w, _ in rows if w == cruise]
    approach = [s for _, _, s, w, _ in rows if w < cruise]
    return {'result': final.get('result'), 'x': final.get('x'), 'ms': None if not rows else rows[-1][0],
            'cruise_n': len(cruise_speeds),
            'cruise_mean': round(statistics.mean(cruise_speeds)) if cruise_speeds else None,
            'cruise_sd': round(statistics.pstdev(cruise_speeds)) if len(cruise_speeds) > 1 else None,
            'approach_zero_samples': sum(1 for s in approach if s == 0),
            'cmd_min': min(r[4] for r in rows) if rows else None, 'cmd_max': max(r[4] for r in rows) if rows else None}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target', type=int, default=60000)
    p.add_argument('--speed', type=int, default=30000)
    p.add_argument('--sets', nargs='*', default=list(SETS))
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    report = {}
    print(run(['home']).strip().splitlines()[-1])
    for name in args.sets:
        params = {**DEFAULTS, **SETS[name]}
        run(['tune', *[f'--set={k}={v}' for k, v in params.items()]])
        out_fwd = run(['move', str(args.target), '--speed', str(args.speed)])
        out_back = run(['move', '1024', '--speed', str(args.speed)])
        fwd, back = score(out_fwd, args.speed), score(out_back, args.speed)
        report[name] = {'params': {NAMES[k]: v for k, v in params.items()}, 'forward': fwd, 'back': back}
        print(f"{name:10s} fwd: {fwd['result']:>9} x={fwd['x']} sd={fwd['cruise_sd']} zero={fwd['approach_zero_samples']} ms={fwd['ms']} | "
              f"back: {back['result']:>9} x={back['x']} sd={back['cruise_sd']} zero={back['approach_zero_samples']} ms={back['ms']}", flush=True)
        run(['home'])
    run(['tune', *[f'--set={k}={v}' for k, v in DEFAULTS.items()]])
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
