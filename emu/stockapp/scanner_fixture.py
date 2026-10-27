#!/usr/bin/env python3
"""Offline stock scanner integration fixture. No printer access.

Real profile/descriptor/AFE/timing code; synthetic IRQ delivery and a small
RTOS event adapter. Pixel DMA and optics are deliberately absent. A stock
return therefore does NOT establish that the buffer contains an image.
"""
import argparse
import json
import struct
from pathlib import Path

from fixture import Fixture, SENTINEL
from unicorn import UcError
from unicorn.arm_const import UC_ARM_REG_PC as PC, UC_ARM_REG_LR as LR, UC_ARM_REG_R0 as R0

K, INPUT, EXPOSURE, AFE = 0x10080000, 0x10081000, 0x10082000, 0x10083000
BUFFER, CAPACITY = 0x18200000, 0x80000  # fixture-only reservation, NOT a hybrid address recommendation


class ScannerFixture(Fixture):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.scan_events = {}
        self.waiting = None
        self.timeline = []
        self.callback_counts = {}
        self.stub = True
        self.heating_model = False

    def stub_return(self, cpu, name, values):
        if name == 'event_set':
            self.scan_events[values[0]] = self.scan_events.get(values[0], 0) | values[1]
        elif name == 'event_clear':
            self.scan_events[values[0]] = self.scan_events.get(values[0], 0) & values[1]
        elif name == 'event_wait':
            group, mask, mode, out, timeout = values[:5]
            have = self.scan_events.get(group, 0) & mask
            if not (have == mask if mode == 0 else bool(have)):
                self.waiting = (group, mask, mode, out, timeout)
                self.stop_reason = 'waiting for modeled event'
                cpu.emu_stop()
                return
            cpu.mem_write(out, struct.pack('<I', have))
        elif name not in ('irq_disable', 'irq_restore', 'irq_restore_b', 'task_delay'):
            self.stop_reason = 'unsupported scanner service: ' + name
            cpu.emu_stop()
            return
        cpu.reg_write(R0, 0xc0 if name == 'irq_disable' else 0)
        cpu.reg_write(PC, cpu.reg_read(LR))

    def execute(self, address, args=(), *, missing=None, budget=2_000_000):
        """Resume only events posted by executed stock handlers, never fabricate success."""
        start = len(self.mmio_writes)
        self.waiting = None
        result = self.call(address, list(args), budget=budget, cpsr=0xdf)
        services = list(result['services'])
        callbacks = []
        for _ in range(100):
            if not self.waiting:
                break
            wait = self.waiting
            group, mask, mode, out, timeout = wait
            source = {(0x24, 0x10000): 0x18, (0x29, 0xffffffff): 0x1d, (0x24, 1): 0x19}.get((group, mask))
            self.timeline.append({'wait': [hex(group), hex(mask)], 'source': source,
                                  'caller': hex(self.uc.reg_read(LR))})
            if source is None or source == missing:
                result['stop_reason'] = 'withheld interrupt' if source is not None and source == missing else 'unsupported event wait'
                break
            context = self.uc.context_save()
            stack = bytes(self.uc.mem_read(0x100fd000, 0x2000))
            handler = {0x18: 0xf5494, 0x19: 0xf54a6, 0x1d: 0xf54bc}[source]
            for n in range(128):
                # Explicit serial completion input; inherited serial controller also requires 3 polls.
                if source == 0x1d and self.pending is not None:
                    self.pending['polls'] = max(3, self.pending['polls'])
                self.waiting = None
                cb = self.call(handler, [], budget=100_000, cpsr=0xdf)
                if not cb['returned'] or cb['error']:
                    raise RuntimeError(('handler failed', source, cb['stop_reason'], cb['pc']))
                services.extend(cb['services'])
                self.callback_counts[source] = self.callback_counts.get(source, 0) + 1
                callbacks.append(source)
                if self.scan_events.get(group, 0) & mask:
                    break
            else:
                raise RuntimeError(('handler failed to post', source, group, mask))
            self.uc.mem_write(0x100fd000, stack)
            self.uc.context_restore(context)
            have = self.scan_events.get(group, 0) & mask
            self.uc.mem_write(out, struct.pack('<I', have))
            self.uc.reg_write(R0, 0)
            target = self.uc.reg_read(LR)
            self.uc.reg_write(PC, target)
            self.waiting = None
            self.service_log = []
            self.stop_reason = self.error = None
            self.in_call = True
            try:
                self.uc.emu_start(target, SENTINEL, count=budget)
            except UcError as exc:
                self.error = str(exc)
            self.in_call = False
            services.extend(self.service_log)
            result = {'returned': self.uc.reg_read(PC) == SENTINEL,
                      'r0': self.uc.reg_read(R0), 'pc': hex(self.uc.reg_read(PC)),
                      'error': self.error, 'stop_reason': self.stop_reason}
        writes = self.mmio_writes[start:]
        return {k: result.get(k) for k in ('returned', 'r0', 'pc', 'error', 'stop_reason')} | {
            'call': hex(address), 'args': [hex(x) for x in args],
            'services': [{'name': x['service'], 'args': x['args'], 'lr': x['lr']} for x in services],
            'callback_sources': callbacks, 'writes': writes,
            'waiting': list(self.waiting) if self.waiting else None}


