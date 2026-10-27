#!/usr/bin/env python3
"""MG2550s controller: protected app uploads and explicit dangerous whole-chip writes."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import time
import zlib

VID = 0x04a9
BOOT_PID, APP_PID = 0x176d, 0x12fe
REQUEST_STAY, REQUEST_REBOOT, REQUEST_INFO, REQUEST_BEGIN = range(0x40, 0x44)
REQUEST_DANGEROUS_WRITE = 0x44
FLASH_SIZE = 0x200000
APP_RAM, APP_FLASH, APP_CAPACITY = 0x20020000, 0x10000, 0x1f0000
BOOT_CAPACITY = 0xc000


def validate_image(data, maintenance=False, legacy=False):
    capacity = (0x10000 if maintenance else 0x1e0000) if legacy else (BOOT_CAPACITY if maintenance else APP_CAPACITY)
    if not 32 < len(data) <= capacity:
        raise ValueError('Expected an MGAP application, not a full-chip image')
    magic, version, length, address, entry, crc, flags, header_crc = struct.unpack('<8I', data[:32])
    if (magic != 0x5041474d or version != (1 if legacy else 2) or length != len(data)-32 or length % 4
            or address != (0x20010000 if maintenance else APP_RAM) or entry or flags or zlib.crc32(data[:28]) != header_crc
            or zlib.crc32(data[32:]) != crc):
        raise ValueError('Invalid application header, target address or CRC')


def get_info(device):
    raw = bytes(device.ctrl_transfer(0xc0, REQUEST_INFO, 0, 0, 32, timeout=1000))
    if len(raw) != 32:
        raise RuntimeError('Short bootloader information reply')
    magic, version, mode, flags, remaining, status, address, capacity = struct.unpack('<8I', raw)
    layout = (version, address, capacity)
    regular = mode in (1, 2) and layout in ((2, APP_FLASH, APP_CAPACITY), (1, 0x20000, 0x1e0000))
    installer = mode == 3 and layout == (2, 0, FLASH_SIZE)
    if magic != 0x5746474d or not (regular or installer):
        raise RuntimeError('Unrecognized firmware management protocol')
    return {'layout_version': version, 'mode': {1: 'bootloader', 2: 'application', 3: 'installer'}[mode],
            'held': bool(flags & 1), 'busy': bool(flags & 2), 'app_valid': bool(flags & 4),
            'dangerous_write': bool(flags & 8), 'reboot_blocked': bool(flags & 16),
            'remaining_ms': remaining, 'status': f'{status:02X}',
            'application_flash': hex(address), 'application_capacity': capacity}


def discover(serial_number=None):
    import usb.core
    import usb.util
    found = []
    for dev in usb.core.find(find_all=True, idVendor=VID):
        if dev.idProduct not in (BOOT_PID, APP_PID):
            continue
        try:
            if serial_number and dev.serial_number != serial_number:
                continue
            info = get_info(dev)
        except (usb.core.USBError, RuntimeError):
            usb.util.dispose_resources(dev)
            continue
        found.append((dev, info))
    if len(found) > 1:
        for dev, _ in found:
            usb.util.dispose_resources(dev)
        raise RuntimeError('Multiple matching printers: select --serial')
    return found[0] if found else (None, None)


def wait_device(mode=None, timeout=15, serial_number=None):
    import usb.util
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dev, info = discover(serial_number)
        if dev is not None and (mode is None or info['mode'] == mode):
            return dev, info
        if dev is not None:
            usb.util.dispose_resources(dev)
        time.sleep(0.1)
    raise TimeoutError(f'No responsive {mode or "management"} USB device; use I2C or reset if firmware is stuck')


def request_reboot(dev, mode):
    # Wait for the acknowledged control transfer, then discard the stale handle.
    import usb.util
    dev.ctrl_transfer(0x40, REQUEST_REBOOT, mode, 0, b'', timeout=3000)
    usb.util.dispose_resources(dev)


def enter_bootloader(serial_number=None):
    dev, info = wait_device(serial_number=serial_number)
    if info['mode'] == 'application':
        request_reboot(dev, 1)
        # The hybrid app (2 MiB image, two CRC passes) takes about 20 s to hand over.
        dev, info = wait_device('bootloader', timeout=90, serial_number=serial_number)
    dev.ctrl_transfer(0x40, REQUEST_STAY, 0, 0, b'', timeout=3000)
    return dev


class Bridge:
    """Own the serial session and always release I2C pins on exit."""
    def __init__(self, port):
        import serial
        self.port = serial.Serial(port, 115200, timeout=0.2, write_timeout=10)

    def send(self, text):
        self.port.write(text.encode() + b'\n')
        self.port.flush()

    def expect(self, prefix, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.port.readline().decode(errors='replace').strip()
            if not line:
                continue
            print(line, flush=True)
            if line.startswith(('ERROR', 'TIMEOUT')):
                raise RuntimeError(line)
            if line.startswith(prefix):
                return line
        raise TimeoutError('Bridge did not return ' + prefix)

    def __enter__(self):
        try:
            self.send('INFO')
            try:
                info = self.expect('INFO RSC1', 2)
            except TimeoutError:
                # ESP32 native USB-Serial/JTAG hard reset; no flash write.
                self.port.dtr = False
                self.port.rts = True
                time.sleep(0.2)
                self.port.rts = False
                time.sleep(0.2)
                self.port.reset_input_buffer()
                self.send('INFO'); info = self.expect('INFO RSC1', 10)
            self.autowire = 'AUTOWIRE=1' in info.split()
            self.send('FLOAT'); self.expect('FLOATING')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        try:
            self.send('FLOAT'); self.expect('FLOATING', 3)
        finally:
            self.port.close()

    def detect(self):
        if not self.autowire:
            raise RuntimeError('ESP32 bridge lacks wiring validation; update its firmware first')
        self.send('DETECT')
        result = self.expect('WIRING ', 12)
        if not result.startswith('WIRING VERIFIED '):
            raise RuntimeError('SDA/SCL wiring was not verified: ' + result)
        return result

    def load(self, data, maintenance=False):
        self.detect()
        verb = 'LOADBOOT' if maintenance else 'LOAD'
        self.send(f'{verb} {len(data)} {zlib.crc32(data):08x}')
        self.expect('SEND', 30)
        for offset in range(0, len(data), 1024):
            self.port.write(data[offset:offset+1024])
        self.port.flush(); self.expect('LOADED FLOATING', 30)

    def completion(self):
        result = self.expect('DONE', 600)
        if result != 'DONE C0 FLOATING':
            raise RuntimeError(result)

    def command(self, command):
        self.detect()
        self.send(f'COMMAND {command}'); self.expect('ARMED'); self.completion()


def flash_usb(data, serial_number=None):
    import usb.util
    dev = enter_bootloader(serial_number)
    try:
        if get_info(dev)['layout_version'] != 2:
            raise RuntimeError('Printer still uses the 128 KiB layout; migrate it before uploading layout-2 apps')
        dev.ctrl_transfer(0x40, REQUEST_BEGIN, len(data) & 0xffff, len(data) >> 16, b'', timeout=3000)
        for offset in range(0, len(data), 16384):
            part = data[offset:offset+16384]
            written = dev.write(1, part, timeout=60000)
            if written != len(part):
                raise RuntimeError('Partial USB write; do not retry a stream of unknown position')
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            info = get_info(dev)
            if info['status'] == 'C0':
                return info
            if info['status'] != '80':
                raise RuntimeError('Update failed: ' + info['status'])
            time.sleep(0.1)
        raise TimeoutError('No flash completion status')
    finally:
        usb.util.dispose_resources(dev)


def dangerous_packet(data, expected_sha256):
    """No padding/truncation or executable assumptions: the exact full-chip image."""
    if len(data) != FLASH_SIZE:
        raise ValueError('dangerous-write requires an exact 2 MiB raw flash image')
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError('Whole-flash SHA-256 mismatch')
    head = struct.pack('<7I', 0x57474e44, 1, FLASH_SIZE, 0, 1, zlib.crc32(data), 0)
    return head + struct.pack('<I', zlib.crc32(head)) + data


def dangerous_write(data, expected_sha256, serial_number=None, report_path=None):
    import usb.util
    packet = dangerous_packet(data, expected_sha256)  # Validate before contacting hardware.
    report = {'image_bytes': len(data), 'image_sha256': hashlib.sha256(data).hexdigest(),
              'packet_sha256': hashlib.sha256(packet).hexdigest(), 'bytes_transferred': 0,
              'status_changes': [], 'result': 'started'}
    dev = None
    try:
        dev = enter_bootloader(serial_number)
        before = get_info(dev); report['before'] = before
        if not before['dangerous_write']:
            raise RuntimeError('Installed firmware has no dangerous-write capability; a compatible installer is required')
        if before['busy']:
            raise RuntimeError('Device already has an active transfer')
        dev.ctrl_transfer(0x40, REQUEST_DANGEROUS_WRITE, len(packet)&65535,
                          len(packet)>>16, b'', timeout=3000)
        for offset in range(0, len(packet), 16384):
            part = packet[offset:offset+16384]
            if dev.write(1, part, timeout=60000) != len(part):
                raise RuntimeError('Partial dangerous write; no automatic retry')
            report['bytes_transferred'] += len(part)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            info = get_info(dev)
            if not report['status_changes'] or info != report['status_changes'][-1]:
                report['status_changes'].append(info)
            if info['status'] == 'C0' and not info['busy'] and not info['reboot_blocked']:
                report['result'] = 'full_flash_readback_verified'
                return info
            if info['status'] != '80':
                raise RuntimeError('Dangerous write failed: '+info['status']+'; keep power on and recover through the RAM writer')
            time.sleep(0.1)
        raise TimeoutError('No final dangerous-write result; keep power on and check status')
    except BaseException as error:
        report['result'] = 'error'; report['error'] = str(error)
        raise
    finally:
        if dev is not None: usb.util.dispose_resources(dev)
        if report_path is not None:
            Path(report_path).write_text(json.dumps(report, indent=2)+'\n')


APP_REQUEST_POWER_STATUS, APP_REQUEST_POWER_CONTROL = 0xa0, 0xa1
POWER_STATES = ('ready', 'active', 'quiescing', 'standby', 'waking', 'fault')
POWER_REASONS = ('boot', 'timeout', 'host', 'panel', 'job', 'fault')
POWER_RESULTS = ('none', 'accepted', 'busy', 'invalid', 'fault')
POWER_ERRORS = ('none', 'scanner_power_readback', 'scan_motor_output_readback', 'owner_active')
POWER_OWNERS = ('scan_motor', 'scan_page', 'scan_lamp', 'scan_armed', 'paper', 'carriage', 'page_job', 'stamp',
                'hybrid', 'cartridge', 'head_fire', 'print_mech', 'print_rx', 'raster_rx', 'print_job')
POWER_CAPS = ('scanner', 'scan_motor', 'panel', 'timeout')


def power_status(dev):
    import usb.core
    try: raw = bytes(dev.ctrl_transfer(0xc0, APP_REQUEST_POWER_STATUS, 0, 0, 64, timeout=1000))
    except usb.core.USBError: raw = b''
    if len(raw) != 64 or raw[:4] != b'1RWP':
        raise RuntimeError('No power manager: this application does not answer USB 0xa0 (protocol PWR1)')
    w = struct.unpack('<16I', raw)
    return {'protocol': 'PWR1', 'state': POWER_STATES[w[1] & 0xff], 'reason': POWER_REASONS[w[1] >> 8 & 0xff],
            'last_result': POWER_RESULTS[w[1] >> 16 & 0xff], 'fail_stage': w[1] >> 24,
            'idle_timeout_s': w[2], 'idle_age_ms': w[3],
            'owners': [n for i, n in enumerate(POWER_OWNERS) if w[4] >> i & 1],
            'last_error': POWER_ERRORS[w[5]] if w[5] < len(POWER_ERRORS) else w[5],
            'capabilities': [n for i, n in enumerate(POWER_CAPS) if w[6] >> i & 1],
            'logical_outputs': {'scanner_power_reg': w[7] & 1, 'panel_dark': bool(w[7] & 2), 'head_prepared': bool(w[7] & 4),
                                'scan_rows_unread': bool(w[7] & 8), 'paper_held': bool(w[7] & 16),
                                'scan_motor_ctrl': w[8], 'scan_motor_compare': w[9]},
            'transitions': w[10], 'standby_count': w[11], 'wake_count': w[12], 'uptime_ms': w[13],
            'unsupported': ['print parking/head capping', 'driver-IC sleep', 'rail switching', 'CPU sleep']}


def power_command(dev, info, action, seconds=None):
    if info.get('mode') != 'application':
        raise RuntimeError('power commands need the running application, not the bootloader')
    import usb.core
    op = {'standby': 1, 'wake': 2, 'timeout': 3}.get(action)
    if op:
        arg = 0
        if action == 'timeout':
            if seconds is None or not 0 <= seconds <= 3600: raise ValueError('timeout needs 0..3600 seconds')
            arg = seconds
        try:
            dev.ctrl_transfer(0x40, APP_REQUEST_POWER_CONTROL, op, arg, b'', timeout=3000); accepted = True
        except usb.core.USBError:
            accepted = False                      # stall: busy, invalid or a failed quiesce; see last_result
        status = power_status(dev); status['request'] = action; status['accepted'] = accepted
        return status
    return power_status(dev)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', help='Printer USB serial number (not the ESP32 port)')
    parser.add_argument('--port', default='/dev/cu.usbmodem1101', help='ESP32 serial port')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    stay = sub.add_parser('stay'); stay.add_argument('--transport', choices=('usb', 'i2c'), default='usb')
    reboot = sub.add_parser('reboot')
    reboot.add_argument('--mode', choices=('normal', 'bootloader', 'application'),
                        default='bootloader', help='Reboot destination (default: persistent bootloader)')
    reboot.add_argument('--transport', choices=('usb', 'i2c'), default='usb')
    flash = sub.add_parser('flash')
    flash.add_argument('image', type=Path)
    flash.add_argument('--transport', choices=('usb', 'i2c'), default='usb')
    flash.add_argument('--stay', action='store_true', help='Remain in rescue after success instead of starting app')
    dangerous = sub.add_parser('dangerous-write', help='Overwrite all 2 MiB, INCLUDING both bootloader stages')
    dangerous.add_argument('image', type=Path, help='Exact full-chip raw image, e.g. factory.bin or the stock dump')
    dangerous.add_argument('--sha256', required=True, help='Required SHA-256 of the exact intended image')
    dangerous.add_argument('--report', type=Path, help='Save transfer and verification results as JSON')
    sub.add_parser('float', help='Release ESP32 GPIO4/5')
    sub.add_parser('detect', help='Validate SDA/SCL orientation without printer reset or flash')
    maintenance = sub.add_parser('stage0-flash', help='Explicitly replace stage 2 using first-stage I2C recovery')
    maintenance.add_argument('image', type=Path)
    maintenance.add_argument('--sha256', required=True, help='Required exact bootloader.bin SHA-256')
    maintenance.add_argument('--manual-reset', action='store_true', help='Wait for a manual printer reset if management is stuck')
    power = sub.add_parser('power', help='Application power manager: status, standby, wake, idle timeout (USB 0xa0/0xa1)')
    power.add_argument('action', choices=('status', 'standby', 'wake', 'timeout'))
    power.add_argument('seconds', type=int, nargs='?', help='timeout: idle seconds before standby, 0 = never (max 3600)')
    args = parser.parse_args()
    import usb.util
    if args.command == 'status':
        dev, info = wait_device(serial_number=args.serial)
        try: print(json.dumps(info, indent=2))
        finally: usb.util.dispose_resources(dev)
    elif args.command == 'power':
        dev, info = wait_device(serial_number=args.serial)
        try: print(json.dumps(power_command(dev, info, args.action, args.seconds), indent=2))
        finally: usb.util.dispose_resources(dev)
    elif args.command == 'float':
        with Bridge(args.port): pass
    elif args.command == 'detect':
        with Bridge(args.port) as bridge: bridge.detect()
    elif args.command == 'dangerous-write':
        print('DANGEROUS: replaces ALL flash, including recovery. Power loss may require SPI. No automatic reboot.', flush=True)
        print(json.dumps(dangerous_write(args.image.read_bytes(), args.sha256,
                                        args.serial, args.report), indent=2))
    elif args.command == 'stay':
        if args.transport == 'usb':
            dev = enter_bootloader(args.serial); usb.util.dispose_resources(dev)
        else:
            with Bridge(args.port) as bridge: bridge.command(3)
    elif args.command == 'reboot':
        mode = ('normal', 'bootloader', 'application').index(args.mode)
        if args.transport == 'usb':
            dev, _ = wait_device(serial_number=args.serial); request_reboot(dev, mode)
        else:
            with Bridge(args.port) as bridge: bridge.command(mode + 2)
    elif args.command == 'stage0-flash':
        data = args.image.read_bytes(); validate_image(data, maintenance=True)
        if hashlib.sha256(data).hexdigest() != args.sha256:
            raise ValueError('Bootloader hash mismatch')
        # Acquire the responsive management interface before arming. A broken
        # stage 2 needs the explicit --manual-reset path; no ordinary app upload
        # can select this first-stage maintenance offer.
        dev = None
        if not args.manual_reset:
            dev, info = wait_device(serial_number=args.serial)
            if info['layout_version'] != 2:
                usb.util.dispose_resources(dev)
                raise RuntimeError('stage0-flash cannot migrate the protected first stage; use an explicit whole-image migration')
        try:
            with Bridge(args.port) as bridge:
                bridge.load(data, maintenance=True); bridge.send('ARM'); bridge.expect('ARMED')
                if args.manual_reset: print('Power-cycle the printer now; bridge is armed.', flush=True)
                else: request_reboot(dev, 1); dev = None
                bridge.completion()
        finally:
            if dev is not None: usb.util.dispose_resources(dev)
    elif args.command == 'flash':
        data = args.image.read_bytes(); validate_image(data)
        print(f'Application: {len(data)} bytes, SHA-256 {hashlib.sha256(data).hexdigest()}', flush=True)
        if args.transport == 'usb':
            print(json.dumps(flash_usb(data, args.serial), indent=2))
        else:
            # The cooperative main app sees OFFER_APPLICATION and resets into
            # rescue itself. Stage 0 ignores it; it only accepts maintenance flag 1.
            with Bridge(args.port) as bridge:
                bridge.load(data); bridge.send('ARM'); bridge.expect('ARMED'); bridge.completion()
        if not args.stay:
            if args.transport == 'usb':
                dev, _ = wait_device('bootloader', serial_number=args.serial); request_reboot(dev, 2)
            else:
                with Bridge(args.port) as bridge: bridge.command(4)
            # USB might deliberately be disconnected for an I2C-only update.
            if args.transport == 'usb':
                dev, info = wait_device('application', serial_number=args.serial)
                usb.util.dispose_resources(dev); print(json.dumps(info, indent=2))


if __name__ == '__main__':
    main()
