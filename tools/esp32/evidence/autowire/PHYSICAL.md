# Automatic wiring validation — 2026-10-25

Installed ESP32 application SHA-256:
`0934821328b07d92b110ba3ee92b3c9c48ce28afc41dd794c741080dccd1f812`.
All 216,528 bytes read back identically. Only the ESP application at `0x10000`
was programmed; this test wrote no printer flash and requested no printer reset.

After an ESP-only USB/JTAG CPU reset, startup automatically tested both orders:

```text
PROBE SDA5 SCL4 RX=0 READ=0 EXCHANGES=0 DROP=0
PROBE SDA4 SCL5 RX=3 READ=1 EXCHANGES=1 DROP=0
WIRING VERIFIED SDA4 SCL5 PROOF=REJECT90 FLOATING
```

The valid request/read/result sequence proves the current routing and a
CRC-accepted reply from the printer. Sending `FLOAT` cleared that proof, and an
immediate `ARM` was rejected as `ERROR WIRING_UNVERIFIED`. A separate fresh host
session then repeated detection successfully with the same mapping. Printer
USB INFO afterwards reported a valid, idle application in layout 2.

This resolves the fixed SDA5/SCL4 assumption for the present connection. It
does not establish which wire order was present at earlier tests. The alternate
physical wire order was exercised in the detector simulation; the wires were
not rearranged again for this hardware test. CPU startup, not a user unplug/
replug cycle, was used to test automatic initialization.

These are routing tests, not validation of the independent first-stage flash
writer or the stock app. Evidence: [manifest](manifest.json),
[full readback](readback.json), [startup](startup_physical.json), and
[host retry](host_physical.json).
