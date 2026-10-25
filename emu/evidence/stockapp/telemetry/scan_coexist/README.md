# Scan with live telemetry — 2026-10-25

Physical evidence collected directly over Mac USB with the installed
telemetry stock app (package SHA-256
`380b98ca115955b8a19d410979a42374b219ced94eb03f5c7c715f28f787461e`).
Three identical scan jobs were run with `tools/stock_scan.py`: 50 × 50 mm at
(10, 10) mm, 150 dpi, RGB, calibration on. The platen held no document, so the
images are blank white platen scans with a dark band at the top edge.
Provenance and hashes are in [manifest.json](manifest.json).

| Run | Telemetry polling | Scan result | Total | Image phase | Max bulk-IN latency |
|---|---|---|---:|---:|---:|
| `scan_baseline` | none | 295 × 295 rows, session closed | 9.17 s | 5.48 s | 33.8 ms |
| `scan_with_telemetry` | 35 EP0 reads / 0.4 s (`--setup-history --interval 0.25`) | same | 8.28 s | 6.91 s | 196.8 ms |
| `scan_with_fast_telemetry` | 2 EP0 reads / 0.1 s (`--interval 0.1`, no history) | same | 7.03 s | 5.65 s | 27.9 ms |

All three jobs completed, closed their session (`AbortSession`/`EndJob`
acknowledged) and produced complete images (mean pixel difference between the
baseline and the polled scan is below one level). The collector never saw a
state other than `[4,1,1,1]` (initialized, attached) and the native error byte
stayed zero before, during and after each job. Total times differ mainly in
calibration: the first job polled `0xda20` eleven times, the warm follow-ups
twice. This is scanner warm-up, not a telemetry effect.

## What this qualifies

- **`tools/stock_scan.py` is physically qualified** for this crop on the
  stock runtime. Full-platen, other resolutions and `stock_print.py` are
  still unqualified physically.
- **Telemetry coexists with scanning.** Both collector rates ran as separate
  processes alongside the scan without USB errors. History polling with 35
  control reads per snapshot does cost bandwidth: the image phase was 26 %
  slower and one bulk-IN read waited 197 ms. History-free polling at 10 Hz
  had no measurable effect on the scan. Prefer history-free polling while a
  job runs; read the SETUP rings before/after instead.

## What the current regions show during a scan

The scan protocol uses only the bulk endpoints, so the SETUP rings record
nothing of it. The single non-telemetry SETUP entry during the job was the
scan tool's own `GET_DESCRIPTOR(string 0)` identity check
(`80 06 00 03 00 00 fe 00`). Region 0 never changed.

The only region-1 fields that react are bytes 1–2, which stock uses as the
indices of its 256-entry message-ID ring (`0x180404ce`, see the telemetry
README). They are 8-bit and alias at 0.4 s sampling, but the 10 Hz
history-free run resolves them. Each telemetry control request adds exactly
four messages; after subtracting those:

| Scan phase | Stock messages |
|---|---|
| Idle, before and after the job (≈ 3 s + 8 s) | 0 |
| StartJob … `0xd920` start scan (0.3 s) | 96 (64 + 26 + 6 per 0.1 s) |
| `0xda20` calibration polling, every 0.25 s | 0 |
| Image transfer, 5.65 s | 2,187 ≈ 387/s, steady 40–44 per 0.1 s, 48–56 near the end |

During image transfer the host issued 362 `0xd420` image requests, of which
361 returned 16-byte empty blocks and 38 returned 8 KiB data blocks. The
message rate scales with the host's command rate, about six messages per
`0xd420` round trip, not with motor or CIS activity. The stock scanner task
therefore exchanges messages per bulk command, and busy-polling the image
command is what drives most of the traffic.

## Limits

- Message counts come from an 8-bit index sampled every ≈ 0.1 s; message IDs
  themselves are not exposed, so this says how many, not which.
- The rings are not atomic; the per-snapshot attribution uses before/after
  indices and may shift an entry by one snapshot.
- No MMIO, IRQ, DMA or motor/CIS state is captured. The next useful
  firmware step is exposing the message-ID rings at `0x180404ce` and
  `0x180406ce` with their indices, which the telemetry README already lists
  as allowlist candidates.
