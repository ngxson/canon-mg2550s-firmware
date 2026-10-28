#!/usr/bin/env python3
"""Print an image through the application's IPP job intake over IPP-USB.

Does what macOS does for an AirPrint printer, directly on the bulk endpoints
of the first IPP-USB interface: builds a 300 dpi URF (UNIRAST) page from the
image, submits it with IPP Print-Job, then follows the job through the
engine status (USB 0xa2) and the firmware log until it completes.

    python3 tools/ipp_print.py photo.png                # colour (SRGB24)
    python3 tools/ipp_print.py text.png --grey          # W8, mono band path
    python3 tools/ipp_print.py --status                 # engine status only

Every sheet carries a printed label (job name + time) in the top-left corner.
"""
import argparse, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app' / 'tools'))

PAGE_W, PAGE_H, DPI = 2480, 3508, 300        # A4 at 300 dpi
ENGINE_STATES = ['idle', 'receiving', 'printing', 'completed', 'canceled', 'failed']
STEPS = ['none', 'homing', 'head', 'loaded', 'first', 'band', 'prepared', 'fired', 'advanced', 'ejected', 'homing2']
# Job timing profile (USB 0xa2 wIndex 1/2), device milliseconds unless named as a count.
# Engine phases are disjoint; `other` is the remainder of the job time. Shim motion
# (cr/lf) runs inside the fire/advance/load/eject phases and overlaps the event waits.
PROFILE_ENGINE = ['total', 'convert', 'rows', 'fill', 'fills', 'input-wait', 'prepare', 'fire', 'advance', 'load', 'eject', 'home', 'head', 'bytes', 'other', 'skipped']
PROFILE_SHIM = ['cr', 'cr-moves', 'lf', 'lf-moves', 'wait', 'waits', 'timeout', 'timeouts', 'delay', 'delays', 'raminit', 'prepare', 'finish', 'drain', 'arena', 'errors']


def urf_rows(rows, width, components):
    """Encode rows (bytes of width*components) with the URF line codes."""
    out = bytearray()
    c = components
    white = b'\xff' * c
    for row in rows:
        out.append(0)                                   # line repeat - 1
        x = 0
        while x < width:
            px = row[x * c:(x + 1) * c]
            rest = row[x * c:]
            if rest == white * (width - x):
                out.append(0x80); break
            run = 1
            while x + run < width and run < 128 and row[(x + run) * c:(x + run + 1) * c] == px:
                run += 1
            if run >= 2:
                out.append(run - 1); out += px; x += run
            else:
                start = x; lit = 0
                while x < width and lit < 127 and not (x + 1 < width and row[x * c:(x + 1) * c] == row[(x + 1) * c:(x + 2) * c]):
                    x += 1; lit += 1
                # Literal codes represent 2..128 pixels. One isolated pixel
                # uses a one-pixel repeat (code 0), not the invalid byte 256.
                out.append(0 if lit == 1 else 257 - lit); out += row[start * c:(start + lit) * c]
    return bytes(out)


def urf_document(pages):
    """pages: list of (width, height, components, rows)."""
    doc = bytearray(b'UNIRAST\0' + struct.pack('>I', len(pages)))
    for width, height, components, rows in pages:
        doc += struct.pack('>BBBBIIIIIII', 8 * components, 0 if components == 1 else 1, 1, 4, 1, 0, width, height, DPI, 0, 0)
        doc += urf_rows(rows, width, components)
    return bytes(doc)


def ipp_message(operation, request_id, attrs, data=b''):
    body = struct.pack('>BBHI', 2, 0, operation, request_id) + b'\x01'
    for tag, name, value in attrs:
        body += bytes([tag]) + struct.pack('>H', len(name)) + name + struct.pack('>H', len(value)) + value
    return body + b'\x03' + data


def ipp_parse(data):
    """-> (status, {name: [(tag, value)]})."""
    status = struct.unpack_from('>H', data, 2)[0]
    pos, attrs, name = 8, {}, None
    while pos < len(data):
        tag = data[pos]; pos += 1
        if tag == 3: break
        if tag < 0x10: continue
        nl = struct.unpack_from('>H', data, pos)[0]; pos += 2
        if nl: name = data[pos:pos + nl].decode(); pos += nl
        vl = struct.unpack_from('>H', data, pos)[0]; pos += 2
        attrs.setdefault(name, []).append((tag, data[pos:pos + vl])); pos += vl
    return status, attrs


