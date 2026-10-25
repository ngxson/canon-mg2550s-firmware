#!/usr/bin/env python3
"""Explicit phases of the user-authorized temporary boot audit.

Only install/restore write flash, using protected app request 0x43. No dangerous
write, arbitrary memory write, motor command or whole-chip operation exists here.
An update error stops without requesting a reboot. Read uses only USB IN 0x95.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / 'app/build/boot-audit'
DEST = ROOT / 'bootloader/evidence/vectors/audit'
sys.path.insert(0, str(ROOT / 'tools'))
import fwctl
import usb.core
import usb.util


def sha(data):
    return hashlib.sha256(data).hexdigest()


def info(dev):
    # Give erase/program gaps more room than fwctl's default 1-second INFO.
    class StatusDevice:
        def ctrl_transfer(self, *args, **kwargs):
            kwargs['timeout'] = 10000
            return dev.ctrl_transfer(*args, **kwargs)
    return fwctl.get_info(StatusDevice())


def preflight():
    manifest = json.loads((BUILD / 'manifest.json').read_text())
    validation = json.loads((BUILD / 'validation.json').read_text())
    audit = (BUILD / 'application.bin').read_bytes()
    original = (BUILD / 'original_application.bin').read_bytes()
    if not validation['all_assertions_passed']:
        raise RuntimeError('Emulation did not pass')
    if sha(audit) != validation['audit_sha256'] or sha(original) != validation['restore_sha256']:
        raise RuntimeError('An artifact differs from the emulator-validated image')
    if sha(audit) != manifest['artifacts']['application.bin']['sha256']:
        raise RuntimeError('Audit build manifest mismatch')
    if sha(original) != '47f078c2a3cc75dac1d67dd02c0531e9c85e47f67b0cfe0edfa471d9e582b15a':
        raise RuntimeError('Unexpected original app')
    fwctl.validate_image(audit)
    fwctl.validate_image(original)
    for relative, digest in manifest['source_sha256'].items():
        if sha(Path(relative).read_bytes()) != digest:
            raise RuntimeError('Source changed since validation: ' + relative)
    return audit, original


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('phase', choices=('install', 'read', 'restore'))
    args = p.parse_args()
    audit, original = preflight()
    DEST.mkdir(parents=True, exist_ok=True)
    path = DEST / ('physical_' + args.phase + '.json')
    if path.exists():
        raise SystemExit('Refusing to overwrite an existing physical phase report: ' + str(path))
    report = {'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'phase': args.phase, 'result': 'started', 'events': []}
    dev = None

    def event(name, **details):
        row = {'event': name, **details}
        report['events'].append(row)
        path.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(row), flush=True)

    def application(expected_product):
        d, status = fwctl.wait_device('application', timeout=30)
        try:
            product = d.product
            if product != expected_product or not status['app_valid']:
                raise RuntimeError('Unexpected application identity/validity: ' + str((product, status)))
            event('application_responding', product=product, info=status)
            return d
        except BaseException:
            usb.util.dispose_resources(d)
            raise

    try:
        if args.phase in ('install', 'restore'):
            payload = audit if args.phase == 'install' else original
            expected_before = 'MG2550s App' if args.phase == 'install' else 'MG2550s Boot Audit'
            dev = application(expected_before)
            fwctl.request_reboot(dev, 1)
            dev = None
            dev, _ = fwctl.wait_device('bootloader', timeout=30)
            before = info(dev)
            if before['layout_version'] != 2 or before['busy'] or before['reboot_blocked']:
                raise RuntimeError('Bootloader is not ready for a protected app update')
            dev.ctrl_transfer(0x40, 0x40, 0, 0, b'', timeout=5000)
            event('protected_upload_begin', info=before, bytes=len(payload), sha256=sha(payload),
                  flash_start='0x10000', request='0x43')
            dev.ctrl_transfer(0x40, 0x43, len(payload) & 65535, len(payload) >> 16, b'', timeout=5000)
            report['bytes_transferred'] = 0
            for offset in range(0, len(payload), 16384):
                part = payload[offset:offset + 16384]
                if dev.write(1, part, timeout=60000) != len(part):
                    raise RuntimeError('Short USB write: no automatic resend or reboot')
                report['bytes_transferred'] += len(part)
            event('payload_transferred', bytes=report['bytes_transferred'])
            deadline = time.monotonic() + 120
            last = None
            while time.monotonic() < deadline:
                status = info(dev)
                if status != last:
                    event('update_status', info=status)
                    last = status
                if status['status'] == 'C0' and not status['busy'] and not status['reboot_blocked'] and status['app_valid']:
                    break
                if status['status'] != '80':
                    raise RuntimeError('Protected update failed: ' + str(status))
                time.sleep(0.2)
            else:
                raise TimeoutError('No verified completion; no reboot requested')
            event('app_readback_verified_by_updater', sha256=sha(payload))
            fwctl.request_reboot(dev, 2)
            dev = None
            dev = application('MG2550s Boot Audit' if args.phase == 'install' else 'MG2550s App')
            report['result'] = 'audit_app_installed' if args.phase == 'install' else 'original_app_restored'
        else:
            dev = application('MG2550s Boot Audit')
            dumps = []
            for number in (1, 2):
                data = bytearray()
                for offset in range(0, 0x10000, 256):
                    part = bytes(dev.ctrl_transfer(0xc0, 0x95, offset, 0, 256, timeout=5000))
                    if len(part) != 256:
                        raise RuntimeError('Short boot-region read at ' + hex(offset))
                    data.extend(part)
                raw = bytes(data)
                (DEST / ('boot_read_' + str(number) + '.bin')).write_bytes(raw)
                dumps.append(raw)
                event('boot_region_read', pass_number=number, bytes=len(raw), sha256=sha(raw))
            if dumps[0] != dumps[1]:
                raise RuntimeError('The two complete reads differ')
            raw = dumps[0]
            candidate = (ROOT / 'bootloader/build/vectors/boot_region.bin').read_bytes()
            baseline = (ROOT / 'bootloader/build/boot_region.bin').read_bytes()
            report['comparison'] = {
                'two_reads_identical': True, 'boot_region_sha256': sha(raw),
                'stage0_sha256': sha(raw[:0x4000]), 'stage2_slot_sha256': sha(raw[0x4000:]),
                'matches_tested_candidate': raw == candidate, 'matches_saved_baseline': raw == baseline,
                'candidate_differing_bytes': sum(a != b for a, b in zip(raw, candidate)),
                'baseline_differing_bytes': sum(a != b for a, b in zip(raw, baseline)),
                'vector_descriptor_words': [hex(x) for x in struct.unpack('<6I', raw[0x40:0x58])]}
            event('boot_comparison', **report['comparison'])
            event('after_read', info=info(dev))
            report['result'] = 'boot_region_read_and_compared'
    except BaseException as error:
        report['result'] = 'error'
        report['error'] = str(error)
        raise
    finally:
        if dev is not None:
            usb.util.dispose_resources(dev)
        report['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        path.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'result': report['result'], 'report': str(path)}), flush=True)


if __name__ == '__main__':
    main()
