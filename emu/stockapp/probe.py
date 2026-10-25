#!/usr/bin/env python3
"""Differential stock/app experiments with an EXPLICIT vector-fetch assumption.

Unicorn has no ARM946 I-cache model. `--vectors overlay` substitutes only the
eight vector branch fetches, without writing backing flash. It does NOT prove
CP15 cache installation, cache residency, electrical timing or hardware safety.
The default `flash` executes the real frozen bootloader vectors and fails.
"""
import argparse
import ctypes
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT/'emu'))
from stock import startup, boot, feedback
from stock.scheduler import TickScheduler
from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_PROT_ALL
from unicorn.arm_const import (UC_ARM_REG_CPSR, UC_ARM_REG_SP, UC_ARM_REG_PC,
    UC_ARM_REG_R0, UC_ARM_REG_LR)

VECTORS = {4: 0x11800, 8: 0x11804, 12: 0x11808, 16: 0x1180c,
           20: 0x11948, 24: 0x11810, 28: 0x11000}


def check(condition, message):
    if not condition:
        raise ValueError(message)


class Inspect:
    def __init__(self, *, candidate=None, vectors='flash', remove_helpers=False,
                 from_reset=False, boot_build=None,
                 **kwargs):
        super().__init__(**kwargs)
        self.stock_path = self.firmware_path
        self.stock_image = self.image
        self.candidate = candidate
        self.registered_vectors = False
        self.runtime_vectors = None
        self.native_startup = False
        self.native_cpu = False
        self.native_boot_phase = False
        self.native_ram_clears = []
        self.vector_model = vectors
        self.remove_helpers = remove_helpers
        self.low_pcs = Counter()
        self.low_reads = Counter()
        self.vector_fetches = Counter()
        self.cp15 = Counter()
        self.spi_writes = []
        self.handoff = None
        self.loader_handoff = None
        self.prefix = self.image[:0x10000]
        self.cpu_flash_stores = []
        self.uc.hook_add(UC_HOOK_MEM_READ, self.low_read, begin=0, end=0x10fff)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self.flash_store, begin=0, end=0x1fffff)
        if candidate:
            self.image = candidate.read_bytes()
            check(len(self.image) == 0x200000, 'candidate must be exactly 2 MiB')
            manifest = json.loads((candidate.parent/'manifest.json').read_text())
            self.registered_vectors = manifest.get('registered_vectors',False)
            self.native_startup = manifest.get('native_startup',False)
            self.native_cpu = manifest.get('native_cpu',False)
            if manifest.get('runtime') or manifest.get('startup_smoke'):
                symbols=manifest['shim_symbols']
                self.runtime_vectors=[int(symbols[n],16) for n in
                    ('smoke_undef','smoke_svc','smoke_pabort','smoke_dabort','smoke_reserved')]+[0x11810,int(symbols['smoke_fiq'],16)]
                if manifest.get('irq_snapshot'):self.runtime_vectors[5]=int(symbols['stockapp_irq'],16)
            check(not self.registered_vectors or vectors=='flash',
                  'registered-vector tests must execute the real flash vectors')
            check(hashlib.sha256(self.image).hexdigest() == manifest['candidate_sha256'],
                  'candidate hash mismatch')
            self.firmware_path = candidate
            self.prefix = self.image[:0x10000]
            self.uc.mem_write(0, self.image)
            self.uc.mem_map(0x20000000, 0x800000)
            fields = struct.unpack_from('<8I', self.image, 0x10000)
            check(fields[:2] == (0x5041474d, 2) and fields[3:5] == (0x20020000, 0),
                  'incorrect app header')
            payload = self.image[0x10020:0x10020+fields[2]]
            check(len(payload) == fields[2], 'truncated payload')
            self.uc.mem_write(0x20020000, payload)
            if remove_helpers:
                self.uc.mem_write(0x10094, b'\xff'*(0x10110-0x10094))
            # Remove the parent's synthetic stock stack setup. The actual shim
            # must initialize every stack used by SVC/IRQ and normal stock.
            for mode in (0xd1, 0xd2, 0xd3, 0xdf):
                self.uc.reg_write(UC_ARM_REG_CPSR, mode)
                self.uc.reg_write(UC_ARM_REG_SP, 0)
            self.uc.reg_write(UC_ARM_REG_CPSR, 0xd3)
            self.uc.reg_write(UC_ARM_REG_SP, 0x207ff000)
            self.uc.reg_write(UC_ARM_REG_PC, 0x20020000)
            if from_reset:
                check(boot_build is not None,'--from-reset needs --boot-build')
                sys.path.insert(0,str(ROOT/'bootloader/tests'))
                from model import Model
                metadata=json.loads((boot_build/'manifest.json').read_text())
                check((boot_build/'boot_region.bin').read_bytes()==self.prefix,
                      'loader build differs from candidate prefix')
                metadata['symbols'].pop('application',None)
                loader=Model(image=self.image,metadata=metadata)
                loader.run(lambda m:0x20020000 in m.entered,budget=800_000_000)
                check(bytes(loader.uc.mem_read(0x20020000,len(payload)))==payload,
                      'real loader payload copy differs')
                # Preserve the complete CPU context (including banked registers),
                # both modeled RAM regions and all stored MMIO values at entry.
                # The stock peripheral model takes over at that boundary; no
                # firmware function, exception vector or return is replaced.
                self.uc.mem_write(0x20000000,bytes(loader.uc.mem_read(0x20000000,0x800000)))
                self.uc.mem_write(0xe0000000,bytes(loader.uc.mem_read(0xe0000000,0x10000)))
                self.uc.context_restore(loader.uc.context_save())
                self.registers.update(loader.regs)
                self.loader_handoff=dict(cpsr=hex(self.uc.reg_read(UC_ARM_REG_CPSR)),
                    sp=hex(self.uc.reg_read(UC_ARM_REG_SP)),pc=hex(self.uc.reg_read(UC_ARM_REG_PC)),
                    payload_bytes=len(payload),stored_mmio_registers=len(loader.regs),
                    reset_summary=loader.summary(),
                    limit=('Peripheral model changes at app entry; native-startup DRAM aliases are explicit model inputs; cache timing is unmodeled'
                           if self.native_startup else 'Peripheral model changes at app entry; RAM aliases and cache timing remain unmodeled'))
            if self.native_startup:
                # Explicit DRAM aliases; retain the loader's complete payload
                # and RAM contents, rather than handing stock fresh zero RAM.
                initial=bytes(self.uc.mem_read(0x20000000,0x800000))
                self.shared_dram=ctypes.create_string_buffer(initial,0x800000)
                for address,length in ((0x10000000,0x100000),(0x18000000,0x800000),(0x20000000,0x800000)):
                    self.uc.mem_unmap(address,length)
                    self.uc.mem_map_ptr(address,0x800000,UC_PROT_ALL,ctypes.addressof(self.shared_dram))
                # Explicit model inputs select stock's configured-memory path;
                # these are not fresh physical measurements of the board straps.
                self.registers[0xc0c84e18]=1
                self.registers[0xc0c84e28]=1
                self.native_boot_phase=True
        self.execution_image = bytes(self.uc.mem_read(0, 0x200000))

    def low_read(self, cpu, access, address, size, value, user):
        self.low_reads[(cpu.reg_read(UC_ARM_REG_PC), address, size)] += 1

    def flash_store(self, cpu, access, address, size, value, user):
        self.cpu_flash_stores.append(dict(pc=hex(cpu.reg_read(UC_ARM_REG_PC)),
            address=hex(address), size=size, value=hex(value)))

    def code(self, cpu, address, size, user):
        cpsr = cpu.reg_read(UC_ARM_REG_CPSR)
        if address < 0x11000:
            self.low_pcs[address] += 1
        if address == 0x11960 and self.handoff is None:
            self.native_boot_phase=False
            self.handoff = dict(cpsr=hex(cpsr), sp=hex(cpu.reg_read(UC_ARM_REG_SP)),
                               r0=hex(cpu.reg_read(UC_ARM_REG_R0)))
        # Record reached CP15 instructions; conditional entries are annotated
        # by opcode, not claimed as completed hardware operations.
        if size == 4 and not cpsr & 0x20:
            w = int.from_bytes(cpu.mem_read(address, 4), 'little')
            if w & 0x0f000f10 == 0x0e000f10:
                self.cp15[(address, w)] += 1
        super().code(cpu, address, size, user)
        if self.candidate and address == 0x4c8:
            self.stop_reason = 'frozen bootloader fault vector reached'
            cpu.emu_stop()
        if self.candidate and self.vector_model == 'overlay' and address in VECTORS:
            # Model just the effect of an instruction-only B at this address.
            # Backing flash and all general/banked registers remain untouched.
            self.vector_fetches[address] += 1
            cpu.reg_write(UC_ARM_REG_PC, VECTORS[address])

    def read(self, cpu, offset, size, user):
        address=startup.PERI+offset
        if self.native_boot_phase and address==0xc0c82008:
            self.registers[address]=(self.registers.get(address,0)+96)&0xffffffff
        value=super().read(cpu,offset,size,user)
        # Same lock-ready input required by source boot_c(). Native memory
        # initialization enables a later power/clock path that reads it again.
        if self.native_startup and address==0xc0c82404:value|=1
        # Stock 0x116b90/0x116bc4 waits for disable/enable acknowledgement.
        # Model the status following its control write, with no analog delay.
        if self.native_startup and address==0xc0c82408:
            value=(value&~1)|int(bool(self.registers.get(0xc0c00204,0)&0x08000000))
        return value

    def write(self, cpu, offset, size, value, user):
        address = startup.PERI + offset
        if 0xc0aa0000 <= address < 0xc0aa1000:
            self.spi_writes.append(dict(pc=hex(cpu.reg_read(UC_ARM_REG_PC)),
                address=hex(address), value=hex(value)))
        result=super().write(cpu, offset, size, value, user)
        if self.native_boot_phase and address==0xc0c21018 and value==1:
            start=self.registers.get(0xc0c2100c,0)
            length=self.registers.get(0xc0c21014,0)
            check(self.registers.get(0xc0c21000)==0x1000000,'unexpected native RAM engine command')
            check(0x10000000<=start<0x10800000 and 0<length<=0x10800000-start,'native RAM clear out of bounds')
            self.uc.mem_write(start,bytes(length))
            self.registers[0xc0c2101c]=1
            self.native_ram_clears.append({'start':hex(start),'bytes':length})
        return result

    def result(self):
        result = super().result()
        result['firmware_bytes_unchanged'] = bytes(self.uc.mem_read(0, 0x200000)) == self.execution_image
        return result

    def audit(self):
        registration_intact=None
        if self.registered_vectors:
            targets=self.runtime_vectors or [VECTORS[a] for a in sorted(VECTORS)]
            expected=[0x3158564d,0xcea7a9b2,1]+targets+[v^0xffffffff for v in targets]
            registration_intact=list(struct.unpack('<17I',self.uc.mem_read(0xe0000880,68)))==expected
        return dict(
            candidate=bool(self.candidate), vector_model=self.vector_model,
            registered_vectors=self.registered_vectors,
            registration_intact=registration_intact,
            native_startup=self.native_startup,native_ram_clears=self.native_ram_clears,
            stock_sha256=hashlib.sha256(self.stock_image).hexdigest(),
            execution_image_sha256=hashlib.sha256(self.execution_image).hexdigest(),
            protected_prefix_sha256=hashlib.sha256(self.prefix).hexdigest(),
            protected_prefix_unchanged=bytes(self.uc.mem_read(0, 0x10000)) == self.prefix,
            full_image_unchanged=bytes(self.uc.mem_read(0, 0x200000)) == self.execution_image,
            handoff=self.handoff, vectors={hex(a): n for a, n in self.vector_fetches.items()},
            loader_handoff=self.loader_handoff,
            low_pcs={hex(a): n for a, n in sorted(self.low_pcs.items())},
            low_reads=[dict(pc=hex(p), address=hex(a), size=s, count=n)
                       for (p, a, s), n in sorted(self.low_reads.items())],
            cp15=[dict(pc=hex(p), opcode=hex(w), count=n)
                  for (p, w), n in sorted(self.cp15.items())],
            cpu_flash_stores=self.cpu_flash_stores, spi_writes=self.spi_writes,
            assumptions=[
                ('Actual loader executes from reset; CPU context, RAM and stored MMIO transfer at app entry.'
                 if self.loader_handoff else
                 'Candidate starts at app handoff after full MGAP payload copy; separate loader validation starts at reset.'),
                ('Real flash vector wrappers, C validator and app registration execute; no vector-fetch substitution.'
                 if self.registered_vectors else
                 'Vector overlay, if enabled, is an explicit instruction-fetch substitution, NOT a validated hardware cache.'),
                'Inherited stock peripheral/IRQ/EEPROM/encoder models remain synthetic.',
                ('Native application RAM/MPU/cache startup executes; incompatible boot-block checksum is bypassed. DRAM aliases, memory straps and DMA clear completion are explicit model inputs.'
                 if self.native_startup else 'Native MPU/cache setup executes before the C runtime; native RAM preparation is bypassed.'
                 if self.native_cpu else 'Shim bypasses native checksum/reset/MPU/cache setup.'),
                'Cache behavior and electrical timing are not modeled.',
                'No custom USB/I2C management poll has yet been integrated into stock.',
                'No physical device access or firmware update is performed.'])


