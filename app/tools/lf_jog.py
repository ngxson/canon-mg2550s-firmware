#!/usr/bin/env python3
"""One bounded paper-feed (LF) motor pulse through USB 0x67, result from 0x68.

Physically drives the paper feed motor. Command is the raw drive register value
(2000 = stock neutral; stock low feed commands are 1880 and 2120). Duration is
capped at 2000 ms by the firmware. Polarity is not known until observed.
"""
import argparse, json, struct, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device, get_info  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--command', type=int, required=True, help='raw drive value 1400..2600, 2000 = neutral')
    p.add_argument('--ms', type=int, required=True, help='pulse duration 1..2000 ms')
    p.add_argument('--channel', type=int, default=0, choices=(0, 1, 2), help='0 = paper feed (0xc0c52000), 1 = absent block (0xc0c54000), 2 = print carriage (0xc0c50000, max 1000 ms)')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    import usb.util
    dev, info = wait_device('application')
    log = bytearray()
    try:
        def drain():
            for _ in range(16):
                d = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
                if not d:
                    break
                log.extend(d)
        drain()
        value = args.command | (args.channel << 12)
        dev.ctrl_transfer(0x40, 0x67, value, args.ms, b'', timeout=1000)
        deadline = time.monotonic() + 8
        while True:
            raw = bytes(dev.ctrl_transfer(0xc0, 0x68, 0, 0, 160, timeout=1000))
            w = struct.unpack('<%dI' % (len(raw) // 4), raw)
            assert w[0] == 0x4a464c4d, [hex(x) for x in w]
            if len(w) > 2 and w[2] == 1 and w[3] == value:
                break
            if time.monotonic() > deadline:
                raise TimeoutError('no jog result')
            time.sleep(0.05)
        drain()
        samples = list(w[8:8 + w[7]])
        result = {'management': get_info(dev), 'channel': args.channel, 'command': w[3] & 0xfff, 'measured_ms': w[4], 'start': w[5], 'end': w[6],
                  'delta': ((w[6] - w[5] + (1 << 31)) & 0xffffffff) - (1 << 31), 'samples': samples,
                  'firmware_log': log.decode(errors='replace')}
        print('\n'.join(l for l in result['firmware_log'].splitlines() if l.startswith('[lf]')))
        print(f"channel={args.channel} command={w[3] & 0xfff} ms={w[4]} position {w[5]:#x} -> {w[6]:#x} delta {result['delta']:+d}")
        print('samples:', ' '.join(f'{s:#x}' for s in samples))
        if args.output:
            args.output.write_text(json.dumps(result, indent=2) + '\n')
    finally:
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
