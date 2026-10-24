#!/usr/bin/env python3
"""Emulate the app's scanner capture end-to-end with the scanner-engine model.

Runs scanner_full_capture (USB 0x59) in emulation on top of the real installed
bootloader, with the modeled LDMAC DMAing a synthetic image into cap_buf, then
checks the firmware captured exactly that image. Validates the capture plumbing
(SCNIO config + LDMAC descriptors + request + completion + buffer read-back)
off-hardware.
"""
import json
import struct
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests'))
sys.path.insert(0, str(APP / 'tests'))
from model import setup                 # noqa: E402
from model_scanner import ScannerModel  # noqa: E402

LINE_U16 = 0xc00                          # ch0 segment (0x600) + ch1 segment (0x600)


def app_mode(e):
    return bool(e.info()) and e.info()[2] == 2


def main():
    boot_region = (BOOT / 'build/boot_region.bin').read_bytes()
    app = (APP / 'build/application.bin').read_bytes()
    assert len(boot_region) == 0x10000
    factory = (boot_region + app).ljust(0x200000, b'\xff')

    boot_syms = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    app_syms = json.loads((APP / 'build/manifest.json').read_text())['symbols']['application']
    assert 'cap_buf' in app_syms, 'cap_buf symbol missing from app manifest'
    cap_buf = int(app_syms['cap_buf'], 16)
    metadata = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
                'symbols': {'bootloader': boot_syms['bootloader'], 'application': app_syms}}

    image = lambda i: (0x6400 + (i & 0xff)) & 0xffff   # synthetic per-pixel pattern

    e = ScannerModel(image=factory, metadata=metadata, scan_image=image)
    e.run(app_mode)
    assert 0x20020000 in e.entered

    # variant 0x04 = scanner power/lamp on, no AFE serial (keeps the emu fast)
    e.requests.appendleft(('scan', setup(0x40, 0x59, 0x04)))
    e.run(lambda x: x.scan_captures >= 1, resume=True, budget=120000000)
    assert e.scan_captures >= 1, 'capture never triggered the modeled DMA'

    data = struct.unpack('<%dH' % LINE_U16, bytes(e.uc.mem_read(cap_buf, LINE_U16 * 2)))
    for i in range(LINE_U16):
        assert data[i] == image(i), f'sample {i}: 0x{data[i]:04x} != 0x{image(i):04x}'
    print(f'PASS scanner_capture_emulated: {LINE_U16} samples match the synthetic image')
    print('ALL SCANNER EMU ASSERTIONS PASSED')


if __name__ == '__main__':
    main()
