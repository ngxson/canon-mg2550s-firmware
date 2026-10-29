#!/usr/bin/env python3
"""Build `app/patch`: stock-as-app, patched, running under OUR bootloader.

This is the stock Canon application packaged as an MGAP app at 0x10000, with a
small set of *verified* byte-patches applied to the stock image. The first
64 KiB (both bootloader stages) is overlaid from the frozen boot build and is
checked byte-for-byte unchanged by the underlying builder: we never modify the
bootloader.

Patch set actually applied tonight (all physically proven in earlier
stock-as-app runtime work; see app/stockapp/runtime/ and emu/evidence):

  * rescue/restart guards + USB/I2C management polling  -> REQUIRED so the app
    can always reboot into held rescue (AGENTS.md). Not optional.
  * stay-awake  (0x113f16)  -> request #3: skip native clock-down / WFI idle,
    USB stays attached and polled instead of sensed. "USB always on."
  * auto-power  (0x12c7e2)  -> one startup Power pulse through the stock panel
    handler, matching the known-good physically-verified scan build.
  * ink-clamp   (0x111eee / 0x111eda)  -> requests #1 and #2: ink-level field
    reads (module 2 / group 0x1c) return full, ink-level writes are dropped;
    all other fields pass through untouched (app/patch/ink_hook.S). The "full"
    value is not yet calibrated to the real encoding -> confirm with a print.

Requested behaviours still out (see PATCHES.md):

  * #5 scanner-fault boot bypass  -> a no-op with a working scanner, cannot be
    exercised yet; deferred to an emu fault-injection run.

AutoPowerOn (request #4) is a real stock feature (verified: service command
`AUTOPOWERON ENABLE|DISABLE` at dispatch index 7 / table 0x156c0c, and MPC
Set/Get handlers 0x32088/0x32114). Enabling it is an NVM-field change, not a
code patch, so it is documented in PATCHES.md, not built in here.

Usage:
    ARM_PREFIX=.../arm-none-eabi- python3 app/patch/build.py
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                       # canon-mg2550s-firmware
sys.path.insert(0, str(ROOT / 'emu' / 'stockapp'))
import build as stockbuild                   # noqa: E402  (emu/stockapp/build.py)


def main():
    out = HERE / 'build'
    stockbuild.build(
        out.resolve(),
        boot_directory=ROOT / 'bootloader' / 'build' / 'vectors',
        register_vectors=True,
        runtime=True,
        native_startup=True,   # restores stock's native memory/cache setup;
                               # required for stock USB bring-up (every
                               # physically-verified build uses it).
        stay_awake=True,
        auto_power=True,
        ink_clamp=True,
        fake_door=True,
    )
    print('\napp/patch built ->', out)


if __name__ == '__main__':
    main()
