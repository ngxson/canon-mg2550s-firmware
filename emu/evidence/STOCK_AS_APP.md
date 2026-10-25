# Stock as an app with the recovery bootloader preserved

**Follow-up:** the user subsequently authorized a bootloader extension. The
[generic vector-ABI candidate](../../bootloader/evidence/VECTORS.md) now runs
stock with real flash exception dispatch, without the overlay used below.
That bootloader is now installed and its complete protected region was read
back identically. Reset-time I²C recovery also passed physically. This report
retains the earlier experiment under the unchanged-bootloader constraint;
see the [startup smoke experiment](../../app/stockapp/smoke/README.md) for the
subsequent restricted stock-app work.

2026-10-25. Offline investigation; no printer access, flash, reset, motor command
or physical recovery test. [Reproduction/source](../stockapp/README.md),
[compact checked results](stockapp/summary.json).

## Finding

**The missing-helper failure is fixed in the new experimental layout.** Under
an explicitly modeled, temporary instruction-fetch overlay for the exception
vectors, the candidate reaches stock panel initialization, runs 1,000 RTOS
ticks, and reaches optical-reference entry after the scripted seek/backoff.
The compared peripheral writes, EEPROM transactions, context restores and
motion observations match native stock in order and value.

**This is not yet hardware-runnable stock-as-app.** The actual frozen vectors
send the first SVC to `fault` at `0x4c8`. An app-only vector mechanism and
running-stock management/reboot support remain necessary. No bootloader
modification is proposed or included in this experiment.

This supersedes the earlier inference that the erased helper at `0x100d0`
proved stock-as-app structurally impossible. It does not retroactively validate
the abandoned candidate or its modified stage-0 vectors.

## Image layout and exact changes

The stock input is the pinned 2 MiB dump with SHA-256
`4b032367cff5976aeb426c8723e08d3c431a38e622e245a614791a3e5067e533`.
The local production boot region is
`67fd01b7baf43c7a3c3c770b45b67075e807ccde2c808cfd8d4af08c754d9f9b`;
its first 16 KiB has the production pin `2b68ecfc…`.
These identify local build artifacts, not a new reading of the connected board.

| Flash interval / address | New candidate |
|---|---|
| `0x00000..0x0ffff` | Both production boot stages, unchanged |
| `0x10000..0x1001f` | Normal MGAP v2 header |
| `0x10020` | One ARM branch to the shim's relative position in the RAM copy |
| `0x10024..0x1010f` | Original stock helper bytes |
| `0x10110..0x1014b` | 60-byte CPU handoff shim, in originally erased padding |
| `0x10d00..0x10d93` | Copy of stock's short NOP-delay routines from `0x10000..0x10093` |
| `0x1509c8` | ARM veneer branch redirected from `0x10000` to `0x10d00` |
| Other bytes `0x11000..0x1fffff` | Original stock, unchanged |

The real loader requires entry offset **zero**, even though the header has an
entry-offset field. The first payload branch satisfies that contract. The
builder checks the unused region and the exact Thumb-to-ARM veneer pattern;
it does not blindly rewrite every integer resembling a code address.
This is not an exhaustive proof against unseen indirect calls into the header.

The full payload is `0x1effe0` bytes, copied to
`[0x20020000,0x2020ffe0)`. The shim executes at `0x200200f0`, establishes stock's
SVC/FIQ/IRQ/User stack banks, and enters ARM/User mode at `0x11960`. Stock then
executes at its original flash addresses; its duplicate in app RAM is not a
relocated stock execution image. The staging overlap is exercised in the real
loader model. Stock/app RAM alias behavior is not newly established physically.

## Controlled emulator results

| Scenario | SVCs | IRQs | Result |
|---|---:|---:|---|
| Candidate, real frozen vectors | 1 | 0 | Expected fault-loop stop at `0x4c8` |
| Candidate, vector overlay, delay helpers erased | 15 | 0 | Expected invalid instruction at `0x100d0` |
| Candidate, vector overlay, helpers retained | 878 | 0 | Panel component startup at `0x135768` |
| Native stock and candidate, 1,000 ticks each | 24,249 each | 2,412 each | Requested tick limit at idle |
| Native stock and candidate, scripted home each | 24,141 each | 2,914 each | Optical-reference entry `0xf4c2c`, tick 888 |

The startup/control runs report explicit CPU errors, final PC and handoff state.
The old missing-helper failure is reproduced, so it cannot silently disappear
behind the later positive result.