def word(cpu, address):
    return struct.unpack('<I', cpu.mem_read(address, 4))[0]


def setup_case(fx, profile, width, components, lines, *, power=True, missing=None):
    fx.restore()
    fx.scan_events, fx.timeline, fx.callback_counts = {}, [], {}
    fx.pending = None
    fx.serial = []
    fx.uc.mem_write(BUFFER, b'\xa5' * CAPACITY)
    setup = []
    for address in (0xffb46, 0xffb1c):
        setup.append(fx.execute(address))
    if power:
        setup.append(fx.execute(0xfa234, missing=missing))
        if not setup[-1]['returned']:
            return setup, []
    fx.uc.mem_write(K, bytes(0x200))
    # Explicit synthetic request, based on fda52's finite request, with profile,
    # width, line count and component count varied through fd7d6's real ABI.
    request = [BUFFER, profile, 4, 2, 1, 0, width, lines, 0, 3000, 3000,
               components, 0, 0, 0, 0, 0, 0, 0]
    fx.uc.mem_write(INPUT, struct.pack('<19I', *request))
    setup.append(fx.execute(0xfd7d6, (0x180dc90c, K, INPUT)))
    setup.append(fx.execute(0xfb93c, (profile, EXPOSURE)))
    setup.append(fx.execute(0xfba68, (AFE, profile)))
    return setup, request


def ranges(pages, shift):
    runs = []
    for page in sorted(pages):
        if runs and runs[-1][1] == page:
            runs[-1][1] = page + 1
        else:
            runs.append([page, page + 1])
    return [[hex(a << shift), hex(b << shift)] for a, b in runs]


def values(result, address):
    return [int(w['value'], 16) for w in result['writes']
            if int(w['address'], 16) == address]


def capture_summary(result):
    return {
        'returned': result['returned'], 'error': result['error'],
        'irq18': result['callback_sources'].count(0x18),
        'irq19': result['callback_sources'].count(0x19),
        'serial_irqs': result['callback_sources'].count(0x1d),
        'requests': sum(bool(x) for x in values(result, 0xc0d6000c)),
        'request_masks': [hex(x) for x in values(result, 0xc0d6000c) if x],
        'afe_tx': [hex(x) for x in values(result, 0xc0120118)],
        'waiting': result['waiting'],
    }