class Startup(Inspect, startup.Startup):
    pass


class Boot(Inspect, boot.BootTrace):
    pass


class Home(Inspect, feedback.Recheck):
    pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('scenario', choices=('startup', 'boot', 'home'))
    p.add_argument('--candidate', type=Path)
    p.add_argument('--vectors', choices=('flash', 'overlay'), default='flash')
    p.add_argument('--remove-helpers', action='store_true')
    p.add_argument('--eeprom', type=Path)
    p.add_argument('--from-reset',action='store_true')
    p.add_argument('--boot-build',type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--expect', choices=('success', 'vector-fault', 'helper-fault'), default='success')
    args = p.parse_args()
    check(__debug__, 'model requires assertions; do not use -O')
    check(args.scenario == 'startup' or args.eeprom is not None, 'EEPROM input required')
    check(args.candidate is not None or (args.vectors == 'flash' and not args.remove_helpers),
          'overlay/removed helpers require a candidate')
    check(not args.from_reset or (args.candidate and args.boot_build and not args.remove_helpers),
          '--from-reset requires a candidate/boot build and retained helpers')
    for path in (args.candidate, args.eeprom, startup.DEFAULT_FIRMWARE):
        check(path is None or args.output.resolve() != path.resolve(), 'output aliases input')
    options = dict(candidate=args.candidate, vectors=args.vectors,
                   remove_helpers=args.remove_helpers,from_reset=args.from_reset,boot_build=args.boot_build)
    if args.scenario != 'startup':
        options['eeprom'] = args.eeprom
    emu = {'startup': Startup, 'boot': Boot, 'home': Home}[args.scenario](**options)
    failure = None
    result = None
    try:
        if args.scenario == 'startup':
            result = emu.run(5_000_000)
            if args.expect == 'success':
                startup.verify_reached_panel(result)
            elif args.expect == 'vector-fault':
                check(result['stop_reason'] == 'frozen bootloader fault vector reached'
                      and result['SVC_exceptions'] == 1, 'expected first SVC to hit frozen vector')
            else:
                check(result['error'] is not None and result['pc'] == '0x100d0',
                      'expected erased helper fault at 0x100d0')
        elif args.scenario == 'boot':
            emu.boot()
            result = emu.run_boot(1000, 30_000_000)
            boot.verify(result)
        else:
            emu.boot()
            result = emu.run_home(1600, 40_000_000)
            feedback.describe(emu, result)
            feedback.verify(emu, result)
        audit = emu.audit()
        check(audit['protected_prefix_unchanged'] and audit['full_image_unchanged'],
              'flash contents changed')
        check(not audit['cpu_flash_stores'] and not audit['spi_writes'],
              'unexpected flash/SPI write')
        if emu.registered_vectors:
            check(audit['registration_intact'],'stock clobbered the reserved vector state')
        if args.candidate:
            check(audit['handoff'] is not None and
                  (int(audit['handoff']['cpsr'],16)&0xff)==0x10 and
                  audit['handoff']['sp']=='0x187dbdfc' and audit['handoff']['r0']=='0x11960',
                  'incorrect shim handoff')
    except Exception as exc:
        failure = f'{type(exc).__name__}: {str(exc)[:1000]}'
    if result is None:
        result = emu.result()
    report = dict(scenario=args.scenario, expected=args.expect,
                  assertions_passed=failure is None, failure=failure,
                  audit=emu.audit(), result=result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    state = result.get('result', result)
    print(json.dumps(dict(output=str(args.output), assertions_passed=failure is None,
                         failure=failure, pc=state['pc'], error=state['error'],
                         stop=state['stop_reason'], svcs=state['SVC_exceptions'],
                         irqs=emu.irqs if hasattr(emu, 'irqs') else 0), indent=2))
    return 1 if failure else 0


if __name__ == '__main__':
    sys.exit(main())
