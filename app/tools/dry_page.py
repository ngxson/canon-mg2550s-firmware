#!/usr/bin/env python3
"""Dry-run page: feed a sheet, sweep the carriage per band, advance, eject (USB 0x71/0x72).

No head firing. Reports per-band progress and total/sweep time.
"""
import argparse, json, sys, time, struct
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device  # noqa: E402
STATES = {0: 'idle', 1: 'home_cr', 2: 'feed', 3: 'sweep', 4: 'advance', 5: 'eject', 6: 'park'}
RESULTS = {0: 'none', 1: 'running', 2: 'ok', 3: 'feed_failed', 4: 'carriage_failed', 5: 'cancelled'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bands', type=int, default=4)
    p.add_argument('--advance', type=int, default=0, help='LF counts per band (0 = default 2000)')
    p.add_argument('--speed', type=int, default=0, help='carriage cruise counts/s, multiple of 1000 (0 = default 40000)')
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
        w = struct.unpack('<10I', bytes(dev.ctrl_transfer(0xc0, 0x72, 0, 0, 40, timeout=1000)))
        assert w[0] == 0x4a50474d, [hex(x) for x in w]
        return dict(state=STATES.get(w[2], w[2]), result=RESULTS.get(w[3], w[3]), band=w[4], bands=w[5], cr_x=w[6] if w[6] < 1 << 31 else w[6] - (1 << 32), ms=w[7], sweep_ms=w[8], advance=w[9])
    sent = False
    try:
        drain()
        dev.ctrl_transfer(0x40, 0x71, (args.bands & 255) | ((args.speed // 1000) << 8), args.advance // 64, b'', timeout=1000); sent = True
        t0 = time.monotonic(); last = None
        while True:
            s = status(); s['t'] = round(time.monotonic() - t0, 2); samples.append(s)
            key = (s['state'], s['result'], s['band'])
            if key != last: print('STATUS', json.dumps(s), flush=True); last = key
            drain()
            if s['state'] == 'idle' and s['result'] != 'running': break
            if time.monotonic() - t0 > 180: raise TimeoutError('host deadline')
            time.sleep(0.1)
        print('\n'.join(l for l in log.decode(errors='replace').splitlines() if l.startswith('[page]')))
        print('FINAL', json.dumps(samples[-1]))
        if args.output:
            args.output.write_text(json.dumps({'management': info, 'args': vars(args) | {'output': str(args.output)}, 'samples': samples,
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