def verify_case(case):
    problems = []

    def check(ok, why):
        if not ok:
            problems.append(why)

    name = case['name']
    result = case['result']
    check(not result['error'], 'CPU execution error')
    check(case['firmware_unchanged'], 'stock flash changed')
    check(case['buffer_unchanged'], 'fixture unexpectedly wrote pixel data')
    if name in ('missing-irq18', 'missing-irq19', 'missing-serial'):
        expected = {'missing-irq18': [0x24, 0x10000],
                    'missing-irq19': [0x24, 1], 'missing-serial': [0x29, 0xffffffff]}[name]
        check(not result['returned'], 'withheld source was treated as success')
        check(result['waiting'] and result['waiting'][:2] == expected, 'wrong blocked event')
        check(result['stop_reason'] == 'withheld interrupt', 'unexpected stop')
        check(case['summary']['requests'] == 0, 'request issued before required event')
        return problems
    check(all(x['returned'] and not x['error'] for x in case['setup']), 'setup did not return')
    for index, capture in enumerate([result] + ([case['repeat']] if case.get('repeat') else [])):
        stats = capture_summary(capture)
        check(capture['returned'] and not capture['error'], 'finite capture did not return')
        check(stats['irq18'] == 2, 'IRQ18 synchronization changed')
        check(stats['irq19'] == case['lines'] + 4, 'finite timing schedule changed')
        check(stats['requests'] == case['lines'], 'wrong number of requests')
        check(stats['serial_irqs'] == (0 if name == 'missing-power' or index else 9), 'AFE setup gate changed')
    if case.get('retuned'):
        tuned = capture_summary(case['retuned'])
        check(case['afe_rebuild']['returned'], 'AFE rebuild failed')
        check(tuned['returned'] and not tuned['error'] and tuned['serial_irqs'] == 9,
              'rebuilt AFE was not applied')
        check('0x2005' in tuned['afe_tx'] and '0x5005' in tuned['afe_tx'],
              'gain change did not reach both AFE segment registers')
        check(tuned['requests'] == case['lines'] and tuned['irq19'] == case['lines'] + 4,
              'retuned capture timing changed')
    if name == 'missing-power':
        check(case['power_before_capture'] == 0, 'negative control unexpectedly powered up')
        check(not case['summary']['afe_tx'], 'AFE wrote with power gate off')
    else:
        check(case['power_before_capture'] == 2, 'stock power-on state missing')
        check(case['power_after_off'] == 0, 'stock power-off state not cleared')
        check(case['power_off']['returned'] and not case['power_off']['error'], 'power-off failed')
        check(values(case['power_off'], 0xc0c85614) == [0], 'power rail not disabled')
    check(case['cleanup']['request'] == 0, 'line request left enabled')
    check(all(x == 0 for x in case['cleanup']['dma_enable']), 'DMA left enabled')
    check(case['cleanup']['scnio_control'] & 0x80000000, 'SCNIO stop absent')
    width, count, components = case['width'], case['lines'], case['components']
    for ch, desc in enumerate(case['descriptors'][:2]):
        check(desc[2] == BUFFER + (components - 1)*count*2*width + ch*width, 'unexpected DMA initial pointer')
        check(desc[3] == BUFFER and desc[4] == BUFFER + (components*count + 1)*2*width,
              'unexpected DMA window')
        check(desc[5] == width and desc[7] == (components - 1)*count*2*width + width,
              'unexpected DMA skips')
        check(desc[10] == width and desc[14:16] == [components*count, 1], 'unexpected finite dimensions')
    check(all(d[0] == 0 for d in case['descriptors'][2:]), 'unexpected extra DMA channels')
    return problems


