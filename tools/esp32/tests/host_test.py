#!/usr/bin/env python3
"""Host operations must not upload or issue management commands without proof."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from fwctl import Bridge

class FakeBridge(Bridge):
    def __init__(self, replies=(), autowire=True):
        self.replies=iter(replies); self.autowire=autowire; self.sent=[]
        self.port=self; self.writes=[]
    def send(self,line): self.sent.append(line)
    def expect(self,prefix,timeout=10):
        result=next(self.replies)
        if isinstance(result,Exception): raise result
        assert result.startswith(prefix), (prefix,result)
        return result
    def write(self,data): self.writes.append(data)
    def flush(self): pass

class HostTests(unittest.TestCase):
    def test_old_bridge_cannot_load_or_command(self):
        for operation in (lambda b:b.load(b'payload'),lambda b:b.command(3)):
            bridge=FakeBridge(autowire=False)
            with self.assertRaises(RuntimeError): operation(bridge)
            self.assertEqual(bridge.sent,[])
    def test_failed_or_silent_probe_cannot_upload_or_reboot(self):
        for reply in ('WIRING FAILED SDA4 SCL5 PROOF=NONE FLOATING',TimeoutError()):
            for operation in (lambda b:b.load(b'payload'),lambda b:b.load(b'payload',True),lambda b:b.command(3)):
                bridge=FakeBridge([reply])
                with self.assertRaises((RuntimeError,TimeoutError)): operation(bridge)
                self.assertEqual(bridge.sent,['DETECT']); self.assertFalse(bridge.writes)
    def test_verified_swapped_wiring_precedes_load(self):
        bridge=FakeBridge(['WIRING VERIFIED SDA4 SCL5 PROOF=REJECT90 FLOATING','SEND','LOADED FLOATING'])
        bridge.load(b'payload',True)
        self.assertEqual(bridge.sent[0],'DETECT'); self.assertTrue(bridge.sent[1].startswith('LOADBOOT '))
        self.assertEqual(bridge.writes,[b'payload'])
    def test_each_command_revalidates(self):
        reply=['WIRING VERIFIED SDA5 SCL4 PROOF=REJECT90 FLOATING','ARMED','DONE C0 FLOATING']
        bridge=FakeBridge(reply*2)
        bridge.command(1); bridge.command(1)
        self.assertEqual(bridge.sent,['DETECT','COMMAND 1','DETECT','COMMAND 1'])

if __name__=='__main__': unittest.main()
