"""Forwarding check without hardware: a fake proxy serves a register map in-process."""
import json, socketserver, threading, unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock import tunnel, inputs  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BOARD = {0xc0c84e08: 0x1, 0xc0c8d028: 0x0, 0xc0c84688: 0x7, 0xc0c83e18: 0x3, 0xc0c85408: 0x1}


class Fake(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            parts = line.decode().split()
            if parts[0] == 'P': reply = 'PONG'
            elif parts[0] == 'R':
                a = int(parts[1], 16); self.server.reads.append(a)
                reply = BOARD.get(a, 0).to_bytes(4, 'little').hex() if 0xc0c80000 <= a < 0xc0c8e000 else 'E'
            elif parts[0] == 'W':
                self.server.writes.append((int(parts[1], 16), int(parts[2], 16))); reply = 'OK'
            else: reply = 'E'
            self.wfile.write((reply + '\n').encode()); self.wfile.flush()


class TunnelTest(unittest.TestCase):
    def test_gpio_reads_are_forwarded(self):
        eeprom = ROOT / 'emu/inputs/eeprom.bin'
        if not eeprom.exists(): self.skipTest('no EEPROM snapshot in emu/inputs')
        server = socketserver.TCPServer(('127.0.0.1', 0), Fake); server.reads = []; server.writes = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        proxy = tunnel.Proxy('127.0.0.1', server.server_address[1])
        emu = tunnel.TunnelTrace(proxy=proxy, forward=tunnel.parse_ranges('c0c80000-c0c88000,c0c8d000-c0c8e000'),
                                 firmware=ROOT / 'dumps/mg2550s_w25q16cv_full.bin', eeprom=eeprom).boot()
        result = emu.run_tunnel(ticks=120)
        t = result['tunnel']
        self.assertGreater(t['reads'], 20)
        self.assertTrue(all(0xc0c80000 <= a < 0xc0c8e000 for a in server.reads))
        self.assertIn(0xc0c8d028, server.reads)                       # key register polled on the board
        self.assertEqual(t['writes_forwarded'], 0)                    # writes stay local by default
        self.assertEqual(t['errors'], [])
        self.assertIsNone(result['result']['error'])
        print('forwarded reads', t['reads'], 'mean ms', t['mean_read_ms'], 'writes seen', t['writes'])
        server.shutdown()


if __name__ == '__main__':
    unittest.main()
