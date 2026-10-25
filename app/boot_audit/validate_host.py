#!/usr/bin/env python3
"""Host-only checks: request bounds and no reboot after a failed app update."""
import json
from pathlib import Path
import struct
import sys
import tempfile

import hardware as h


class Device:
    def __init__(self, product, target, fail=False):
        self.product = product
        self.target = target
        self.mode = 2
        self.status = 0x80
        self.received = bytearray()
        self.requests = []
        self.resets = []
        self.fail = fail
        self.boot = (h.ROOT / 'bootloader/build/vectors/boot_region.bin').read_bytes()

    def ctrl_transfer(self, kind, request, value, index, payload, **kwargs):
        self.requests.append((kind, request, value, index, payload))
        assert request != 0x44, 'Dangerous writer must never be invoked'
        if (kind, request) == (0xc0, 0x42):
            return struct.pack('<8I', 0x5746474d, 2, self.mode, 4 | (9 if self.mode == 1 else 0),
                               0, self.status, 0x10000, 0x1f0000)
        if (kind, request) == (0xc0, 0x95):
            assert index == 0 and 0 <= value <= 0xff00 and payload == 256
            return self.boot[value:value + payload]
        assert kind == 0x40 and request in (0x40, 0x43)
        if request == 0x43:
            self.status = 0x80
            self.expected = value | index << 16
        return 0

    def write(self, endpoint, data, **kwargs):
        assert endpoint == 1
        self.received.extend(data)
        if len(self.received) == self.expected:
            self.status = 0xa3 if self.fail else 0xc0
        return len(data)


def main():
    actual_sleep = h.time.sleep
    actual_wait = h.fwctl.wait_device
    actual_reboot = h.fwctl.request_reboot
    actual_dispose = h.usb.util.dispose_resources
    cases = {}
    try:
        h.time.sleep = lambda _: None
        h.usb.util.dispose_resources = lambda _: None
        for phase, failed in [('install', False), ('read', False), ('restore', False), ('install', True)]:
            name = phase + ('_failed_update' if failed else '')
            initial = 'MG2550s App' if phase == 'install' else 'MG2550s Boot Audit'
            target = 'MG2550s Boot Audit' if phase == 'install' else 'MG2550s App'
            d = Device(initial, target, failed)
            h.fwctl.wait_device = lambda *a, **k: (d, h.info(d))
            def reboot(dev, mode):
                if mode == 2:
                    assert dev.status == 0xc0, 'App reboot before successful verification'
                dev.resets.append(mode)
                dev.mode = 1 if mode == 1 else 2
                if mode == 2:
                    dev.product = dev.target
                dev.status = 0x80
            h.fwctl.request_reboot = reboot
            with tempfile.TemporaryDirectory(prefix='mg2550s-audit-host-') as directory:
                h.DEST = Path(directory)
                sys.argv = ['hardware.py', phase]
                try:
                    h.main()
                    assert not failed
                except RuntimeError:
                    assert failed
                result = json.loads((h.DEST / ('physical_' + phase + '.json')).read_text())
                if phase == 'read':
                    assert not d.resets and not d.received
                    assert result['comparison']['matches_tested_candidate']
                    assert sum(req[1] == 0x95 for req in d.requests) == 512
                    assert all(req[0] == 0xc0 for req in d.requests)
                else:
                    assert d.resets == ([1] if failed else [1, 2])
                    file = 'application.bin' if phase == 'install' else 'original_application.bin'
                    assert bytes(d.received) == (h.BUILD / file).read_bytes()
                cases[name] = {'passed': True, 'reboots': d.resets, 'result': result['result']}
            print('PASS', name, flush=True)
    finally:
        h.time.sleep = actual_sleep
        h.fwctl.wait_device = actual_wait
        h.fwctl.request_reboot = actual_reboot
        h.usb.util.dispose_resources = actual_dispose
    (h.BUILD / 'host_validation.json').write_text(json.dumps({'all_assertions_passed': True, 'cases': cases}, indent=2) + '\n')


if __name__ == '__main__':
    main()
