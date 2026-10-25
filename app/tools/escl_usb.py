#!/usr/bin/env python3
"""Talk eSCL to the app directly over its IPP-USB bulk endpoints.

A bench client for the driverless scan path, doing what ipp-usb does on
Linux: HTTP/1.1 over the bulk endpoints of an IPP-USB interface (class 7/1/4).
It checks IPP identity, scanner capabilities and status, runs one scan job and
saves the returned image. Nothing moves: the current scan source is synthetic.
"""
import argparse
import json
import struct
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP.parent / 'tools'))
from fwctl import wait_device

NS = {'scan': 'http://schemas.hp.com/imaging/escl/2011/05/03',
      'pwg': 'http://www.pwg.org/schemas/2010/12/sm'}
HOST = 'localhost'


class Connection:
    """One IPP-USB interface: a persistent HTTP/1.1 byte stream."""

    def __init__(self, dev, interface):
        import usb.util
        self.dev, self.number = dev, interface.bInterfaceNumber
        try:
            if dev.is_kernel_driver_active(self.number):
                dev.detach_kernel_driver(self.number)
        except (NotImplementedError, usb.core.USBError):
            pass
        usb.util.claim_interface(dev, self.number)
        dev.set_interface_altsetting(self.number, interface.bAlternateSetting)
        endpoints = list(interface)
        self.out = next(e for e in endpoints if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.into = next(e for e in endpoints if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        self.buffer = bytearray()

    def read_more(self, timeout):
        self.buffer += bytes(self.into.read(65536, timeout=timeout))

    def request(self, method, path, body=b'', content_type=None, timeout=10000):
        head = [f'{method} {path} HTTP/1.1', f'Host: {HOST}', 'User-Agent: escl_usb.py']
        if content_type:
            head.append(f'Content-Type: {content_type}')
        if body:
            head.append(f'Content-Length: {len(body)}')
        self.out.write(('\r\n'.join(head) + '\r\n\r\n').encode() + body, timeout=timeout)
        deadline = time.monotonic() + timeout / 1000
        while True:
            result = parse_response(self.buffer)
            if result:
                status, headers, payload, used = result
                del self.buffer[:used]
                if status != 100:
                    return status, headers, payload
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f'{method} {path}: incomplete response ({len(self.buffer)} bytes)')
            self.read_more(timeout)


def parse_response(buf):
    end = buf.find(b'\r\n\r\n')
    if end < 0:
        return None
    lines = bytes(buf[:end]).decode('latin1').split('\r\n')
    status = int(lines[0].split()[1])
    headers = {k.strip().lower(): v.strip() for k, v in (line.split(':', 1) for line in lines[1:])}
    pos = end + 4
    if status == 100:
        return status, headers, b'', pos
    if 'content-length' in headers:
        n = int(headers['content-length'])
        return (status, headers, bytes(buf[pos:pos + n]), pos + n) if len(buf) >= pos + n else None
    body = bytearray()
    while True:
        eol = buf.find(b'\r\n', pos)
        if eol < 0:
            return None
        size = int(bytes(buf[pos:eol]).split(b';')[0], 16)
        if len(buf) < eol + 4 + size:
            return None
        body += buf[eol + 2:eol + 2 + size]
        pos = eol + 4 + size
        if not size:
            return status, headers, bytes(body), pos


def ipp_get_printer_attributes():
    attrs = b''
    for tag, name, value in ((0x47, b'attributes-charset', b'utf-8'),
                             (0x48, b'attributes-natural-language', b'en'),
                             (0x45, b'printer-uri', b'ipp://localhost/ipp/print')):
        attrs += bytes([tag]) + struct.pack('>H', len(name)) + name + struct.pack('>H', len(value)) + value
    return struct.pack('>BBHI', 2, 0, 0x000b, 1) + b'\x01' + attrs + b'\x03'


def ipp_text_attributes(data):
    status, pos, attrs, name = struct.unpack_from('>H', data, 2)[0], 8, {}, None
    while data[pos] != 0x03:
        tag = data[pos]; pos += 1
        if tag < 0x10:
            continue
        n = struct.unpack_from('>H', data, pos)[0]; pos += 2
        name = data[pos:pos + n].decode() if n else name; pos += n
        n = struct.unpack_from('>H', data, pos)[0]; pos += 2
        value = data[pos:pos + n]; pos += n
        if tag >= 0x40:
            attrs.setdefault(name, []).append(value.decode())
    return status, attrs


def settings(mode, resolution, width, height):
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<scan:ScanSettings xmlns:scan="{NS["scan"]}" '
            f'xmlns:pwg="{NS["pwg"]}"><pwg:Version>2.63</pwg:Version><pwg:ScanRegions><pwg:ScanRegion>'
            f'<pwg:Height>{height}</pwg:Height><pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches'
            f'</pwg:ContentRegionUnits><pwg:Width>{width}</pwg:Width><pwg:XOffset>0</pwg:XOffset>'
            f'<pwg:YOffset>0</pwg:YOffset></pwg:ScanRegion></pwg:ScanRegions>'
            f'<pwg:InputSource>Platen</pwg:InputSource><scan:ColorMode>{mode}</scan:ColorMode>'
            f'<scan:XResolution>{resolution}</scan:XResolution><scan:YResolution>{resolution}'
            f'</scan:YResolution><scan:DocumentFormatExt>image/png</scan:DocumentFormatExt>'
            f'</scan:ScanSettings>').encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('scans/escl-test.png'))
    parser.add_argument('--mode', choices=('Grayscale8', 'RGB24'), default='RGB24')
    parser.add_argument('--resolution', type=int, default=75)
    parser.add_argument('--width', type=int, default=2550, help='1/300 inch')
    parser.add_argument('--height', type=int, default=3507, help='1/300 inch')
    parser.add_argument('--interface', type=int, default=0, help='which IPP-USB interface (0 or 1)')
    parser.add_argument('--interface-class', default='7/1/4',
                        help='class/subclass/protocol of the HTTP interfaces (diagnostic builds may differ)')
    parser.add_argument('--serial')
    args = parser.parse_args()
    wanted = tuple(int(x, 0) for x in args.interface_class.split('/'))
    import usb.util

    dev, _ = wait_device('application', serial_number=args.serial)
    report = {}
    try:
        ipp = [i for i in dev.get_active_configuration()
               if (i.bInterfaceClass, i.bInterfaceSubClass, i.bInterfaceProtocol) == wanted
               and i.bNumEndpoints == 2]
        report['ipp_usb_interfaces'] = [i.bInterfaceNumber for i in ipp]
        assert ipp, f'no {args.interface_class} interface: is the eSCL app installed?'
        conn = Connection(dev, ipp[args.interface])

        status, headers, body = conn.request('POST', '/ipp/print', ipp_get_printer_attributes(), 'application/ipp')
        ipp_status, attrs = ipp_text_attributes(body)
        report['ipp'] = {'http': status, 'status': ipp_status,
                         'make_and_model': attrs.get('printer-make-and-model'),
                         'uuid': attrs.get('printer-uuid')}

        status, headers, body = conn.request('GET', '/eSCL/ScannerCapabilities')
        caps = ET.fromstring(body)
        report['capabilities'] = {
            'http': status, 'make_and_model': caps.findtext('pwg:MakeAndModel', namespaces=NS),
            'color_modes': [m.text for m in caps.iterfind('.//scan:ColorMode', NS)],
            'resolutions': [int(r.text) for r in caps.iterfind('.//scan:XResolution', NS)],
            'formats': [f.text for f in caps.iterfind('.//scan:DocumentFormatExt', NS)]}

        status, headers, body = conn.request('GET', '/eSCL/ScannerStatus')
        report['status_before'] = ET.fromstring(body).findtext('pwg:State', namespaces=NS)

        status, headers, _ = conn.request('POST', '/eSCL/ScanJobs',
                                          settings(args.mode, args.resolution, args.width, args.height), 'text/xml')
        assert status == 201, f'ScanJobs returned {status}'
        location = headers['location']
        job = '/eSCL/' + location.split('/eSCL/', 1)[1]
        started = time.monotonic()
        status, headers, image = conn.request('GET', job + '/NextDocument', timeout=1200000)   # 150 dpi step-mode scans take about ten minutes
        elapsed = time.monotonic() - started
        assert status == 200, f'NextDocument returned {status}'
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(image)
        report['scan'] = {'location': location, 'content_type': headers.get('content-type'),
                          'bytes': len(image), 'seconds': round(elapsed, 3),
                          'mbytes_per_second': round(len(image) / elapsed / 1e6, 2), 'file': str(args.out)}
        status, headers, _ = conn.request('GET', job + '/NextDocument')
        report['next_document_after_page'] = status
        status, headers, body = conn.request('GET', '/eSCL/ScannerStatus')
        report['job_state_after'] = ET.fromstring(body).findtext('.//pwg:JobState', namespaces=NS)
        usb.util.release_interface(dev, conn.number)
    finally:
        usb.util.dispose_resources(dev)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
