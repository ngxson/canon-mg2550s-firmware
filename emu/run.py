#!/usr/bin/env python3
"""Run MG2550s stock experiments or replacement-firmware emulator tests.

All devices are modeled in memory. No USB, serial port or physical printer is
accessed. Run inside the supplied Linux container on macOS.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from stock.inputs import DEFAULT_FIRMWARE

ROOT = Path(__file__).resolve().parents[1]
APP_TESTS = ('app', 'home', 'motor', 'buttons', 'scanner', 'page', 'cartridge')


def positive(value):
    number = int(value, 0)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    for name, help_text in (
        ('startup', 'stock C runtime through panel initialization'),
        ('scheduler', 'stock grouped interrupts and scheduler'),
        ('boot', 'stock task startup with EEPROM and readiness inputs'),
        ('home', 'stock mechanical homing control flow'),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument('--firmware', type=Path, default=DEFAULT_FIRMWARE,
                         help='supported stock image (defaults to repository dump)')
        sub.add_argument('--output', type=Path, default=ROOT / f'emu/output/{name}.json')
        sub.add_argument('--verify', action='store_true',
                         help='also assert the reference scenario outcome')
        sub.add_argument('--budget', type=positive,
                         default=3_000_000 if name == 'startup' else
                         500_000 if name == 'scheduler' else 40_000_000,
                         help='instruction limit (boot-to-idle has a separate 5M limit)')
        if name != 'startup':
            sub.add_argument('--ticks', type=positive,
                             default={'scheduler': 400, 'boot': 2000, 'home': 3500}[name])
        if name in ('boot', 'home'):
            sub.add_argument('--eeprom', type=Path, required=True,
                             help='raw 1024-byte 24C08 image; never written')
        if name == 'startup':
            sub.add_argument('--matrix', action='store_true',
                             help='run ten verified completion/stall/release cases')
            sub.add_argument('--stall-transfer', type=int, choices=range(1, 6))
            sub.add_argument('--response', type=lambda v: int(v, 0), default=0x40,
                             choices=range(256), metavar='BYTE')
        elif name == 'scheduler':
            controls = sub.add_mutually_exclusive_group()
            controls.add_argument('--no-irq', action='store_true')
            controls.add_argument('--spurious', action='store_true')
        elif name == 'boot':
            sub.add_argument('--ready-level', type=int, choices=range(5), default=4)
        elif name == 'home':
            sub.add_argument('--feedback', choices=('stationary', 'backoff'), default='stationary')
            sub.add_argument('--no-completion', action='store_true',
                             help='with backoff feedback, withhold companion event/IRQ27')
            sub.add_argument('--no-timer4', action='store_true')
            sub.add_argument('--no-serial-irq', action='store_true')
            sub.add_argument('--mode', type=lambda v: int(v, 0),
                             choices=(0, 0x0200000f, 0x0b00000f), default=0x0200000f)
    sub = commands.add_parser('test', help='run the existing replacement-firmware ARM suites')
    sub.add_argument('--suite', choices=('app', 'cartridge', 'bootloader', 'all'), default='app')
    return p


def replacement_tests(suite):
    needed = ['bootloader/build/boot_region.bin', 'bootloader/build/manifest.json']
    if suite in ('app', 'cartridge', 'all'):
        needed += ['app/build/application.bin', 'app/build/manifest.json']
    if suite in ('bootloader', 'all'):
        needed += ['bootloader/build/factory.bin', 'bootloader/build/application.bin',
                   'bootloader/build/bootloader.bin']
    missing = [name for name in needed if not (ROOT / name).is_file()]
    if missing:
        raise ValueError('Build firmware first (see emu/README.md). Missing: ' + ', '.join(missing))
    scripts = []
    if suite in ('bootloader', 'all'):
        scripts += ['bootloader/tests/validate.py', 'bootloader/tests/validate_dangerous.py']
    if suite in ('app', 'all'):
        scripts += [f'app/tests/validate_{name}.py' for name in APP_TESTS]
    elif suite == 'cartridge':
        scripts += ['app/tests/validate_cartridge.py']
    for script in scripts:
        print(f'Running {script}', flush=True)
        result = subprocess.run([sys.executable, '-u', str(ROOT / script)], cwd=ROOT)
        if result.returncode:
            return result.returncode if result.returncode > 0 else 1
    print(f'Passed {len(scripts)} emulator suites.', flush=True)
    return 0


def stock_experiment(args):
    from stock import startup, scheduler, boot, home, feedback

    inputs = dict(firmware=args.firmware)
    if args.command in ('boot', 'home'):
        inputs['eeprom'] = args.eeprom
    check = None
    if args.command == 'startup':
        if args.matrix:
            return startup.experiment(args.firmware), None
        emu = startup.Startup(scanner_complete=args.stall_transfer is None,
                              stall_transfer=args.stall_transfer or 1,
                              response=args.response, **inputs)
        result = emu.run(args.budget)
        check = (lambda: startup.verify_stalled(result, args.stall_transfer)) if args.stall_transfer else (
            lambda: startup.verify_reached_panel(result))
    elif args.command == 'scheduler':
        emu = scheduler.TickScheduler(**inputs).boot()
        result = emu.exercise(ticks=args.ticks, budget=args.budget,
                              control=args.no_irq, spurious=args.spurious)
        check = lambda: scheduler.verify(result, args.ticks, args.no_irq, args.spurious)
    elif args.command == 'boot':
        emu = boot.BootTrace(ready_level=args.ready_level, **inputs).boot()
        result = emu.run_boot(args.ticks, args.budget)
        check = lambda: boot.verify(result)
    elif args.feedback == 'backoff':
        emu = feedback.Recheck(completion=not args.no_completion, **inputs).boot()
        result = emu.run_home(args.ticks, args.budget)
        feedback.describe(emu, result)
        check = lambda: feedback.verify(emu, result)
    else:
        emu = home.HomeCapture(mode=args.mode, timer4=not args.no_timer4,
                               serial_irq=not args.no_serial_irq, **inputs).boot()
        result = emu.run_home(args.ticks, args.budget)
        check = lambda: home.verify(result)
    result['firmware_sha256'] = hashlib.sha256(emu.image).hexdigest()
    return result, check


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not __debug__:
        p.error('Do not run with -O or PYTHONOPTIMIZE: model assertions must stay enabled')
    if args.command == 'startup' and args.matrix and (
            args.stall_transfer is not None or args.response != 0x40 or args.budget != 3_000_000):
        p.error('--matrix uses fixed responses, stalled transfers and instruction budgets')
    if args.command == 'home':
        if args.feedback == 'backoff' and (args.no_timer4 or args.no_serial_irq or args.mode != 0x0200000f):
            p.error('backoff feedback requires timer4, serial IRQ and mode 0x0200000f')
        if args.no_completion and args.feedback != 'backoff':
            p.error('--no-completion requires --feedback backoff')
    try:
        if args.command == 'test':
            return replacement_tests(args.suite)
        output = args.output.resolve()
        for path in (args.firmware, getattr(args, 'eeprom', None)):
            if path is not None and (output == path.resolve() or
                    (output.exists() and path.exists() and output.samefile(path))):
                p.error('--output must not overwrite an input image')
        result, check = stock_experiment(args)
        state = result.get('result', result)
        problem = state.get('error')
        if result.get('faults') or result.get('flash_writes') or state.get('firmware_bytes_unchanged') is False:
            problem = problem or 'Firmware fault or unexpected flash mutation'
        if not all(result.get('input_files_unchanged', {}).values()):
            problem = problem or 'Input file changed during execution'
        if args.verify and check is not None and not problem:
            try:
                check()
                result['scenario_assertions_passed'] = True
            except AssertionError as exc:
                problem = f'Scenario assertion failed: {exc}'
                result['scenario_assertions_passed'] = False
        result['run'] = {'command': args.command, 'validation_error': problem,
                         'scenario_verification_requested': args.verify or getattr(args, 'matrix', False)}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'report': str(output), 'stop': state.get('stop_reason'),
                          'error': problem, 'firmware_sha256': result.get('firmware_sha256')}, indent=2))
        return 1 if problem else 0
    except (OSError, ValueError, AssertionError) as exc:
        print(f'Emulator error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
