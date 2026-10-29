# app/patch — patch ledger

Stock image: `dumps/mg2550s_w25q16cv_full.bin`,
SHA-256 `4b032367cff5976aeb426c8723e08d3c431a38e622e245a614791a3e5067e533`.
Application code is **Thumb**; file offset == virtual address (XIP from flash,
DRAM variables at `0x18xxxxxx`). All patches are applied by
`emu/stockapp/build.py:build()` via `replace(addr, original_hex, replacement,
purpose)`, which verifies the original bytes before writing — keep that
discipline for every new patch.

Labels: **[V]** verified against the dump / emulator · **[I]** inferred ·
**[TODO]** not yet pinned.

---

## Build config (IMPORTANT)

`app/patch` builds with `register_vectors + runtime + native_startup +
stay_awake + auto_power + ink_clamp`. **`native_startup` is required**: without
it the stock USB stack never comes up (emu: USB state stays 0, no attach; HW:
no enumeration). Every physically-verified stockapp build uses native_startup
(see `emu/evidence/stockapp/bringup/README.md` — "USB initialized and
attached"). A 2026-10-28 build that omitted it did not enumerate on HW.

## Applied patches

| Req | What | Site | Original → Replacement | State |
|-----|------|------|------------------------|-------|
| — | Rescue/restart guards + mgmt polling | `0x154146/62/7e`, `0x11004`, `0x126b2` | see build.py runtime_patches | [V] required (AGENTS.md) |
| — | Native-startup checksum bypass | `0x11020` | `7dcc03fa` → `nop` | [V] required for USB |
| 3 | USB always on (skip clock-down/WFI, stay attached) | `0x113f16` | `10b50400` → `01207047` (`movs r0,#1; bx lr`) | [V] shipped |
| — | Startup Power pulse (match known-good scan build) | `0x12c7e2` | panel-key hook | [V] shipped |
| 1 | No ink-counter write | `0x111eda` | `fff79bfd` → `bl ink_setter_veneer` | [V] built, emu-passed |
| 2 | Ink reads return full | `0x111eee` | `fff791fd` → `bl ink_getter_veneer` | [V] built, emu-passed |
| — | Fake door closed (allow scan with cover open) | `0xcd6cc` | `214840787047` → `00207047c046` (`movs r0,#0; bx lr; nop`) | [V] built, emu-passed |

Fake-door: `0xcd6cc` is the `get AccessCoverCtrl` accessor (`ldrb r0,[0x1807af92]; bx lr`). The cover byte is 1=open / 0=closed (both callers `0xc2da4`/`0xc2de6` do `cmp r0,#1; bne skip` then raise the cover-open error). Forcing the getter to return 0 means the cover-open error is never raised → status stays ready → scans aren't refused with DeviceBusy. The byte is a live sensor value (not EEPROM); the only direct reads of `0x1807af92` are through this getter.

Ink veneers in `ink_hook.S`; discrimination `((id>>16)&0xff)==0x1c &&
((id>>24)&0x3f)==2` (module 2 / group 0x1c = `inkLevel_0..3`). `INK_FULL`
(0xffff) is **not yet calibrated** to the real inkLevel encoding — confirm with
a print (the IL() oracle). The emulator has no USB *host* model, so it cannot
confirm enumeration; `emu_usb_check.py` only shows device-side readiness and is
used as a base-vs-ink differential (base ≡ ink confirmed — ink patch does not
affect USB bring-up).

**CORRECTED (BRIEF_ink_status.md answer, verified against the dump).** The
original clamp was wrong on every count and never fired:
- `inkLevel_0..3` are ids **`0x0103109c..0x0103109f`** (module 1 / group 3),
  not module 2 / group 0x1c. `ink_hook.S` now range-tests these ids.
- inkLevel_0..3 are **4-bit nibbles (0..15) at EEPROM 0x15c/0x15d → full = 15**
  (`0xf`), not percent/100 and not `0xffff` (BRIEF_counters.md note d corrects an
  earlier wrong 100). The IL(...) line prints the raw nibble. `INK_FULL` is 15.
- The report/status reads inkLevel through a **second field API** (registry
  object `0x1878edf4` → universal accessor `0x10d246`), not the vtable getter
  `0x111eea`. So the vtable getter/setter hooks cover only vtable-family
  consumers. The real report is clamped directly:
  - **IL(...) service line** → `il_clamp_veneer` at the formatter `0xbc7f2`
    forces the 4 per-tank bytes to 100 (`ink_report`).
  - **`CTK:` status line** → dropped via `0x3f03a` `0fd5`→`0fe0` (`ink_report`).
  - **`MarkerSupplyAttention`** → status-detail decoder ink codes 6/7/8 set to
    0 at `0x38850`/`0x387b4`/`0x387be` (`ink_detail`).

These are staged as three candidates for one HW session — see
`app/patch/STAGING.md`. The field-API boot trace that first exposed the problem
(`trace_fields.py`, `evidence/field_trace_base.json`: 66 getter calls, 0
inkLevel reads at boot) still stands. **Still open**: the job-block on empty
ink (BRIEF §5), and confirming the real inkLevel/page-count *write* path (likely
`0x10d246`, not the vtable setter) — verify with a print before trusting the
write-drop. No guess-patching — reference-first.

## Verified feature, no code patch needed

**Req 4 — AutoPowerOn (wake on incoming USB job).** Real stock feature [V]:
- Service-mode BJL command `AUTOPOWERON ENABLE|DISABLE` — name `0x1e4ce1`,
  command record `0x156c0c`, numeric dispatch table `0x156adc` **index 7**
  (`0x156af8 → 0x156c0c`); params `ENABLE 0x1e4bc1` / `DISABLE 0x1e4bc8`.
- Driver MPC path `COMMAND-MPC_SetAutoPowerOn`/`Get` → handlers **`0x32088`
  (Set)** / **`0x32114` (Get)**; Set writes a persistent field (index 9 via
  base `0x18039c14`, helper `0x2dd92`).
- Per `patchfw.md`: `AutoPowOn` = UserDblSet module, group 2, byte offset
  `0x0c`, width 1.

Enabling it is an **NVM-field write**, not an app code patch. Options next
session: (a) send `AUTOPOWERON ENABLE` over the service path, or (b) set the
EEPROM byte directly once its absolute address is resolved (see patchfw.md
§2.3 — group-2 → EEPROM base mapping is runtime-built; run
`fw_dump/gpt/auto_powon_probe.py` to capture it). The **soft-off wake consumer**
(who reads AutoPowOn in soft-off, which HW input it gates) is still **[TODO]** —
likely in the low-power domain outside the booted app. Needs a physical test:
enable, soft-off, send print data, observe.

---

## Deferred — pinned RE for next session

### Req 1 — remove ink-counter write  ·  Req 2 — always report ink 100%

Architecture [V]: ink levels are NVM fields reached through a unified
**logical-field API** (vtable-dispatched):
- getter **`0x111eea`** — `r0 = field ID` in, value in `r0`
  (`push {r4,lr}; movs r4,r0; bl 0x111a14; ldr r1,[r0]; ldr r2,[r1,#0x28];
  movs r1,r4; blx r2; pop {r4,pc}`) [V]
- setter **`0x111ed4`** — `r0 = field ID`, `r1 = value`
  (method at `[vtable,#0x24]`) [V]
- accessor hub `0x9cf00..0x9d800` (~103 call sites). [I, from patchfw.md]

Ink field family (from patchfw.md registry, `0x193d94..`): `inkLevel_0..3`,
`inkConsumptionNg_HIGH/LOW_0..3`, `TnkInkDetect_BK/CL` (group 6, off `0x06/0x07`),
`ntank_changecount{Bk,Cl,...}`, `inkConsumptionMg`,
`inkConsumption100Mg_detectOn/Off`. Live RAM accumulators also exist:
`ink_qty_cap_bk_read_only 0x18079c6c`, `_cl 0x18079c70`,
`dot_cnt_cap_* 0x18079d74+`, enable `CNT_ENB_DOTCOUNT 0x1807666a`. [V present]

**Plan (both reqs, one shim hook):** replace the getter `0x111eea` entry with a
`BL` to a shim that, for ink-level field IDs, returns "full" and otherwise tail-
calls the original; and no-op the setter `0x111ed4` for the same IDs so the
decremented/consumed values are never persisted. This is cleaner than hunting
individual dot-count flush sites (patchfw.md §3 agrees).

**[TODO] before building:**
1. Exact ink-level **field IDs** (composed `(module<<24)|(group<<16)|index`) and
   the full-value encoding — run `fw_dump/gpt/auto_powon_probe.py`-style field-API
   hook to log IDs/readers at boot.
2. Decide whether `TnkInkDetect_BK/CL` (the optical empty) must also be clamped —
   if the optical hard-stop is a separate runtime error, clamping only the % is
   not enough.
3. Physical oracle: the service report `IL(BK=.. Y=.. M=.. C=..)` line — needs a
   **print** to confirm, hence deferred past tonight.

### Req 5 — scanner-fault boot bypass

Architecture [V]: an error module literally named **`service_call`**
(string `0x1ea92f`); module descriptor `0x18b8c8 = {name, RAM base 0x180d7c18,
field-table 0x18b6ac}`. Scanner sub-faults are fields in that group:
`scanner_hpsensor_err`(0), `_basic`(1), `_light`(2), `_usecase`(3),
`_fbmotor`(4), `_adfmotor`(5), `_circuit`(6), `_solenoid`(7), `_lock`(8), plus
`warmup_*`, `ScannerIllegalPowerOFF`, `ScannerCoverOpen`. On a self-test failure
the matching bit is set in a **service-call bitmask word at `0x180d7c24`** via a
core read-modify-write accessor at **`0xeff88`** (locked helpers `0x14f494`/
`0x14f4b8`); a fan-out of generated "raise service-call X" wrappers lives around
`0xf0ba0..`. Base `0x180d7c18` is in the runtime status area (next to
`DeviceTimeout 0x180d9401`), **not** in the EEPROM field caches (`0x1878e2xx`) —
so it is RAM-only, recomputed each boot, i.e. not an EEPROM-latched flag. [I]

**Plan:** mask the scanner bits of `0x180d7c24` at the power-on readiness gate
(scoped to scanner — leave head/paper/ink service calls intact), or clear them
immediately after the scanner self-test.

**[TODO] before building:**
1. The exact **readiness gate** that reads `0x180d7c24` and routes to the error
   state (choke points bracketed: `0xeff88` reader/writer, `0xf0ba0..` wrappers;
   the single branch to patch is not yet pinned).
2. The exact scanner **bit mask** within `0x180d7c24`.
3. Emu **fault-injection**: force a scanner sub-fault in the emulator, confirm
   stock refuses readiness, then confirm the patch reaches ready. Cannot be
   exercised with a working scanner, hence deferred.

Also still open: does stock *fully* refuse (no USB at all) or boot to an
error-ready state that still enumerates USB but blocks jobs? That decides
whether the patch is even needed for print-over-USB. Confirm in emu.