def print_job_request(document, name, request_id=1, grey=False):
    return ipp_message(0x0002, request_id, [
        (0x47, b'attributes-charset', b'utf-8'), (0x48, b'attributes-natural-language', b'en'),
        (0x45, b'printer-uri', b'ipp://localhost/ipp/print'), (0x42, b'requesting-user-name', b'ipp_print'),
        (0x42, b'job-name', name.encode()), (0x49, b'document-format', b'image/urf')], document)


def page_from_image(path, label, grey, scale):
    from PIL import Image, ImageDraw
    im = Image.open(path).convert('L' if grey else 'RGB')
    canvas = Image.new(im.mode, (PAGE_W, PAGE_H), 255 if grey else (255, 255, 255))
    w = int(PAGE_W * scale); h = int(im.height * w / im.width)
    canvas.paste(im.resize((w, min(h, PAGE_H - 200))), ((PAGE_W - w) // 2, 150))
    ImageDraw.Draw(canvas).text((200, 60), label, fill=0 if grey else (0, 0, 0))
    comps = 1 if grey else 3
    raw = canvas.tobytes()
    rows = [raw[y * PAGE_W * comps:(y + 1) * PAGE_W * comps] for y in range(PAGE_H)]
    return PAGE_W, PAGE_H, comps, rows


def engine_status(dev):
    w = struct.unpack('<16I', bytes(dev.ctrl_transfer(0xc0, 0xa2, 0, 0, 64, timeout=2000)))
    assert w[0] == 0x54525031, hex(w[0])
    f = w[1]
    return {'state': ENGINE_STATES[f & 0xff], 'step': STEPS[f >> 8 & 0xff], 'waiting': bool(f & 0x10000), 'mono': bool(f & 0x20000),
            'page_open': bool(f & 0x40000), 'input_done': bool(f & 0x80000), 'sheet': bool(f & 0x100000), 'dry': bool(f & 0x400000),
            'rows_in': w[2], 'band': w[3], 'bands': w[4], 'segment': w[5] & 0xff, 'segments': w[5] >> 8 & 0xff, 'skipped': w[5] >> 16, 'pages': w[6], 'sweeps': w[7], 'failures': w[8], 'last_fail': w[9],
            'urf': (w[10], w[11], w[12], w[13]), 'ms': w[14], 'routines': w[15]}


def engine_profile(dev):
    """Both profile blocks of the current/last job: {'engine': {...}, 'shim': {...}}."""
    import usb.core
    out = {}
    for index, magic, names, key in ((1, 0x31465250, PROFILE_ENGINE, 'engine'), (2, 0x32465250, PROFILE_SHIM, 'shim')):
        try:
            w = struct.unpack('<17I', bytes(dev.ctrl_transfer(0xc0, 0xa2, 0, index, 68, timeout=2000)))
        except usb.core.USBError:
            return None                                       # package without the profile (stalls wIndex 1/2)
        assert w[0] == magic, hex(w[0])
        out[key] = dict(zip(names, w[1:]))
    try:
        w = struct.unpack('<23I', bytes(dev.ctrl_transfer(0xc0, 0xa2, 0, 3, 92, timeout=2000)))
        assert w[0] == 0x33465250, hex(w[0])
        names = {1: 'cap', 2: 'uncap', 3: 'wipe', 4: 'clean', 5: 'prepare', 6: 'prep+run', 7: 'fire', 8: 'load', 9: 'eject', 10: 'advance'}
        out['routines'] = {names[r]: {'delay_ms': w[1 + r] & 0xfffff, 'delays': w[1 + r] >> 20, 'wait_ms': w[12 + r] & 0xfffff, 'timeouts': w[12 + r] >> 20}
                           for r in range(1, 11) if w[1 + r] or w[12 + r]}
    except usb.core.USBError:
        pass
    return out


def format_profile(p):
    if p is None: return 'no timing profile on this package'
    e, s = p['engine'], p['shim']
    return (f"job {e['total']} ms: fire {e['fire']} (cr motion {s['cr']} in {s['cr-moves']} moves), prepare {e['prepare']} (stock part {s['prepare']}), "
            f"advance {e['advance']} (lf {s['lf']} in {s['lf-moves']} moves), load {e['load']}, eject {e['eject']}, home {e['home']}, head {e['head']}, "
            f"convert {e['convert']} ({e['rows']} rows), fill {e['fill']} ({e['fills']}), input-wait {e['input-wait']}, other {e['other']}; "
            f"shim waits {s['wait']} ({s['waits']}), timeouts {s['timeout']} ({s['timeouts']}), delays {s['delay']} ({s['delays']}), raminit {s['raminit']}, "
            f"finish {s['finish']}, drain {s['drain']}, arena {s['arena']}, errors {s['errors']}, bytes {e['bytes']}, empty phases skipped {e['skipped']}"
            + (f"; per routine {p['routines']}" if p.get('routines') else ''))


def claim_ipp(dev, escl_usb):
    """An IPP-USB connection on whichever 7/1/4 interface the host OS has not claimed (macOS takes one after enumeration)."""
    import usb.core
    last = None
    for intf in dev.get_active_configuration():
        if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) != (7, 1, 4): continue
        try:
            return escl_usb.Connection(dev, intf)
        except usb.core.USBError as e:
            last = e
    raise last


def drain_log(dev):
    out = b''
    for _ in range(64):
        d = bytes(dev.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=1000))
        if not d: break
        out += d
    return out.decode('latin1')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('image', nargs='?', type=Path)
    ap.add_argument('--grey', action='store_true', help='W8 grey page (mono band path) instead of SRGB24')
    ap.add_argument('--scale', type=float, default=0.8, help='image width as a fraction of the page width')
    ap.add_argument('--label', default=None)
    ap.add_argument('--status', action='store_true', help='print the engine status and exit')
    ap.add_argument('--cancel', action='store_true', help='cancel the running job (USB 0xa3 op 2)')
    ap.add_argument('--serial')
    a = ap.parse_args()
    from fwctl import wait_device
    import escl_usb, usb.util
    dev, info = wait_device(serial_number=a.serial)
    try:
        if a.cancel:
            dev.ctrl_transfer(0x40, 0xa3, 2, 0, b'', timeout=1000); print('cancel requested')
        if a.status or a.cancel:
            print(engine_status(dev)); print(format_profile(engine_profile(dev))); print(drain_log(dev)); return
        if not a.image: ap.error('image required')
        label = a.label or ('IPP' + time.strftime('%H%M'))
        page = page_from_image(a.image, label, a.grey, a.scale)
        document = urf_document([page])
        print(f'{label}: {a.image.name} -> URF {len(document)} bytes ({"W8" if a.grey else "SRGB24"} {PAGE_W}x{PAGE_H})')
        conn = claim_ipp(dev, escl_usb)
        drain_log(dev)
        t0 = time.monotonic()
        status, headers, body = conn.request('POST', '/ipp/print', print_job_request(document, label, grey=a.grey), 'application/ipp', timeout=1800000)
        ipp_status, attrs = ipp_parse(body)
        print(f'HTTP {status}, IPP status {ipp_status:#06x}, job-id {attrs.get("job-id")}, job-state {attrs.get("job-state")} after {time.monotonic() - t0:.0f}s')
        print(drain_log(dev))
        if ipp_status != 0: sys.exit('job refused')
        while True:
            s = engine_status(dev)
            print(f'  {s["state"]}/{s["step"]} band {s["band"]}/{s["bands"]} seg {s["segment"]} sweeps {s["sweeps"]} rows_in {s["rows_in"]} pages {s["pages"]} fail {s["last_fail"]} {s["ms"] // 1000}s')
            log = drain_log(dev)
            for l in log.splitlines():
                if '[print]' in l or 'returned' in l or 'timeout' in l: print('   ', l)
            if s['state'] in ('completed', 'canceled', 'failed', 'idle'): break
            time.sleep(3)
        print('done:', s['state'], f'{time.monotonic() - t0:.0f}s')
        print('profile:', format_profile(engine_profile(dev)))
    finally:
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
