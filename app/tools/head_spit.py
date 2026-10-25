#!/usr/bin/env python3
"""Stationary black prefire ("spit") test through the open app (USB 0x81 / 0x82).

  head_spit.py --position 6000 --dry-run      # every register write, no arm; check read-backs
  head_spit.py --position 6000                # stock prefire list: 2 x 100 pulses, black lanes
  head_spit.py --position 6000 --count 15 --ambient 44 --output app/evidence/head_fire/run1.json

Sequence: read the cartridge fuse ROM (the firmware refuses unless the ranks
are the ones its waveform tables were computed for), read the head thermistor
ADC, home the carriage and move it to --position (counts from the park wall,
left positive), run the prefire, drain the firmware log, park again. The
firmware samples the ADC before and after on its own. Physically fires the
black head unless --dry-run is given. Ctrl-C sends the USB stop request to the
carriage; the prefire itself is bounded in firmware (1 s per record).
"""
import argparse, json, struct, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device  # noqa: E402

RESULTS = {0: 'none', 1: 'running', 2: 'ok', 3: 'refused_rank', 4: 'refused_carriage', 5: 'timeout', 6: 'dry_ok', 7: 'busy', 8: 'bad_parameter'}
CR_RESULTS = {0: 'none', 1: 'running', 2: 'ok', 3: 'stall', 4: 'timeout', 5: 'cancelled', 6: 'limit', 7: 'refused'}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--position', type=int, required=True, help='carriage position for the spit, counts from the park wall (left positive)')
    p.add_argument('--count', type=int, default=0, help='pulses per record, 15..100 (0 = stock 100)')
    p.add_argument('--ambient', type=int, default=0, help='ambient temperature in half-degrees C for the stock waveform selector (0 = 50, i.e. 25 C)')
    p.add_argument('--dry-run', action='store_true', help='configuration writes only, never arm the engine')
    p.add_argument('--record', type=int, choices=(1, 2), help='run only this stock record (default both)')
    p.add_argument('--colour', action='store_true', help='stock colour prefire list ID 0x25 (lanes 2..7, 600 pulses per record) instead of the black list')
    p.add_argument('--no-park', action='store_true', help='leave the carriage at --position afterwards')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    if args.count and not 15 <= args.count <= (600 if args.colour else 100): p.error('--count out of range')
    if args.colour and args.count % 6: p.error('--count must be a multiple of 6 for the colour list')
    import usb.util
    dev, info = wait_device('application')
    log = bytearray(); report = {'management': info, 'args': vars(args) | {'output': str(args.output)}}
    s32 = lambda v: v - (1 << 32) if v >= 1 << 31 else v

    def drain():
        for _ in range(16):
            d = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
            if not d: break
            log.extend(d)

    def cr_status():
        w = struct.unpack('<12I', bytes(dev.ctrl_transfer(0xc0, 0x6e, 0, 0, 48, timeout=1000)))
        return dict(state=w[2], result=CR_RESULTS.get(w[3], w[3]), x=s32(w[4]), referenced=bool(w[10] & 1))

    def cr_wait(what):
        t0 = time.monotonic()
        while True:
            s = cr_status(); drain()
            if s['state'] == 0 and s['result'] != 'running':
                print(what, json.dumps(s)); assert s['result'] == 'ok', (what, s); return s
            if time.monotonic() - t0 > 15: raise TimeoutError(what)
            time.sleep(0.05)

    def hf_status():
        w = struct.unpack('<20I', bytes(dev.ctrl_transfer(0xc0, 0x82, 0, 0, 80, timeout=1000)))
        assert w[0] == 0x4648474d, [hex(x) for x in w[:2]]
        return dict(state=w[2], result=RESULTS.get(w[3], w[3]), count=w[4], records_done=w[5], mask=w[6], ambient=w[7],
                    bk_selector=w[8], cl_selector=w[9], adc_before={'black': w[10], 'colour': w[11]},
                    adc_after={'black': w[12], 'colour': w[13]}, elapsed_ms=w[14], x=s32(w[15]), flags=hex(w[16]),
                    sequence=w[17], status_end=hex(w[18]), wait_ms=w[19])

    moved = False
    try:
        drain()
        # 1) cartridge ranks (keep the interface prepared for the prefire)
        before = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
        dev.ctrl_transfer(0x40, 0x62, 3 | 0x200, 0, b'', timeout=1000)
        t0 = time.monotonic()
        while True:
            s = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0x63, 0, 0, 64, timeout=1000)))
            if s[2] == 2 and s[5] != before[5]: break
            if time.monotonic() - t0 > 15: raise TimeoutError('cartridge read')
            drain(); time.sleep(0.05)
        bk, cl = s[8], s[12]
        ranks = {'black_rank1': (bk >> 11) & 63, 'colour_rank1': (cl >> 11) & 31, 'colour_rank2': (cl >> 16) & 15,
                 'black_stable': s[8] == s[9], 'colour_stable': s[12] == s[13], 'connection': hex(s[6])}
        report['cartridge'] = ranks; print('cartridge', json.dumps(ranks))
        if (ranks['black_rank1'], ranks['colour_rank1'], ranks['colour_rank2']) != (37, 16, 7):
            print('ranks differ from the embedded waveform tables (37 / 16,7); the firmware will refuse', file=sys.stderr)
        # 2) thermistor
        w = struct.unpack('<36I', bytes(dev.ctrl_transfer(0xc0, 0x80, 0, 0, 144, timeout=2000)))
        report['adc_host'] = {'black': w[2], 'colour': w[3]}; print('adc', report['adc_host'])
        # 3) carriage
        if not cr_status()['referenced']:
            dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); cr_wait('home')
        dev.ctrl_transfer(0x40, 0x6d, args.position // 64, 0, b'', timeout=1000); moved = True
        report['carriage_at'] = cr_wait('move')
        # 4) prefire
        value = ((args.count // 6 if args.colour else args.count) & 0xff) | (0x100 if args.dry_run else 0) | {None: 0, 1: 0x400, 2: 0x800}[args.record] | (0x1000 if args.colour else 0)
        seq = hf_status()['sequence']
        print('prefire', 'DRY RUN' if args.dry_run else 'FIRING', 'colour' if args.colour else 'black', 'count', args.count or (600 if args.colour else 100), 'ambient', args.ambient or 50, flush=True)
        dev.ctrl_transfer(0x40, 0x81, value, args.ambient, b'', timeout=1000)
        t0 = time.monotonic()
        while True:
            s = hf_status(); drain()
            if s['state'] == 2 and s['sequence'] != seq: break
            if time.monotonic() - t0 > 20: raise TimeoutError('prefire')
            time.sleep(0.05)
        s['host_seconds'] = round(time.monotonic() - t0, 2)
        report['prefire'] = s; print('PREFIRE', json.dumps(s), flush=True)
        # 5) park
        if not args.no_park:
            dev.ctrl_transfer(0x40, 0x6c, 0, 0, b'', timeout=1000); report['parked'] = cr_wait('park')
        drain()
    except KeyboardInterrupt:
        print('cancelling', file=sys.stderr)
    finally:
        try:
            if moved and cr_status()['state'] != 0: dev.ctrl_transfer(0x40, 0x76, 0, 0, b'', timeout=1000)
        except Exception as exc: print('stop attempt:', exc, file=sys.stderr)
        try: drain()
        except Exception: pass
        report['firmware_log'] = log.decode(errors='replace')
        print('\n'.join(l for l in report['firmware_log'].splitlines() if l.startswith(('[head]', '[cart]', '[cr]'))))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + '\n'); print('saved', args.output)
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
