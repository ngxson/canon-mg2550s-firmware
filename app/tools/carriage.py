#!/usr/bin/env python3
"""Home or move the print carriage (USB 0x6c / 0x6d / 0x6e).

  carriage.py home                 # drive right until the park wall, set reference
  carriage.py move 50000           # absolute counts from the park wall, left positive
  carriage.py move 50000 --speed 40000
  carriage.py status

Physically moves the carriage. Soft limits: 0 .. 116736 counts. Ctrl-C sends
the USB stop request.
"""
import argparse, json, struct, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device  # noqa: E402

STATES = {0: 'idle', 1: 'homing', 2: 'moving', 3: 'settle'}
RESULTS = {0: 'none', 1: 'running', 2: 'ok', 3: 'stall', 4: 'timeout', 5: 'cancelled', 6: 'limit', 7: 'refused'}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('action', choices=('home', 'move', 'status', 'tune'))
    p.add_argument('target', type=int, nargs='?', default=0, help='counts from the park wall (move)')
    p.add_argument('--speed', type=int, default=0, help='cruise counts/s (0 = firmware default 30000)')
    p.add_argument('--set', action='append', default=[], metavar='ID=VALUE', help='tune: controller parameter (0 kff_div,1 kp_div,2 ki_div,3 integral_max,4 brake_max,5 floor_speed,6 decel_counts,7 kick,8 kick_ticks,9 window)')
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
        w = struct.unpack('<12I', bytes(dev.ctrl_transfer(0xc0, 0x6e, 0, 0, 48, timeout=1000)))
        assert w[0] == 0x5243474d, [hex(x) for x in w]
        s = lambda v: v - (1 << 32) if v >= 1 << 31 else v
        return dict(state=STATES.get(w[2], w[2]), result=RESULTS.get(w[3], w[3]), x=s(w[4]), reference=w[5],
                    target=s(w[6]), speed=s(w[7]), command=s(w[8]), ms=w[9], referenced=bool(w[10] & 1), raw=w[11])
    sent = False
    try:
        drain()
        if args.action == 'status':
            print(json.dumps(status())); return
        if args.action == 'tune':
            for kv in args.set:
                k, v = kv.split('='); dev.ctrl_transfer(0x40, 0x6f, int(k), int(v), b'', timeout=1000); print('set', k, v)
            return
        before = status(); print('before:', json.dumps(before))
        if args.action == 'home':
            dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000)
        else:
            dev.ctrl_transfer(0x40, 0x6d, args.target // 64, args.speed // 100, b'', timeout=1000)
        sent = True; t0 = time.monotonic(); last = None
        while True:
            s = status(); s['t'] = round(time.monotonic() - t0, 2); samples.append(s)
            if (s['state'], s['result']) != last:
                print('STATUS', json.dumps(s), flush=True); last = (s['state'], s['result'])
            drain()
            if s['state'] == 'idle' and s['result'] != 'running': break
            if time.monotonic() - t0 > 15: raise TimeoutError('host deadline')
            time.sleep(0.05)
        print('\n'.join(l for l in log.decode(errors='replace').splitlines() if l.startswith('[cr]')))
        print('FINAL', json.dumps(samples[-1]))
        if args.output:
            args.output.write_text(json.dumps({'management': info, 'action': args.action, 'target': args.target,
                                               'before': before, 'samples': samples,
                                               'firmware_log': log.decode(errors='replace')}, indent=2) + '\n')
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