def run_case(fx, name, profile, width, components, lines, missing=None):
    setup, request = setup_case(fx, profile, width, components, lines,
                               power=name != 'missing-power', missing=missing)
    config = bytes(fx.uc.mem_read(K, 0x154)) if request else b''
    descriptors = [list(struct.unpack_from('<16I', config, 0x54 + 0x40*i)) for i in range(4)] if config else []
    power = fx.uc.mem_read(0x180d9508, 1)[0]
    result = fx.execute(0xfb896, (K, EXPOSURE, AFE, 1, 4, 1), missing=missing) if request else setup[-1]
    case = {'name': name, 'profile': profile, 'width': width, 'components': components, 'lines': lines,
            'request_words': request, 'setup': setup, 'config_hex': config.hex(),
            'descriptors': descriptors, 'result': result, 'power_before_capture': power,
            'summary': capture_summary(result)}
    # Re-arm on the SAME scanner state, without restore/clearing events: catch
    # stale event success and one-shot-only initialization.
    if name == 'p0-gray-4':
        case['repeat'] = fx.execute(0xfb896, (K, EXPOSURE, AFE, 1, 4, 1))
        # fd044 rebuilds A and clears its already-applied byte. Verify a real
        # gain change reaches both AFE segments on the next acquisition.
        case['afe_rebuild'] = fx.execute(0xfba68, (AFE, profile))
        fx.uc.mem_write(AFE + 4, struct.pack('<I', 5))
        case['retuned'] = fx.execute(0xfb896, (K, EXPOSURE, AFE, 1, 4, 1))
    case['cleanup'] = {'request': fx.registers.get(0xc0d6000c, 0),
        'scnio_control': fx.registers.get(0xc0d60008, 0),
        'dma_enable': [fx.registers.get(0xc0d50008 + 0x100*i, 0) for i in range(4)]}
    if result['returned'] and power:
        case['power_off'] = fx.execute(0xfa2d0)
    case['power_after_off'] = fx.uc.mem_read(0x180d9508, 1)[0]
    case['timeline'] = list(fx.timeline)
    case['serial_transfers'] = list(fx.serial)
    case['buffer_unchanged'] = bytes(fx.uc.mem_read(BUFFER, CAPACITY)) == b'\xa5'*CAPACITY
    case['firmware_unchanged'] = bytes(fx.uc.mem_read(0, len(fx.image))) == fx.image
    case['problems'] = verify_case(case)
    return case


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--eeprom', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True, help='JSON trace; verification always runs')
    args = ap.parse_args()
    if args.output.suffix != '.json' or args.output.resolve() == args.eeprom.resolve():
        ap.error('output must be a .json path distinct from the EEPROM input')
    fx = ScannerFixture(ready_level=4, eeprom=args.eeprom)
    fx.stub = False
    fx.boot()
    boot = fx.run_boot(ticks=300, budget=12_000_000)
    if boot['result']['error'] or boot['faults']:
        raise RuntimeError('stock boot failed')
    fx.snapshot()
    fx.stub = True
    report = {'scope': __doc__, 'boot': {k: boot[k] for k in ('firmware_sha256', 'eeprom_sha256', 'ticks')},
              'assumptions': ['Inherited BootTrace readiness and serial response 0x40 (type 0)',
                'Direct Thumb calls, IRQ/FIQ masked; no scheduler during calls',
                'RTOS event set/clear/wait implemented by fixture; delay returns immediately',
                'IRQ18/19/1d stock handler calls supplied explicitly, not elapsed physical time',
                'No optical, DMA, encoder, or motor completion model',
                'Restored full boot RAM, NOT the current hybrid sparse snapshot'], 'cases': []}
    # These fixture buffers are intentionally outside the stock scanner state.
    # setup_case fills the canary before power-on, including blocked cases.
    for spec in [('p7-diagnostic', 7, 3072, 1, 1), ('p0-gray-1', 0, 2592, 1, 1),
            ('p0-gray-2', 0, 2592, 1, 2), ('p0-gray-4', 0, 2592, 1, 4),
            ('p0-three-components', 0, 2592, 3, 4), ('p6-three-components', 6, 5184, 3, 4),
            ('missing-irq18', 0, 2592, 1, 4, 0x18), ('missing-irq19', 0, 2592, 1, 4, 0x19),
            ('missing-serial', 0, 2592, 1, 4, 0x1d), ('missing-power', 0, 2592, 1, 4)]:
        case = run_case(fx, *spec)
        report['cases'].append(case)
        print(case['name'], case['summary'], 'problems:', case['problems'], flush=True)
    setup_case(fx, 0, 2592, 1, 1, power=False)
    report['profiles'] = []
    for index in range(9):
        address = 0x180dd7f4 + index*0x2c
        raw = bytes(fx.uc.mem_read(address, 0x2c))
        report['profiles'].append({'profile': index, 'words': list(struct.unpack('<11I', raw)),
            'width': word(fx.uc, address + 0x14),
            'subdivision': struct.unpack_from('<H', raw, 0x18)[0],
            'segments': struct.unpack_from('<H', raw, 0x1a)[0]})
    # Execute the gain computation with synthetic statistics, without claiming
    # to run the optical calibration loop which normally produces them.
    report['gain_cases'] = []
    for high in (10000, 14745, 16000, 20000, 30000):
        result = fx.execute(0xfe4d0, (0, 0, high, 0))
        report['gain_cases'].append({'high': high, 'low': 0,
            'target': word(fx.uc, 0x180dca80), 'minimum_gain': word(fx.uc, 0x180dca84),
            'gain': result['r0'], 'result': result})
    report['ram_pages_touched_4k'] = ranges(fx.ram_pages, 12)
    report['flash_coverage'] = {'page_bytes': 256,
        'call_code_ranges': ranges(fx.flash_code_pages, 8),
        'call_data_ranges': ranges(fx.flash_data_pages, 8)}
    gains = report['gain_cases']
    report['gain_verified'] = (all(g['result']['returned'] and not g['result']['error'] for g in gains)
                              and [g['gain'] for g in gains] == [13, 13, 10, 4, 3])
    report['verified'] = all(not c['problems'] for c in report['cases']) and report['gain_verified']
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print('gain cases:', [(g['high'], g['target'], g['gain']) for g in gains])
    print('PASS' if report['verified'] else 'FAIL', args.output)
    if not report['verified']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
