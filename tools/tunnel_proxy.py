#!/usr/bin/env python3
"""TCP-to-USB proxy for hardware-in-the-loop emulation of the stock firmware.

  tunnel_proxy.py [--port 7731] [--unlock]

The stock emulator runs in Docker and cannot reach USB, so it connects here.
Protocol: one request per line, one reply per line.
  R <hex address> <bytes>     -> hex bytes, or "E" if the firmware refused
  W <hex address> <hex value> -> "OK" or "E"
  P                           -> "PONG"
Reads and writes go through the app's register tunnel (USB 0x90..0x94), which
enforces its own address allowlist; writes also need --unlock here, which sends
the firmware's unlock key once and locks again on exit. Every access is logged
to stderr with a sequence number so a run can be reconstructed.
"""
import argparse, socketserver, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fwctl import wait_device  # noqa: E402

KEY = 0x7d4e


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        dev = self.server.dev; seq = 0
        for line in self.rfile:
            parts = line.decode().split()
            if not parts: continue
            seq += 1; t0 = time.monotonic()
            try:
                if parts[0] == 'P': reply = 'PONG'
                elif parts[0] == 'R':
                    a, n = int(parts[1], 16), int(parts[2])
                    dev.ctrl_transfer(0x40, 0x90, a & 0xffff, a >> 16, b'', timeout=1000)
                    try: data = bytes(dev.ctrl_transfer(0xc0, 0x91, 0, 0, n, timeout=1000)); reply = data.hex()
                    except Exception: reply = 'E'
                elif parts[0] == 'W':
                    a, v = int(parts[1], 16), int(parts[2], 16)
                    dev.ctrl_transfer(0x40, 0x90, a & 0xffff, a >> 16, b'', timeout=1000)
                    try: dev.ctrl_transfer(0x40, 0x92, v & 0xffff, v >> 16, b'', timeout=1000); reply = 'OK'
                    except Exception: reply = 'E'
                else: reply = 'E'
            except Exception as exc:
                reply = 'E'; print('error', exc, file=sys.stderr)
            print(f'{seq} {line.decode().strip()} -> {reply[:40]} ({(time.monotonic() - t0) * 1000:.1f} ms)', file=sys.stderr, flush=True)
            self.wfile.write((reply + '\n').encode()); self.wfile.flush()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=7731); p.add_argument('--unlock', action='store_true', help='enable forwarded writes (allowlisted in firmware)')
    args = p.parse_args()
    import usb.util
    dev, info = wait_device('application')
    dev.ctrl_transfer(0x40, 0x93, KEY if args.unlock else 0, 0, b'', timeout=1000)
    print('tunnel proxy on port', args.port, 'writes', 'UNLOCKED' if args.unlock else 'locked', file=sys.stderr, flush=True)
    try:
        with socketserver.TCPServer(('0.0.0.0', args.port), Handler) as server:
            server.dev = dev; server.serve_forever()
    finally:
        try: dev.ctrl_transfer(0x40, 0x93, 0, 0, b'', timeout=1000)
        except Exception: pass
        usb.util.dispose_resources(dev)


if __name__ == '__main__':
    main()
