#!/usr/bin/env python3
"""Take a read-only GPIO snapshot (USB 0x66) or diff two saved snapshots.

  gpio_snapshot.py --output paper_in.json
  gpio_snapshot.py --output paper_out.json
  gpio_snapshot.py --diff paper_in.json paper_out.json

Words 0..575 are alias read slots 0xc0c84008 + group*0x200 + slot*0x10;
words 576..895 are bank words 0xc0c81200 + i*4. Several samples are taken and
only bits that are stable within each snapshot are compared, so noisy inputs do
not masquerade as the sensor.
"""
import argparse, json, struct, sys, time
from pathlib import Path
APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))

ALIAS_WORDS, BANK_WORDS = 18 * 32, 320


def address(i):
    if i < ALIAS_WORDS:
        return 0xc0c84008 + (i // 32) * 0x200 + (i % 32) * 0x10
    return 0xc0c81200 + (i - ALIAS_WORDS) * 4


def snapshot(dev):
    words = []
    for chunk in range((ALIAS_WORDS + BANK_WORDS + 63) // 64):
        raw = bytes(dev.ctrl_transfer(0xc0, 0x66, chunk, 0, 256, timeout=1000))
        words += struct.unpack('<%dI' % (len(raw) // 4), raw)
    assert len(words) == ALIAS_WORDS + BANK_WORDS, len(words)
    return words


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--output', type=Path)
    p.add_argument('--samples', type=int, default=5)
    p.add_argument('--diff', nargs=2, type=Path, metavar=('A', 'B'))
    args = p.parse_args()
    if args.diff:
        a, b = (json.loads(f.read_text()) for f in args.diff)
        for i, (wa, wb) in enumerate(zip(a['stable'], b['stable'])):
            mask = a['stable_mask'][i] & b['stable_mask'][i]
            changed = (wa ^ wb) & mask
            if changed:
                print(f'{address(i):#010x}  {wa:#010x} -> {wb:#010x}  changed bits {changed:#x}')
        return
    from fwctl import wait_device
    import usb.util
    dev, info = wait_device('application')
    try:
        samples = [snapshot(dev) for _ in range(args.samples)]
        for _ in range(args.samples - 1):
            time.sleep(0.1)
    finally:
        usb.util.dispose_resources(dev)
    stable_mask = [~0 & 0xffffffff] * len(samples[0])
    for s in samples[1:]:
        for i, (w0, w) in enumerate(zip(samples[0], s)):
            stable_mask[i] &= ~(w0 ^ w) & 0xffffffff
    result = {'management': info, 'stable': samples[0], 'stable_mask': stable_mask, 'samples': samples}
    noisy = [f'{address(i):#010x}' for i, m in enumerate(stable_mask) if m != 0xffffffff]
    print(f'{len(samples)} samples; noisy words: {noisy or "none"}')
    nonzero = [(address(i), w) for i, w in enumerate(samples[0]) if w]
    print(f'{len(nonzero)} non-zero words')
    if args.output:
        args.output.write_text(json.dumps(result) + '\n')


if __name__ == '__main__':
    main()
