#!/usr/bin/env python3
"""Emulate an IPP-USB host talking eSCL to the app behind the real bootloader.

The host side behaves like ipp-usb: HTTP/1.1 requests go out on the bulk OUT
endpoint of an IPP-USB interface (one max-size packet per armed transfer) and
responses are read from its bulk IN endpoint. A read only completes on a short
packet, as on a real host, so every response must end with a short packet or
a zero-length packet. Every image byte is checked against the synthetic chart
recomputed here. USB management (INFO polling, reboot into rescue) must keep
working throughout.
"""
import hashlib
import json
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
BOOT = APP.parent / 'bootloader'
sys.path.insert(0, str(BOOT / 'tests'))
from model import Model, setup, TICKS_PER_MS  # noqa: E402

NS = {'scan': 'http://schemas.hp.com/imaging/escl/2011/05/03',
      'pwg': 'http://www.pwg.org/schemas/2010/12/sm'}
CONN = ((2, 1), (3, 3))     # (bulk OUT, bulk IN) endpoint numbers per IPP-USB interface
HOST = 'localhost:60000'


class IppUsbHost(Model):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.to_device = [bytearray(), bytearray()]
        self.from_device = [bytearray(), bytearray()]
        self.transfers = [[], []]
        self.cursor = [0, 0]
        self.hold_in = set()
        self.eager = None           # request name whose status stage is followed at once
        self.legacy = bytearray()   # stray data for the legacy printer interface (EP1 OUT)

    def eager_status_then_setup(self):
        """Finish the eager request's status stage and deliver the next SETUP in
        the same instant, before the firmware has polled: what a host does when
        a second client (macOS ippusbd) queues control requests back to back."""
        request = self.current
        if not (request and request['name'] == self.eager and request['phase'] == 'in'
                and self.ready('in', 0) and self.requests and ('out', 0) in self.armed):
            return False
        assert not self.transfer('in', 0)
        self.finish()
        name, packet = self.requests.popleft()
        self.current = {'name': name, 'setup': packet, 'data': bytearray(), 'phase': 'in',
                        'length': struct.unpack_from('<H', packet, 6)[0]}
        self.transfer('out', 0, packet, True)
        self.eager = None
        return True

    def host_step(self):
        if self.eager and self.eager_status_then_setup():
            return
        if self.configured and self.legacy and self.ready('out', 1):
            chunk = bytes(self.legacy[:self.max_packet])
            del self.legacy[:len(chunk)]
            self.transfer('out', 1, chunk)
        if self.configured:
            for c, (out_ep, in_ep) in enumerate(CONN):
                if c not in self.hold_in and self.ready('in', in_ep):
                    block = self.endpoint('in', in_ep)
                    size = self.reg(block + 0x10)
                    length, packets = size & 0x7ffff, (size >> 19) & 0x3ff
                    assert packets == max(1, -(-length // self.max_packet)), (c, length, packets)
                    assert self.reg(block + 0x14) % 4 == 0, 'IN DMA address not word aligned'
                    data = self.transfer('in', in_ep)
                    self.from_device[c] += data
                    self.transfers[c].append(len(data))
                if self.to_device[c] and self.ready('out', out_ep):
                    block = self.endpoint('out', out_ep)
                    size = self.reg(block + 0x10)
                    assert size == (1 << 19) | self.max_packet, hex(size)
                    assert self.reg(block + 0x14) % 4 == 0, 'OUT DMA address not word aligned'
                    chunk = bytes(self.to_device[c][:self.max_packet])
                    del self.to_device[c][:len(chunk)]
                    self.transfer('out', out_ep, chunk)
        super().host_step()


def parse_response(buf):
    """(status, headers, body, consumed) or None while incomplete."""
    end = buf.find(b'\r\n\r\n')
    if end < 0:
        return None
    lines = buf[:end].decode('latin1').split('\r\n')
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, value = line.split(':', 1)
        headers[name.strip().lower()] = value.strip()
    pos = end + 4
    if status == 100:
        return status, headers, b'', pos
    if 'content-length' in headers:
        n = int(headers['content-length'])
        if len(buf) < pos + n:
            return None
        return status, headers, buf[pos:pos + n], pos + n
    assert headers.get('transfer-encoding') == 'chunked', headers
    body = bytearray()
    while True:
        eol = buf.find(b'\r\n', pos)
        if eol < 0:
            return None
        size = int(buf[pos:eol], 16)
        if len(buf) < eol + 2 + size + 2:
            return None
        assert buf[eol + 2 + size:eol + 4 + size] == b'\r\n'
        body += buf[eol + 2:eol + 2 + size]
        pos = eol + 4 + size
        if size == 0:
            return status, headers, bytes(body), pos


def send(e, c, raw):
    e.to_device[c] += raw


def receive(e, c, budget=400_000_000):
    """Run until a complete, short-packet-terminated response arrives on c."""
    state = {}

    def done(x):
        if 'r' in state:
            return True                     # Model.run re-checks after stopping
        key = len(x.transfers[c])
        if key == state.get('key'):
            return False
        state['key'] = key
        last = x.transfers[c][-1] if x.transfers[c] else None
        if last is None or (last and last % x.max_packet == 0):
            return False                    # a real host read is still pending
        while True:
            r = parse_response(bytes(x.from_device[c][x.cursor[c]:]))
            if not r:
                return False
            if r[0] == 100:
                state.setdefault('interim', 0)
                state['interim'] += 1
                x.cursor[c] += r[3]
                continue
            state['r'] = r
            return True
    e.run(done, resume=True, budget=budget)
    status, headers, body, used = state['r']
    e.cursor[c] += used
    assert e.cursor[c] == len(e.from_device[c]), 'unexpected bytes after the response'
    return status, headers, body, state.get('interim', 0)


def http(e, c, method, path, body=b'', headers=(), chunked=False):
    head = [f'{method} {path} HTTP/1.1', f'Host: {HOST}', 'User-Agent: validate_escl', *headers]
    if chunked:
        head.append('Transfer-Encoding: chunked')
        pieces = [body[i:i + 97] for i in range(0, len(body), 97)]
        body = b''.join(b'%x;ext=1\r\n%s\r\n' % (len(p), p) for p in pieces) + b'0\r\nX-Trailer: 1\r\n\r\n'
    elif body:
        head.append(f'Content-Length: {len(body)}')
    send(e, c, ('\r\n'.join(head) + '\r\n\r\n').encode() + body)
    return receive(e, c)


def ipp_request(operation, request_id):
    attrs = b''
    for tag, name, value in ((0x47, b'attributes-charset', b'utf-8'),
                             (0x48, b'attributes-natural-language', b'en'),
                             (0x45, b'printer-uri', b'ipp://localhost/ipp/print')):
        attrs += bytes([tag]) + struct.pack('>H', len(name)) + name + struct.pack('>H', len(value)) + value
    return struct.pack('>BBHI', 2, 0, operation, request_id) + b'\x01' + attrs + b'\x03'


def ipp_parse(data):
    version, status, request_id = data[0:2], *struct.unpack_from('>HI', data, 2)
    pos, attrs, groups, name = 8, {}, [], None
    while True:
        tag = data[pos]; pos += 1
        if tag == 0x03:
            break
        if tag < 0x10:
            groups.append(tag); continue
        n = struct.unpack_from('>H', data, pos)[0]; pos += 2
        if n:
            name = data[pos:pos + n].decode(); attrs.setdefault(name, [])
        pos += n
        n = struct.unpack_from('>H', data, pos)[0]; pos += 2
        attrs[name].append((tag, data[pos:pos + n])); pos += n
    assert pos == len(data), 'trailing IPP bytes'
    return version, status, request_id, groups, attrs


def chart(x, y, width, height, resolution, color, bilevel=False):
    step = 300 // resolution
    pixels, lines = width * resolution // 300, height * resolution // 300
    rows = []
    for j in range(lines):
        v = y + j * step
        row = bytearray()
        for i in range(pixels):
            u = x + i * step
            r, g = u * 255 // 2550, v * 255 // 3507
            b = 224 if ((u // 150) ^ (v // 150)) & 1 else 64
            if u % 300 < 3 or v % 300 < 3:
                r = g = b = 0
            row += bytes((r, g, b)) if color else bytes(((r * 77 + g * 150 + b * 29) >> 8,))
        if bilevel:                         # threshold at 128, pack MSB first
            row = bytes(sum(0x80 >> b for b in range(8) if i + b < pixels and row[i + b] >= 128)
                        for i in range(0, pixels, 8))
        rows.append(bytes(row))
    return pixels, lines, rows


def decode_png(data, complete=True):
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    pos, chunks = 8, []
    while pos < len(data):
        length = struct.unpack_from('>I', data, pos)[0]
        kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        crc = struct.unpack_from('>I', data, pos + 8 + length)[0]
        assert zlib.crc32(kind + body) == crc, f'bad CRC in {kind}'
        chunks.append((kind, body)); pos += 12 + length
    assert pos == len(data)
    kinds = [k for k, _ in chunks]
    width, height, depth, ctype = struct.unpack_from('>IIBB', chunks[0][1])
    phys = dict(chunks).get(b'pHYs')
    idat = b''.join(b for k, b in chunks if k == b'IDAT')
    if not complete:
        assert kinds[-1] != b'IEND'
        return width, height, ctype, None, phys, depth
    assert kinds[0] == b'IHDR' and kinds[-1] == b'IEND' and depth in (1, 8)
    raw = zlib.decompress(idat)
    stride = ((width + 7) // 8 if depth == 1 else width * (3 if ctype == 2 else 1)) + 1
    assert len(raw) == height * stride
    rows = []
    for j in range(height):
        assert raw[j * stride] == 0, 'unexpected filter type'
        rows.append(raw[j * stride + 1:(j + 1) * stride])
    return width, height, ctype, rows, phys, depth


def settings(*, mode='Grayscale8', res=75, region=None, fmt='image/png', extra=''):
    reg = ''
    if region:
        x, y, w, h = region
        reg = ('<pwg:ScanRegions><pwg:ScanRegion><pwg:Height>%d</pwg:Height><pwg:ContentRegionUnits>'
               'escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits><pwg:Width>%d</pwg:Width>'
               '<pwg:XOffset>%d</pwg:XOffset><pwg:YOffset>%d</pwg:YOffset></pwg:ScanRegion>'
               '</pwg:ScanRegions>' % (h, w, x, y))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<scan:ScanSettings '
            'xmlns:scan="%s" xmlns:pwg="%s"><pwg:Version>2.63</pwg:Version>%s'
            '<scan:Intent>Document</scan:Intent><pwg:InputSource>Platen</pwg:InputSource>'
            '<scan:ColorMode>%s</scan:ColorMode><scan:XResolution>%d</scan:XResolution>'
            '<scan:YResolution>%d</scan:YResolution><scan:DocumentFormatExt>%s</scan:DocumentFormatExt>'
            '%s</scan:ScanSettings>' % (NS['scan'], NS['pwg'], reg, mode, res, res, fmt, extra)).encode()


def scanner_status(e, c):
    status, headers, body, _ = http(e, c, 'GET', '/eSCL/ScannerStatus')
    assert status == 200 and headers['content-type'] == 'text/xml'
    root = ET.fromstring(body)
    job = root.find('scan:Jobs/scan:JobInfo', NS)
    info = None
    if job is not None:
        info = {k: job.findtext('pwg:' + k, namespaces=NS)
                for k in ('JobUri', 'JobState', 'ImagesCompleted', 'ImagesToTransfer')}
    return root.findtext('pwg:State', namespaces=NS), info


def check_image(body, region, res, color, complete=True, bilevel=False):
    x, y, w, h = region
    pixels, lines, expected = chart(x, y, w, h, res, color, bilevel)
    width, height, ctype, rows, phys, depth = decode_png(body, complete)
    assert (width, height, ctype, depth) == (pixels, lines, 2 if color else 0, 1 if bilevel else 8)
    ppm = struct.unpack_from('>II', phys)
    assert ppm[0] == ppm[1] == (res * 10000 + 127) // 254 and phys[8] == 1
    if complete:
        bad = [j for j in range(lines) if rows[j] != expected[j]]
        assert not bad, f'{len(bad)} rows differ, first {bad[0]}'
    return pixels, lines


def check_jpeg(body, region, res, color, bilevel=False, min_psnr=30.0):
    """Decode with Pillow (an independent decoder) and compare with the chart."""
    import io
    import math
    from PIL import Image
    x, y, w, h = region
    pixels, lines, expected = chart(x, y, w, h, res, color)
    if bilevel:
        expected = [bytes(255 if v >= 128 else 0 for v in row) for row in expected]
    image = Image.open(io.BytesIO(body))
    assert image.format == 'JPEG' and image.size == (pixels, lines), (image.format, image.size)
    assert image.mode == ('RGB' if color else 'L'), image.mode
    assert image.info.get('dpi') == (res, res) and not image.info.get('progressive')
    decoded = image.tobytes()
    reference = b''.join(expected)
    error = sum((a - b) ** 2 for a, b in zip(decoded, reference)) / len(reference)
    psnr = 99.0 if not error else 10 * math.log10(255 ** 2 / error)
    assert psnr >= min_psnr, f'PSNR {psnr:.1f} dB'
    return pixels, lines, round(psnr, 1)


def create_job(e, c, body, **kw):
    status, headers, _, interim = http(e, c, 'POST', '/eSCL/ScanJobs', body,
                                       headers=('Content-Type: text/xml',), **kw)
    return status, headers.get('location'), interim


def factory_and_metadata(build=APP / 'build/dummy'):
    """Protocol tests use the synthetic-chart build so every byte is checkable."""
    boot_region = (BOOT / 'build/boot_region.bin').read_bytes()
    app = (build / 'application.bin').read_bytes()
    factory = (boot_region + app).ljust(0x200000, b'\xff')
    boot_syms = json.loads((BOOT / 'build/manifest.json').read_text())['symbols']
    app_syms = json.loads((build / 'manifest.json').read_text())['symbols']['application']
    metadata = {'layout': {'version': 2, 'application': [0x10000, 0x200000]},
                'symbols': {'bootloader': boot_syms['bootloader'], 'application': app_syms}}
    return factory, app, metadata


def app_mode(e):
    return bool(e.info()) and e.info()[2] == 2


def config_interfaces(e):
    desc = [d for g, n, d in e.responses if n == 'config' and g == e.generation][-1]
    total = struct.unpack_from('<H', desc, 2)[0]
    assert len(desc) == total == 107, (len(desc), total)
    interfaces, pos = [], 0
    while pos < len(desc):
        length, kind = desc[pos], desc[pos + 1]
        if kind == 4:
            interfaces.append({'number': desc[pos + 2], 'class': tuple(desc[pos + 5:pos + 8]),
                               'endpoints': [], 'device_info': None})
        elif kind == 0x21:
            # IPP USB Device Info: release 1, one Basic Capabilities descriptor.
            assert length == 10 and desc[pos + 2:pos + 6] == bytes([1, 1, 0, 4]), desc[pos:pos + length]
            interfaces[-1]['device_info'] = struct.unpack_from('<H', desc, pos + 6)[0]
        elif kind == 5:
            interfaces[-1]['endpoints'].append((desc[pos + 2], desc[pos + 3], struct.unpack_from('<H', desc, pos + 4)[0]))
        pos += length
    return interfaces


def main():
    factory, app, metadata = factory_and_metadata()
    report = {'app_sha256': hashlib.sha256(app).hexdigest(),
              'factory_sha256': hashlib.sha256(factory).hexdigest(), 'cases': {}}

    def record(name, **details):
        report['cases'][name] = details
        print('PASS', name, json.dumps(details) if details else '', flush=True)

    # High speed: the whole protocol surface.
    e = IppUsbHost(image=factory, metadata=metadata)
    e.run(app_mode)
    interfaces = config_interfaces(e)
    assert [i['class'] for i in interfaces] == [(0xff, 0, 0), (0xff, 0, 0), (7, 1, 4), (7, 1, 4)]   # bulk tool interface is vendor class: only IPP-USB makes a printer
    assert interfaces[2]['endpoints'] == [(0x02, 2, 512), (0x81, 2, 512)]
    assert interfaces[3]['endpoints'] == [(0x03, 2, 512), (0x83, 2, 512)]
    # Print (bit 1), Scan (bit 2), Any HTTP/1.1 (bit 5) on every IPP-USB interface.
    assert [i['device_info'] for i in interfaces] == [None, None, 0x13, 0x13]
    record('descriptor_has_two_ipp_usb_interfaces', interfaces=[i['class'] for i in interfaces])

    e.requests.extend([('altset2', setup(1, 11, 0, 2)), ('altset3', setup(1, 11, 0, 3)),
                       ('device_id', setup(0xa1, 0, 0, 0x200, 256))])
    e.run(lambda x: any(n == 'device_id' for g, n, d in x.responses if g == x.generation), resume=True)
    device_id = [d for g, n, d in e.responses if n == 'device_id'][-1]
    assert b'MDL:MG2550s' in device_id and b'CMD:URF' in device_id and b';URF:' in device_id
    assert not e.stalls, e.stalls
    record('set_interface_and_device_id_on_ipp_interface')

    status, headers, body, _ = http(e, 0, 'POST', '/ipp/print', ipp_request(0x000b, 7),
                                    headers=('Content-Type: application/ipp',))
    version, ipp_status, request_id, groups, attrs = ipp_parse(body)
    assert status == 200 and headers['content-type'] == 'application/ipp'
    # IPP USB: Host is echoed as Server and in self-referencing URLs.
    assert headers['server'] == HOST
    assert attrs['printer-uri-supported'] == [(0x45, f'ipp://{HOST}/ipp/print'.encode())]
    assert attrs['printer-more-info'] == [(0x45, f'http://{HOST}/'.encode())]
    assert (version, ipp_status, request_id, groups) == (b'\x02\x00', 0, 7, [1, 4])
    assert attrs['printer-make-and-model'] == [(0x41, b'OpenFW MG2550s')]
    assert attrs['printer-uuid'][0][1].startswith(b'urn:uuid:')
    assert attrs['printer-is-accepting-jobs'] == [(0x22, b'\x00')]
    assert attrs['ipp-versions-supported'] == [(0x44, b'1.1'), (0x44, b'2.0')]
    assert (0x49, b'image/urf') in attrs['document-format-supported']
    assert (0x44, b'V1.4') in attrs['urf-supported'] and (0x44, b'RS300') in attrs['urf-supported']
    assert attrs['printer-resolution-default'] == [(0x32, struct.pack('>IIB', 300, 300, 3))]
    assert attrs['copies-supported'] == [(0x33, struct.pack('>II', 1, 1))]
    operations = {struct.unpack('>I', v)[0] for _, v in attrs['operations-supported']}
    assert {0x02, 0x0a, 0x0b} <= operations
    big = ipp_request(0x0002, 9)
    big = big[:-1] + b'\x49\x00\x0fdocument-format\x00\x09image/urf' + b'\x03' + b'UNIRAST\x00' + b'\x00\x00\x00\x01' + bytes(50000)
    status, headers, body, _ = http(e, 1, 'POST', '/ipp/print', big, headers=('Content-Type: application/ipp',))
    _, ipp_status, _, _, attrs = ipp_parse(body)
    assert status == 200 and ipp_status == 0x0506, (status, ipp_status)
    record('print_job_document_streamed_and_refused', body_bytes=len(big))
    for operation, expected in ((0x0002, 0x0506), (0x0004, 0x0506), (0x000a, 0), (0x0009, 0x0406), (0x0023, 0x0501)):
        _, _, body, _ = http(e, 1, 'POST', '/ipp/print', ipp_request(operation, 8),
                             headers=('Content-Type: application/ipp',))
        version, ipp_status, request_id, groups, _ = ipp_parse(body)
        assert (ipp_status, request_id, groups) == (expected, 8, [1]), (hex(operation), hex(ipp_status), groups)
    # The single IPP Printer object: also reachable at "/" (macOS probes it),
    # never at other paths such as /ipp/faxout.
    assert http(e, 1, 'POST', '/ipp/faxout', ipp_request(0x000b, 9), headers=('Content-Type: application/ipp',))[0] == 404
    status, _, body, _ = http(e, 1, 'POST', '/', ipp_request(0x000b, 9), headers=('Content-Type: application/ipp',))
    assert status == 200 and ipp_parse(body)[1] == 0
    record('ipp_get_printer_attributes', attributes=len(attrs))

    status, headers, body, _ = http(e, 1, 'GET', '/eSCL/ScannerCapabilities')
    assert status == 200 and headers['content-type'] == 'text/xml'
    caps = ET.fromstring(body)
    platen = caps.find('scan:Platen/scan:PlatenInputCaps', NS)
    assert caps.findtext('pwg:MakeAndModel', namespaces=NS) == 'OpenFW MG2550s'
    assert platen.findtext('scan:MaxWidth', namespaces=NS) == '2550'
    assert platen.findtext('scan:MaxHeight', namespaces=NS) == '3507'
    modes = [m.text for m in platen.iterfind('.//scan:ColorMode', NS)]
    resolutions = [int(r.text) for r in platen.iterfind('.//scan:XResolution', NS)]
    formats = [f.text for f in platen.iterfind('.//scan:DocumentFormatExt', NS)]
    assert modes == ['BlackAndWhite1', 'Grayscale8', 'RGB24']
    assert resolutions == [75, 150, 300] and formats == ['image/jpeg', 'image/png']
    record('escl_capabilities', modes=modes, resolutions=resolutions, formats=formats)

    assert scanner_status(e, 0) == ('Idle', None)
    status, headers, body, _ = http(e, 0, 'GET', '/')
    assert status == 200 and b'OpenFW MG2550s' in body
    assert http(e, 1, 'GET', '/eSCL/Nope')[0] == 404
    assert http(e, 1, 'GET', '/nothing')[0] == 404
    assert http(e, 0, 'PUT', '/eSCL/ScannerStatus')[0] == 405
    send(e, 1, b'GET /eSCL/ScannerStatus HTTP/1.0\r\nHost: localhost\r\n\r\n')
    assert receive(e, 1)[0] == 505
    record('status_root_page_and_errors')

    # A generic printer driver writing to the legacy printer interface must
    # never stall: data outside a print-test transfer is consumed and dropped.
    e.legacy += bytes(range(256)) * 9
    e.run(lambda x: not x.legacy, resume=True)
    assert scanner_status(e, 0) == ('Idle', None)
    record('legacy_printer_interface_discards_stray_data', bytes=256 * 9)

    # Full A4/Letter platen, grayscale 75 dpi, POST spans several packets.
    padding = tuple(f'X-Padding-{i}: {"p" * 60}' for i in range(10))
    status, location, _ = create_job(e, 0, settings())
    assert status == 201 and location == f'http://{HOST}/eSCL/ScanJobs/1', location
    assert scanner_status(e, 1) == ('Processing', {'JobUri': '/eSCL/ScanJobs/1', 'JobState': 'Pending',
                                                  'ImagesCompleted': '0', 'ImagesToTransfer': '1'})
    send(e, 0, ('GET /eSCL/ScanJobs/1/NextDocument HTTP/1.1\r\nHost: %s\r\n%s\r\n\r\n'
                % (HOST, '\r\n'.join(padding))).encode())
    status, headers, body, _ = receive(e, 0, budget=3_000_000_000)
    assert status == 200 and headers['content-type'] == 'image/png'
    size = check_image(body, (0, 0, 2550, 3507), 75, False)
    assert http(e, 0, 'GET', '/eSCL/ScanJobs/1/NextDocument')[0] == 404
    assert scanner_status(e, 1) == ('Idle', {'JobUri': '/eSCL/ScanJobs/1', 'JobState': 'Completed',
                                            'ImagesCompleted': '1', 'ImagesToTransfer': '0'})
    record('full_page_grayscale_75dpi', pixels=size, png_bytes=len(body), transfers=len(e.transfers[0]))

    # Colour region, chunked POST with Expect: 100-continue; status served on
    # the other interface while the image stream is held mid-transfer.
    region = (450, 600, 600, 300)
    status, location, interim = create_job(e, 1, settings(mode='RGB24', res=150, region=region),
                                           chunked=True)
    assert status == 201 and location.endswith('/eSCL/ScanJobs/2') and interim == 0
    assert create_job(e, 0, settings())[0] == 503
    e.hold_in.add(0)
    send(e, 0, f'GET /eSCL/ScanJobs/2/NextDocument HTTP/1.1\r\nHost: {HOST}\r\n\r\n'.encode())
    e.run(lambda x: x.ready('in', CONN[0][1]), resume=True)
    assert scanner_status(e, 1)[1]['JobState'] == 'Processing'
    e.hold_in.discard(0)
    status, headers, body, _ = receive(e, 0)
    size = check_image(body, region, 150, True)
    png_bytes = len(body)
    status, headers, body, _ = http(e, 1, 'GET', '/eSCL/ScanJobs/2/ScanImageInfo')
    info = ET.fromstring(body)
    assert info.findtext('scan:ActualWidth', namespaces=NS) == str(size[0])
    assert info.findtext('scan:ActualBytesPerLine', namespaces=NS) == str(size[0] * 3)
    record('rgb_region_150dpi_chunked_post_concurrent_status', pixels=size, png_bytes=png_bytes)

    # Like macOS ippusbd: write the whole request (Expect: 100-continue, body
    # spanning packets) before reading anything back. The interim response
    # must not stop the device from accepting the rest of the body.
    head = f'POST /eSCL/ScanJobs HTTP/1.1\r\nHost: {HOST}\r\nExpect: 100-continue\r\n'
    body = settings(res=300, region=(0, 0, 300, 300)) + b' ' * 600
    e.hold_in.add(1)
    send(e, 1, (head + f'Content-Length: {len(body)}\r\n\r\n').encode() + body)
    e.run(lambda x: not x.to_device[1], resume=True, budget=200_000_000)
    e.hold_in.discard(1)
    status, headers, _, interim = receive(e, 1)
    assert status == 201 and interim == 1
    assert http(e, 0, 'DELETE', '/eSCL/ScanJobs/3')[0] == 200
    assert http(e, 0, 'GET', '/eSCL/ScanJobs/3/NextDocument')[0] == 404
    assert scanner_status(e, 1)[1]['JobState'] == 'Canceled'
    record('expect_100_continue_and_cancel_before_transfer')

    # Cancel mid-stream: the stream ends early, without IEND.
    assert create_job(e, 0, settings(mode='RGB24', res=300, region=(0, 0, 2550, 600)))[0] == 201
    send(e, 0, f'GET /eSCL/ScanJobs/4/NextDocument HTTP/1.1\r\nHost: {HOST}\r\n\r\n'.encode())
    e.run(lambda x: len(x.from_device[0]) - x.cursor[0] > 50000, resume=True, budget=1_000_000_000)
    e.hold_in.add(0)
    assert http(e, 1, 'DELETE', '/eSCL/ScanJobs/4')[0] == 200
    e.hold_in.discard(0)
    status, headers, body, _ = receive(e, 0)
    assert status == 200
    check_image(body, (0, 0, 2550, 600), 300, True, complete=False)
    assert scanner_status(e, 1)[1]['JobState'] == 'Canceled'
    record('cancel_mid_stream_truncates_image', png_bytes=len(body))

    for bad in (settings(mode='RGB48'), settings(res=600), settings(fmt='application/pdf'),
                settings(region=(2550, 0, 100, 100)), b'<scan:ScanSettings><scan:XResolution>abc'
                b'</scan:XResolution></scan:ScanSettings>'):
        assert create_job(e, 1, bad)[0] == 409
    record('unsupported_settings_conflict')

    # Exactly what macOS Image Capture sends for its overview scan: no
    # resolution, format or region. Defaults: 75 dpi, JPEG, whole platen.
    overview = ('<?xml version="1.0" encoding="UTF-8"?><scan:ScanSettings xmlns:scan="%s" '
                'xmlns:pwg="%s"><pwg:Version>2.63</pwg:Version><scan:Intent>TextAndGraphic'
                '</scan:Intent><pwg:InputSource>Platen</pwg:InputSource><scan:ColorMode>'
                'BlackAndWhite1</scan:ColorMode></scan:ScanSettings>' % (NS['scan'], NS['pwg'])).encode()
    status, location, _ = create_job(e, 0, overview)
    assert status == 201, status
    status, headers, body, _ = http(e, 0, 'GET', '/eSCL/' + location.split('/eSCL/', 1)[1] + '/NextDocument')
    assert status == 200 and headers['content-type'] == 'image/jpeg'
    result = check_jpeg(body, (0, 0, 2550, 3507), 75, False, bilevel=True, min_psnr=20.0)
    record('macos_overview_settings_bilevel_jpeg_defaults', result=result, jpeg_bytes=len(body))

    # JPEG in colour and grey; sizes not multiples of 8 exercise edge MCUs.
    for mode, res, region, color in (('RGB24', 150, (450, 600, 1006, 470), True),
                                     ('Grayscale8', 75, (300, 300, 1006, 470), False)):
        status, location, _ = create_job(e, 1, settings(mode=mode, res=res, region=region, fmt='image/jpeg'))
        assert status == 201
        status, headers, body, _ = http(e, 1, 'GET', '/eSCL/' + location.split('/eSCL/', 1)[1] + '/NextDocument')
        assert status == 200 and headers['content-type'] == 'image/jpeg'
        result = check_jpeg(body, region, res, color)
        record(f'jpeg_{mode.lower()}_{res}dpi', result=result, jpeg_bytes=len(body))

    # A job that is never fetched times out (clock advanced in < 2^29 tick steps).
    assert create_job(e, 0, settings())[0] == 201
    for _ in range(13):
        e.counter = (e.counter + 5000 * TICKS_PER_MS) & 0xffffffff
        calls = [0]
        e.run(lambda x: calls.__setitem__(0, calls[0] + 1) or calls[0] > 3, resume=True)
    assert scanner_status(e, 1)[1]['JobState'] == 'Aborted'
    record('unfetched_job_times_out')

    zero_length = sum(t == 0 for c in (0, 1) for t in e.transfers[c])
    info = e.info()
    assert info and info[2] == 2 and info[3] & 4
    before = e.resets
    # The next SETUP (another client's GET_PORT_STATUS) completes in the same
    # poll as the REBOOT status stage; the reboot must not be lost.
    e.eager = 'reboot_boot'
    e.requests.appendleft(('port_status', setup(0xa1, 1, 0, 0x100, 1)))
    e.requests.appendleft(('reboot_boot', setup(0x40, 0x41, 1)))
    e.run(lambda x: x.resets == before + 1 and bool(x.info()) and x.info()[2] == 1
          and bool(x.info()[3] & 1), resume=True)
    assert e.eager is None, 'eager SETUP was not exercised'
    record('management_alive_and_reboots_into_rescue', zero_length_packets=zero_length,
           info_replies=sum(n == 'info' for g, n, d in e.responses))

    # Full speed: 64-byte packets, more boundary cases.
    e = IppUsbHost(image=factory, metadata=metadata, speed=False)
    e.run(app_mode)
    assert config_interfaces(e)[2]['endpoints'] == [(0x02, 2, 64), (0x81, 2, 64)]
    assert http(e, 1, 'GET', '/eSCL/ScannerCapabilities')[0] == 200
    region = (300, 300, 640, 128)
    assert create_job(e, 1, settings(mode='RGB24', res=75, region=region))[0] == 201
    status, headers, body, _ = http(e, 0, 'GET', '/eSCL/ScanJobs/1/NextDocument')
    size = check_image(body, region, 75, True)
    record('full_speed_64_byte_packets', pixels=size)

    # A response ending exactly on a packet boundary must be closed by a
    # zero-length packet, or a real host read would never complete. A 404
    # echoes Host exactly once (as Server), so padding Host sets its length.
    def not_found(host):
        start = e.cursor[1]
        send(e, 1, f'GET /missing HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())
        status, headers, _, _ = receive(e, 1)
        assert status == 404 and headers['server'] == host
        return e.cursor[1] - start
    length = not_found(HOST)
    host = HOST + 'a' * (-length % e.max_packet)
    zero_before = e.transfers[1].count(0)
    boundary = not_found(host)
    assert boundary % e.max_packet == 0, boundary
    assert e.transfers[1].count(0) == zero_before + 1, (zero_before, e.transfers[1][-8:])
    record('packet_boundary_response_ends_with_zero_length_packet', response_bytes=boundary)

    report['all_assertions_passed'] = True
    (APP / 'build/validation_escl.json').write_text(json.dumps(report, indent=2) + '\n')
    print('ALL ESCL ASSERTIONS PASSED', flush=True)


if __name__ == '__main__':
    main()
