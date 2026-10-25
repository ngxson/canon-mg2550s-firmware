# Print attempt 1 — failed, 2026-10-25 20:57 UTC

Both planned jobs failed before the retry documented in the parent folder.
Printer: stock telemetry app `380b98ca…461e`, direct Mac USB. Sheet:
[test_sheet.png](../test_sheet.png) (rulers, patches, ramps, line weights).

| Job | Polling | Outcome |
|---|---|---|
| 300 dpi A4, `print_300_baseline_trace.jsonl` | none | StartJob acked after 4.3 s, ModeShift after 1.1 s. 40 KiB of raster accepted at once, then 8 KiB in 1.9 s, 8 KiB in 5.2 s, then only 3,584 of 8,192 bytes within the 8 s OUT timeout. The tool raised "Short USB OUT" at 21 s and sent the printer-class SOFT_RESET. |
| 600 dpi A4, `print_600_telemetry_trace.jsonl` | 10 Hz, history-free | Started 19 s after the reset. The StartJob OUT itself timed out after 8 s; nothing was accepted. SOFT_RESET sent. |

Telemetry stayed `[4,1,1,1]` with native error zero throughout, including
the 7 minutes of 10 Hz polling after the failure. The printer afterwards
reported printer-class port status `0x18` (selected, no error, paper
present), `STA:10`, and ivec status `idle` with `MarkerSupplyAttention`
(colour cartridge `LOW`).

## Cause

Two deviations from the working ESP32 host, found by reading
`hobby-printer-esp32/src/usb_printer.c`:

1. **OUT timeout.** The ESP32 comments: "The printer NAKs while its buffer is
   full, which can last as long as it takes to print (or until paper is
   loaded)", and waits without bound, logging port status every 15 s. The
   Python port gave each 8 KiB write 8 s and cancelled the job on expiry.
   The decelerating acceptance (40 KiB, then 8 KiB per 2–5 s) is a printer
   consuming data at print speed, so the first job was most likely printing
   when it was cancelled. The second job met a printer still finishing that
   cancellation.
2. **Backchannel flood.** The stock printer answers every bulk-IN read with a
   fresh ~1 KB `GetStatusResponse`; the ESP32 polls every 250 ms during a job
   (`IN_POLL_JOB_MS`, "don't read back-to-back"). The Python drain read in a
   tight loop: 35,596 IN reads (13.9 MB) in 21 s during job 1 and 12,508 in
   8.4 s during job 2. The 10 Hz telemetry showed about 1,750 stock messages
   per second during that flood, roughly one per IN transaction, and nothing
   else; so the telemetry message counter is a direct measure of host-driven
   load.

The SETUP ring after job 1 holds the tool's `GET_DESCRIPTOR(string 0)`
checks and the one printer-class `SOFT_RESET` (`21 02 00 00 01 00 00 00`).

## Fix applied before the retry

`tools/stockusb/transport.py::write_stalling` sends through libusb directly so
each 1 s slice reports exactly how many bytes were accepted; progress continues
from there and nothing is ever resent. The wait is bounded by
`--stall-timeout` (default 900 s) and port status is reported every 15 s.
`tools/stockusb/printing.py` now polls the back channel every 250 ms once a
reply is complete and sends 16 KiB chunks like the ESP32. Offline tests in
`tools/tests/validate_stock_usb.py` cover exact resume after NAK slices, the
deadline, pipe errors and the polling rate; all 20 groups pass.

Paper consumption by job 1 is a user observation recorded in the parent README.
