#!/usr/bin/env python3
# Usage (from the submodule root): escl_bench.py <application.bin or -> <rows at 75 dpi> <out.jpg>
# Power-cycles (webhook), optionally flashes, scans a full-width 75 dpi grey region through
# the macOS ippusbd proxy, saves .jpg/.log/.raw (raw line buffer), power-cycles again.
# Optional env MG2550S_PROBE="<lines>,<drain>" turns the stationary probe experiment on (vendor 0x7d);
# MG2550S_SPEED=<counts/s> sets the continuous travel speed, 17000..30000 (vendor 0x7c);
# MG2550S_RETURN=<counts/s> enables the fast return home at that cruise, 20000..120000 (vendor 0x7e);
# MG2550S_AFE="<exposure eighths>,<gain>" sets the continuous-scan optics (vendor 0x7f).
"""Physical eSCL bench: power-cycle, [flash], scan a region via the macOS proxy, read page timing, power-cycle."""
import json, re, subprocess, sys, time, urllib.request
sys.path.insert(0, 'tools')
import usb.util
from fwctl import wait_device
import os
HOOK = os.environ['MG2550S_POWER_HOOK']   # power-cycle webhook URL (private; see internal memo)
PROBE = [int(x) for x in os.environ.get('MG2550S_PROBE', '0,3').split(',')]   # probe lines, drain events
SPEED = int(os.environ.get('MG2550S_SPEED', '0'))            # 0 = firmware default
RETURN = int(os.environ.get('MG2550S_RETURN', '0'))
AFE = [int(x) for x in os.environ.get('MG2550S_AFE', '0,0').split(',')]
def probe_config():
    d, _ = wait_device('application', timeout=30)
    try:
        if PROBE[0]: d.ctrl_transfer(0x40, 0x7d, PROBE[0], PROBE[1], None, timeout=2000); print('probe', PROBE, flush=True)
        if SPEED: d.ctrl_transfer(0x40, 0x7c, SPEED // 100, 0, None, timeout=2000); print('speed', SPEED, flush=True)
        if RETURN: d.ctrl_transfer(0x40, 0x7e, RETURN // 100, 0, None, timeout=2000); print('return', RETURN, flush=True)
        if AFE[0]: d.ctrl_transfer(0x40, 0x7f, AFE[0], AFE[1], None, timeout=2000); print('afe', AFE, flush=True)
    finally: usb.util.dispose_resources(d)
def power_cycle():
    print('power-cycle', subprocess.run(['curl', '-s', '-m', '20', '-X', 'POST', '-o', '/dev/null', '-w', '%{http_code}', HOOK], capture_output=True, text=True).stdout, flush=True)
def drain():
    d, _ = wait_device('application', timeout=30); out = b''
    for _ in range(600):
        b = bytes(d.ctrl_transfer(0xc0, 0x50, 0, 0, 256, timeout=2000))
        if not b: break
        out += b
    usb.util.dispose_resources(d); return out.decode('latin1')
def proxy():
    p = subprocess.Popen(['dns-sd', '-L', 'OpenFW MG2550s', '_uscan._tcp', 'local.'], stdout=subprocess.PIPE, text=True)
    try: o, _ = p.communicate(timeout=5)
    except subprocess.TimeoutExpired: p.kill(); o, _ = p.communicate()
    return re.search(r'localhost\.:(\d+)', o).group(1)
image, rows, out = (sys.argv[1] if sys.argv[1] != '-' else None), int(sys.argv[2]), sys.argv[3]
power_cycle(); time.sleep(25)
if image:
    print(subprocess.run(['python3', 'tools/fwctl.py', 'flash', image], capture_output=True, text=True).stdout.count('"C0"') and 'flash C0', flush=True)
    time.sleep(15)
drain()
if PROBE[0] or SPEED or RETURN or AFE[0]: probe_config()
base = f'http://localhost:{proxy()}/eSCL'
xml = ('<?xml version="1.0" encoding="UTF-8"?><scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" '
       'xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:Version>2.0</pwg:Version><pwg:ScanRegions><pwg:ScanRegion>'
       f'<pwg:Height>{rows * 4}</pwg:Height><pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>'
       '<pwg:Width>2550</pwg:Width><pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset></pwg:ScanRegion></pwg:ScanRegions>'
       '<pwg:InputSource>Platen</pwg:InputSource><scan:ColorMode>Grayscale8</scan:ColorMode><scan:XResolution>75</scan:XResolution>'
       '<scan:YResolution>75</scan:YResolution><scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt></scan:ScanSettings>').encode()
t0 = time.monotonic()
r = urllib.request.urlopen(urllib.request.Request(base + '/ScanJobs', data=xml, method='POST', headers={'Content-Type': 'text/xml'}), timeout=30)
job = '/eSCL/' + r.headers['Location'].split('/eSCL/', 1)[1]
data = urllib.request.urlopen('http://' + base.split('//')[1].split('/')[0] + job + '/NextDocument', timeout=1200).read()
t1 = time.monotonic()
open(out, 'wb').write(data)
time.sleep(25)
log = drain()
open(out + '.log', 'w').write(log)
end = [l for l in log.splitlines() if l.startswith('[page] end')]
result = {'rows': rows, 'seconds': round(t1 - t0, 2), 'ms_per_row': round((t1 - t0) * 1000 / rows, 1), 'bytes': len(data), 'page_end': end[-1:] }
print(json.dumps(result), flush=True)
print(subprocess.run(['python3', 'app/tools/line_dump.py', '--rows', str(rows), '--probe', str(PROBE[0]), '--out', out + '.raw'], capture_output=True, text=True).stdout.strip(), flush=True)
power_cycle()
