# Continuous colour software fixes and startup comparison

2026-10-27. Offline helper work; no printer access or flashing. The two
source fixes in `software_fixes.patch` are already applied. Do not apply
the patch a second time. The hardware owner's handoff is
[SCANNER_COLOUR_HANDOFF.md](../../../../fw_dump/gpt/SCANNER_COLOUR_HANDOFF.md).

| Evidence | Result |
|---|---|
| `baseline.log` | Original ARM build rejects 300 dpi jobs, HTTP 503 |
| `admission_only.log` | Admission fixed; 531/540 bytes of the first RGB row are incorrectly zero |
| `fixed.log` | Both fixes; exact 300 dpi output pixels pass |
| `escl_suite.log` | Existing scanner/eSCL suite passes |
| `colour_suite.log`, `colour_continuous.json` | Exact RGB at 75/150/300 dpi, ring reuse at 230 rows, invalid routing rejected |
| `startup_comparison.json` | 133 stock-written SCNIO fields compared before capture; only moving-mode `+11c/+164` differ; ordered stock cold-start and application prepare writes retained |
| `validation_manifest.json` | Tested image/compiler and source hashes |
| `baseline_sha256.json` | Source hashes before helper changes |

The image in this validation is an isolated standalone ARM application built
from the owner's current sources. It is not a deployed hybrid image. DMA,
motion feedback and optics in the tests are synthetic; passing does not
prove physical RGB channel order, calibration, or cold AFE startup.

To reproduce without overwriting the owner's build directory, from the
firmware repository root:

```sh
python3 app/build.py --variants scanner --output /tmp/scan-colour-build
docker run --rm --network none \
  -v "$PWD":/project:ro -v /tmp/scan-colour-build:/build:rw \
  mg2550s-emu app/tests/validate_colour_continuous.py \
  --build /build --output /build/colour_continuous.json
```

The stock side of the startup comparison comes from the unchanged pinned
image executed by `emu/stockapp/scanner_fixture.py`, case
`p0-three-components`. The application side calls compiled
`scanner_line_prepare(3,32,128,108360)` after an emulated boot. The comparison
does not include every register in the moving stock path. The cold-start
differences are a reference for the next controlled change, not a proven
cause of the physical raw-zero fault.
