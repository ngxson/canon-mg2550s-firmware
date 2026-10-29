# app/patch — patched stock-as-app under our bootloader

Runs the **stock Canon MG2550s application** as an MGAP app at `0x10000`, under
**our** bootloader (both stages overlaid from the frozen boot build and verified
unchanged — we never modify the bootloader), with a small set of verified
byte-patches on the stock image for the behaviours we want to control.

Goal: (1) more control over stock behaviour, (2) keep using our bootloader and
its held-rescue path. It is the stock app, not a mixed open image — do not call
it open firmware.

## Requested behaviours and status

| # | Behaviour | Tonight | Notes |
|---|-----------|---------|-------|
| 3 | USB always attached / polled (not VBUS-sensed) | ✅ built | `stay-awake` @ `0x113f16` |
| — | Reboot-into-rescue + mgmt polling | ✅ built | required by AGENTS.md |
| — | Startup power pulse | ✅ built | matches known-good scan build |
| 4 | Wake-on-USB (AutoPowerOn) | 🔎 verified exists | real stock feature; enable = NVM-field, not a code patch. See PATCHES.md |
| 1 | Remove ink-counter write | ✅ built, emu-passed | ink-level writes dropped (`ink_hook.S`, setter `0x111eda`). HW-boot checked; effect needs a print |
| 2 | Always report ink 100% | ✅ built, emu-passed | ink-level reads return full (`ink_hook.S`, getter `0x111eee`); `INK_FULL` value needs print calibration |
| 5 | Scanner-fault boot bypass | ⏳ deferred | no-op with a working scanner; emu fault-injection next. RE pinned |

Ink discrimination is index-free: a field is an ink level iff `((id>>16)&0xff)==0x1c && ((id>>24)&0x3f)==2` (module 2 / group 0x1c = `inkLevel_0..3`). Every other field read/write is replayed unchanged.

`PATCHES.md` is the ledger: exact addresses, original bytes, and the remaining
RE for the deferred items.

## Build

```sh
ARM_PREFIX="$HOME/.platformio/packages/toolchain-gccarmnoneeabi/bin/arm-none-eabi-" \
  python3 app/patch/build.py
# -> app/patch/build/{candidate.bin,candidate.mgap,manifest.json,shim.*}
```

`candidate.bin` is the full 2 MiB flash image (boot region + MGAP app);
`candidate.mgap` is just the app payload uploaded over USB to `0x10000`.

## Validate (emulator, Docker — always with a timeout)

```sh
docker run --rm --network none -v "$PWD:/project:rw" -w /project \
  --entrypoint bash mg2550s-emu -c \
  "timeout 420 python3 app/stockapp/runtime/validate.py --directory app/patch/build"
```

Expect `passed: true`, `flash_unchanged`, `boot_unchanged`, held-rescue intact,
0 SPI writes.

## Flash (USB, ordinary protected app update)

Protected update to `0x10000` (request `0x43`) — retains both bootloader stages
and their guards. See `tools/` for the USB flasher. Recovery stays available via
held rescue if the app does not come up.
