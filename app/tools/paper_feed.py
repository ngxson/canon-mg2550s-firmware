#!/usr/bin/env python3
"""Closed-loop paper feed through the open app (USB 0x69 / 0x6a).

Modes: feed (pick a sheet, advance, eject), hold (pick and stop at the advance
position), eject (forward until the paper-end sensor clears, plus a margin).
Physically drives the paper-feed motor. The firmware retries pickup three times
and reports no-paper, timeout or cancel; Ctrl-C sends the USB stop request.
"""
import argparse, json, struct, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device, get_info  # noqa: E402

MODES = {'feed': 0, 'hold': 1, 'eject': 2}
STATES = {0: 'idle', 1: 'pickup', 2: 'pause', 3: 'advance', 4: 'eject', 5: 'margin', 6: 'stop', 7: 'handoff', 8: 'settle', 9: 'reset', 10: 'home'}
RESULTS = {0: 'none', 1: 'running', 2: 'ok', 3: 'no_paper', 4: 'timeout', 5: 'cancelled', 6: 'refused', 7: 'jam'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=sorted(MODES))
    p.add_argument('--advance', type=int, default=0, help='forward counts from the handoff point before stopping/ejecting (0 = firmware default 3762)')
    p.add_argument('--phase1', type=int, default=0, help='reverse counts before the sensor is consulted (0 = firmware default 2048)')
    p.add_argument('--handoff', type=int, default=0, help='extra pickup counts past the sensor edge (0 = firmware default 2048)')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    import usb.util
    dev, info = wait_device('application')
    log = bytearray(); samples = []
    def drain():
        for _ in range(16):
            d = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
            if not d: break
            log.extend(d)
    def status():
        w = struct.unpack('<12I', bytes(dev.ctrl_transfer(0xc0, 0x6a, 0, 0, 48, timeout=1000)))
        assert w[0] == 0x4446504d and w[1] == 1, [hex(x) for x in w]
        return dict(state=STATES.get(w[2], w[2]), result=RESULTS.get(w[3], w[3]), attempts=w[4], pickup=w[5],
                    advance=w[6], eject=w[7], paper=bool(w[8]), position=w[9], holding=bool(w[10] & 1), arm_dirty=bool(w[10] & 2), ms=w[11])
    sent = False
    try:
        drain()
        before = status(); print('before:', json.dumps(before))
        dev.ctrl_transfer(0x40, 0x69, MODES[args.mode] | ((args.phase1 // 256) << 8), (args.advance // 64) | ((args.handoff // 64) << 8), b'', timeout=1000); sent = True
        last = None; t0 = time.monotonic()
        while True:
            s = status(); s['t'] = round(time.monotonic() - t0, 2); samples.append(s)
            if (s['state'], s['result']) != last:
                print('STATUS', json.dumps(s), flush=True); last = (s['state'], s['result'])
            drain()
            if s['state'] == 'idle' and s['result'] != 'running': break
            if time.monotonic() - t0 > 90: raise TimeoutError('host deadline')
            time.sleep(0.05)
        print('\n'.join(l for l in log.decode(errors='replace').splitlines() if l.startswith('[feed]')))
        print('FINAL', json.dumps(samples[-1]))
        if args.output:
            args.output.write_text(json.dumps({'management': get_info(dev), 'mode': args.mode, 'before': before,
                                               'samples': samples, 'firmware_log': log.decode(errors='replace')}, indent=2) + '\n')
    except KeyboardInterrupt:
        print('cancelling', file=sys.stderr)
    finally:
        if sent:
            try:
                if status()['state'] != 'idle': dev.ctrl_transfer(0x40, 0x76, 0, 0, b'', timeout=1000)
            except Exception as exc: print('stop attempt:', exc, file=sys.stderr)
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
