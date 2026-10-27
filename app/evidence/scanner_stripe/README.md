# Scanner central bright stripe — 2026-10-27

User observation: the broad central vertical bright band in our scans is not
on the paper. Print-quality repair is already physically verified on package
`62517f31…`; this is a separate calibration correction.

## Cause and correction

The white-reference window used travel units 20..89 from mechanical home.
Raw full-width captures show a black registration notch at sensor columns
approximately 1180..1415, remaining dark until about travel unit 30. Including
it in the average lowers the central white reference and raises the gains
there, producing the bright stripe at output columns roughly 1157..1389.

The correction samples units **40..75** (`STRIP_FROM=40`, `STRIP_TO=76`). This
interval is white over the whole sensor and ends before the trailing falloff.
It uses 36/18/9 reference rows at 300/150/75 dpi. The dark reference, document
origin, component order, gamma and motor control are unchanged.

The calibration-only candidate `bf22e2de…` differs from the verified print
image by only two payload bytes plus package CRCs (`package_diff.json`). It
was not flashed. The required scanner pipeline check then exposed an existing
HTTP parsing delay: 23,528 instructions between scanner motor polls, over
the conservative 16,000 limit. `ippusb.c` now calls `app_yield()` every 64
consumed bytes. This services scanner motion/acquisition without recursively
entering IPP parsing. The bound is retained, not relaxed.

The installed package includes both changes and retains the print repairs:
`602fe9f47cef1b47a05794f6c5e7e9b5d4c12700060bd3ee17e7e2305d3a0fa5`, archived
in `app/hybrid/build/scanner_stripe_fix/`. All scanner, RGB, print and 17-suite
regressions passed before flashing. Protected application flash returned C0,
startup passed, and the same-sheet physical scan confirms the correction.
Both recovery stages were retained.

## Evidence

- `acc_white.bin`, `acc_dark.bin`, `cal_scale.bin`, `acc_nw.bin`, `acc_nd.bin`:
  references/gains from the completed QI272229B scan. Every central gain is
  valid/nonzero; fallback conversion is not the cause.
- `document_raw_800_807.bin`: eight retained blank-paper RGB rows. The raw
  paper does not have the reference's abrupt central step. Metadata includes
  ring/component coordinates; extraction accounts for 0x1440 row boundaries
  that are not always 256-byte aligned.
- `reference_rows_0_171.bin`: a short 300-dpi RGB scan retaining all reference
  rows without ring wrap. `reference_notch.png` visualizes the actual raw
  strip/notch geometry. At sample 1200, the early central reference is dark;
  by unit 40 the whole strip is white. Units 40..75 precede its fading edge.
- `fix.patch` records the source and fixture changes relative to the print-fix
  state. `manifest.json` records the installed package's source hashes.

## Validation

The emulator optical fixture now includes the measured notch independently
of the firmware's chosen window. A 300-dpi midtone region crosses both notch
edges so clipping cannot hide the defect. The old package fails at input
x=1157: RGB 187/185/175 versus expected 173/172/163 (`notch_before.log`).
The new window passes that case, exact RGB at all three resolutions, colour
ring reuse and a full 75-dpi page (`colour_validation.log/json`).
The first diagnostic probe used saturated whites and could hide the error;
it was replaced with midtones for the recorded failing regression.

The scanner-source eSCL suite passes, including cancellation and automatic
return home. Its maximum motor-service gap is now 5,814 instructions, below
the unchanged 16,000 limit (`scanner_pipeline.log`). Both print raster/servo
checks pass (`print_regression.log`). The final standard 17-suite regression
also passed (`regression.log`).

## Physical verification

The same QI272229B sheet was left on the glass and rescanned through ordinary
eSCL after the update: **2480 × 700 RGB at 300 dpi, 22.934 s**, completed with
the expected end-of-document response (`after_rgb300.log/png`). No new print
or sheet repositioning was needed. The central bright band has disappeared;
the printed K/C/M/Y bars remain continuous.

`compare_scans.py` measures the same blank-paper region in both captures.
Across rows 300..649, the centre (x=1175..1349) exceeded the mean of its two
flanks by **18.286 levels before and 0.732 after**, on the 0..255 scale.
`scan_comparison.json` retains the RGB values and exact bounds;
[before_after.png](before_after.png) shows identical uncorrected crops.
This verifies this stripe correction, not general photographic calibration.

`final_health.json` confirms scanner home/idle, scanner power and motor drive
off, no active owners, both RAM canaries intact, and USB management responsive.
The idle timer entered standby normally. Reference counts are 36 white and
16 dark rows as intended at 300 dpi. The final scan is
`after_rgb300.png` (not kept in git).