Differential checks compare complete ordered lists after excluding only the
instruction-count field:

| Observation | Boot: matched records | Home: matched records |
|---|---:|---:|
| All captured MMIO writes | 15,735 | 17,984 |
| Motor-related writes | 106 | 1,167 |
| EEPROM transactions | 100 | 100 |
| Task context restores | 4,559 | 4,955 |
| Additional home register capture | — | 1,179 |
| Motion requests | — | 2 |

All runs preserve backing flash. During the candidate's bounded runtime paths:

- The only executed addresses below `0x10000` are SVC/IRQ vector addresses
  `0x8` and `0x18` (apart from the deliberate frozen-vector fault control).
- There are no CPU data reads below `0x11000`, no flash stores, and no writes
  to the SPI-controller range `0xc0aa0000..0xc0aa0fff`.
- No CP15 instructions are reached. In particular, these paths do not disable
  or invalidate the proposed cache overlay. Other stock paths can do so.

Native stock reads its vector literal pool at `0x28` and `0x34`. The candidate's
modeled direct vector branches avoid those two data dependencies. Native runs
also execute no CP15 instructions in these scenarios.

The separate production-loader test starts at reset, accepts the package,
checks its CRCs and copies **all 2,031,584 payload bytes**. A normal emulated
USB app update performs 31 erases and 496 programming jobs, all at or above
`0x10000`, committing the header block last. Both stages remain byte-identical.
A fresh modeled reset can hold rescue with `STAY`. This corrects the blanket
claim that an app crash inherently makes recovery SPI-only; it does not prove
automatic reboot from a crashed app or revalidate physical recovery.

## Candidate mechanism: a volatile instruction-cache vector line

The [ARM946E-S TRM](https://documentation-service.arm.com/static/5e8e3ee588295d1e18d3aa82),
§2.3.16, documents CP15 cache debug index, instruction tag, and instruction data
access. Figure 2-6 identifies tag validity at bit 4. §3.4 documents instruction
cache segment lockdown. These suggest installing eight direct ARM branches in
the cache line for `0x00..0x1f`, while flash reads still return the bootloader.
Direct branches avoid a literal pool in protected flash. Reset's branch can
remain the production branch to `0x100`.

This is an **inference from the documented CPU facilities**, not measured
Canon-ASIC behavior. A RAM-running installer would need to configure the MPU/
cache attributes, write and read back the line, lock its segment, and verify
real SVC/IRQ dispatch. Cache invalidation/disable must be controlled. Stock has
such routines at `0x1500fc` and `0x150110`, plus lockdown code at `0x150230`.
Hardware reset discards the volatile state; software recovery needs an explicit
cleanup path. Cache residency and this board's implementation remain untested.

Unicorn does not implement this cache. The experiment substitutes **only vector
branch fetches**, preserving backing flash and exception registers. It does not
execute a CP15 cache installer. The matching traces therefore establish that
the runtime can coexist with the preserved flash layout **if** temporary vector
redirection works. They cannot establish that mechanism themselves.

High vectors also require actual accessible memory at their address; creating
a `0xffff0000` mapping in Unicorn would only assume the missing hardware.
No TCM/remap capability is newly established here.

## Remaining work before physical stock-as-app

1. Build an app-only cache/vector probe, initially with a tiny source-defined
   handler and readback results, retaining the existing management services.
   Emulate its software contracts, then physically establish instruction-cache
   replacement, real exception delivery, cleanup, and reset recovery. An
   unsupported CPU operation can fault; that probe must still leave flash intact.
2. Integrate cache setup and preserve the overlay through any cache-management
   operations that the selected stock jobs reach. The current shim bypasses
   stock's native checksum/reset/MPU setup; native cache/timing equivalence is
   not claimed by the present comparison.
3. Add observation hooks and USB/I²C management polling without colliding with
   stock's RTOS, memory allocation, EEPROM controller, or USB driver. Patches
   should live in the app slot and be listed/hash-checked.
4. Redirect stock's restart/update paths (`SVC 7 -> 0x110`,
   `SVC 8 -> 0x11004`, and updater entry below `0x10000`) to our recovery flow;
   audit their predecessors for writes. The normal updater's bounds protect
   installation, but they do not sandbox arbitrary running stock code's MMIO.
   No SPI writes were observed here; this is not proof for all stock jobs.

Optical homing, complete scan/print jobs, electrical behavior and physical
recovery remain outside these results. Stock runtime/RTOS/drivers remain
proprietary dependencies used for observation, to be replaced in the separate
source-built firmware.
