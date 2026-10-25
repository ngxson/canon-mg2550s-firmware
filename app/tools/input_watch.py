#!/usr/bin/env python3
"""Watch the panel and GPIO input registers through the register tunnel and report changes.

  input_watch.py --seconds 20

Snapshots the panel block, the GPIO alias/sense banks and the sensor words the
stock firmware polls, several times per second, and prints every word that
changes with its time, so a key press or a sensor change can be located
without knowing the bit beforehand. Read-only (USB 0x90/0x91).
"""
import argparse, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from fwctl import wait_device  # noqa: E402

BLOCKS = [(0xc0c8d000, 0x100), (0xc0c84600, 0x100), (0xc0c84e00, 0x40), (0xc0c83e00, 0x40),
          (0xc0c85400, 0x40), (0xc0c81000, 0x40), (0xc0c81200, 0x100), (0xc0c840c0, 0x40)]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seconds', type=float, default=20); p.add_argument('--interval', type=float, default=0.15)
    args = p.parse_args()
    import usb.util
    dev, info = wait_device('application')

    def snapshot():
        words = {}
        for base, size in BLOCKS:
            for off in range(0, size, 256):
                n = min(256, size - off)
                dev.ctrl_transfer(0x40, 0x90, (base + off) & 0xffff, (base + off) >> 16, b'', timeout=1000)
                data = bytes(dev.ctrl_transfer(0xc0, 0x91, 0, 0, n, timeout=1000))
                for i in range(0, len(data), 4): words[base + off + i] = struct.unpack_from('<I', data, i)[0]
        return words
    try:
        base = snapshot(); print('baseline words:', len(base), 'nonzero:', {hex(a): hex(v) for a, v in base.items() if v}, flush=True)
        t0 = time.monotonic(); last = base; changes = 0
        while time.monotonic() - t0 < args.seconds:
            cur = snapshot()
            for a in cur:
                if cur[a] != last[a]:
                    changes += 1; print(f'{time.monotonic() - t0:6.2f}s  {a:#010x}: {last[a]:#010x} -> {cur[a]:#010x}', flush=True)
            last = cur; time.sleep(args.interval)
        print('done,', changes, 'changes')
    finally:
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
