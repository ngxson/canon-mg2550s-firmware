# Stock USB printing and scanning from Python

These tools reproduce the working `hobby-printer-esp32` USB protocols directly
from this repository. They use PyUSB/libusb, bypassing CUPS, Image Capture, and
the macOS scanner/print drivers. They require the running stock MG2500 runtime;
no printer/ESP32 firmware change or telemetry patch is needed.

**Status: independently tested against the ESP32 project's C implementation.
`stock_scan.py` is physically qualified on this Mac for a 50 × 50 mm, 150 dpi
crop (three complete scans on 2026-10-25, with and without concurrent
telemetry polling; see
[the scan evidence](../emu/evidence/stockapp/telemetry/scan_coexist/README.md)).
`stock_print.py` is physically qualified for A4 at 300 and 600 dpi, and
full-platen 300 dpi scans also passed (see
[the print evidence](../emu/evidence/stockapp/telemetry/print_coexist/README.md)).
Letter, the `--test-page` pattern and 75/600 dpi scans remain unqualified.** The user's earlier scan/print success was through the
ESP32 host.

## Setup

Run from the `canon-mg2550s-firmware` repository root, in a Python environment
with libusb available:

```sh
python3 -m pip install -r tools/requirements-stock.txt
```

On this development Mac, `/Users/ngxson/.venv/bin/python` already has the needed
PyUSB, NumPy and Pillow packages. Supported host platforms are macOS and Linux.

Connect the printer's USB cable directly to the computer. The ESP32 and computer
cannot simultaneously be its USB host. Close other printer/scanner clients if
the interface is busy. The scripts check `04a9:176d`, stock product name and
interface descriptors before sending jobs. They do not change USB configuration,
detach kernel drivers, reset the device, flash firmware or enter recovery.
This avoids the OS imaging stack but still depends on working libusb access.

## Scan

```sh
python3 tools/stock_scan.py --output scan.png --dpi 150
python3 tools/stock_scan.py --output crop.png --dpi 300 \
  --x 10 --y 10 --width 50 --height 50 --trace scan-timing.jsonl
```

Coordinates and sizes are millimetres from the platen origin. The default is
an A4 rectangle at 150 dpi, RGB, with calibration. Available resolutions are
75, 150, 300 and 600 dpi. Outputs are PNG or binary PPM. Files are never
overwritten, and an incomplete image is not published as a successful scan.

The sequence is XML StartJob/ModeShift, start session with bounded busy retries,
three gamma tables, scan parameters, start scan, calibration polling, chunked
image download, AbortSession and XML EndJob. It removes the native 32-pixel
horizontal alignment padding. Image blocks/lengths/checksums are checked, USB
zero-length packets are handled, and failures/cancellation attempt session
cleanup. Cleanup failures remain visible. A refused StartJob does not close
another client's job. `--timeout` bounds the scan, default 300 seconds.

## Print

Generate a job without opening USB:

```sh
python3 tools/stock_print.py --test-page --output test.prn
python3 tools/stock_print.py --image picture.png --dpi 600 --paper Letter \
  --output picture.prn
```

Explicitly print one page:

```sh
python3 tools/stock_print.py --test-page --send --trace print-timing.jsonl
python3 tools/stock_print.py --image picture.png --dpi 300 --paper A4 --send
```

The image is fitted and centered inside the printable area, preserving its
aspect ratio. Supported modes are A4/Letter, plain paper, 300/600 dpi. At 300 dpi
CMYK has one bit per pixel; at 600 dpi C/M use three levels packed into two bits,
and Y/K use one bit. Borders, gamma/dot gain, dithering, PackBits compression,
eight-line CMYK bands, page geometry and IVEC wrappers follow the ESP32 C code.
The small generated test pattern is new; it is not the ESP32's full test-page
design. PDF interpretation, AirPrint/IPP and multi-page jobs are outside this
port's scope.

The bulk-IN backchannel is drained concurrently with output, polled every
250 ms once a reply is complete, as the ESP32 does: the stock printer answers
every bulk-IN read with a fresh ~1 KB status, and a tight loop pulled about
1,500 replies per second from the real printer. StartJob and ModeShift
acknowledgements are awaited for five seconds; like the ESP32, missing optional
acknowledgements are reported and the job continues. The printer NAKs bulk OUT
for as long as it takes to print or until paper is loaded; the tool waits
through that for up to `--stall-timeout` (default 900 s, ESP32: unbounded),
printing port status every 15 s, and resumes each 16 KiB chunk from exactly the
byte count libusb reports, so nothing is ever replayed. EndJob gets 180 seconds
(`--finish-timeout`); without its acknowledgement the tool reports that data
was sent but physical completion is unverified. Failed jobs attempt the
printer-class SOFT_RESET cancellation used by the ESP32; this is not a firmware
reboot. The first physical attempt failed because of an 8 s OUT timeout and the
back-channel flood; see
[the attempt record](../emu/evidence/stockapp/telemetry/print_coexist/attempt1_failed/README.md).

Our scan/print processes share an exclusive per-device job lock. That lock
does not control unrelated OS clients. Telemetry does not acquire the job lock.

## Evidence and emulator use

Both tools accept `--trace new-file.jsonl`. These files contain timestamps,
command IDs, transfer lengths and outcomes, **not document or image bytes**.
An explicitly saved `.prn`, `.png` or `.ppm` naturally contains the document.
Host command timing provides reproducible stimuli for stock telemetry and later
emulator replay; it is not itself an internal MMIO/IRQ trace.

[Validation report](evidence/stock_usb_validation.json) records exact Python and
C reference hashes. It covers:

- Seven scans against the independently compiled C fake scanner: 75–600 dpi,
  odd crop origins, a narrow platen-edge crop, a full platen scan, busy retries
  and abort/retry recovery. Every command byte and every output pixel matches
  the real C scan implementation on the same synthetic device.
- Failed image blocks, cancellation, invalid checksums, truncated images,
  retryable IN timeouts, zero-length packets, and cleanup.
- Thirty PackBits comparisons plus four full-page streams (A4/Letter at
  300/600 dpi), byte-identical to the C encoder using common RGB fixtures.
  The ESP32 project's separate decoder also accepts all four streams.
- Fragmented print acknowledgements, concurrent backchannel handling, and
  cancellation without retransmission after an OUT timeout.

Reproduce without opening USB or modifying the reference checkout:

```sh
python3 tools/tests/validate_stock_usb.py \
  --reference /Users/ngxson/work/hobby-printer-esp32 \
  --report tools/evidence/stock_usb_validation.json
```

The reference checkout and a C compiler are only test dependencies. Runtime
protocol code is Python under `tools/stockusb/`; the ESP32 project is not needed
to run the tools. No firmware changes were made for this port. These tests
establish protocol equivalence on the tested fixtures, not USB electrical
behavior or physical scan/print success from this Mac.
