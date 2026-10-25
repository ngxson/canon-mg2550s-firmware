#!/usr/bin/env python3
"""Exercise stock button entry and a full replacement image, entirely offline.

Requires the historical fw_dump/gpt USB harness. The source-built target is
snapshotted from the public bootloader/app build artifacts; nothing is flashed
to physical hardware. Stock instructions execute the USB parser and writer.
"""
import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

from stock_panel_update_probe import model_class


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness', required=True, type=Path)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    assert not (output / 'manifest.json').exists(), 'Use a new evidence directory'
    project = args.project
    boot_dir, app_dir = project / 'bootloader/build', project / 'app/build'
    boot_meta = json.loads((boot_dir / 'manifest.json').read_text())
    app_meta = json.loads((app_dir / 'manifest.json').read_text())
    boot = (boot_dir / 'boot_region.bin').read_bytes()
    app = (app_dir / 'application.bin').read_bytes()
    assert len(boot) == 0x10000
    assert sha(boot) == boot_meta['artifacts']['boot_region.bin']['sha256']
    assert sha(app) == app_meta['artifacts']['application.bin']['sha256']
    assert len(boot) + len(app) <= 0x200000
    image = (boot + app).ljust(0x200000, b'\xff')
    # Pin the full image and symbols so later app builds cannot change this run.
    (output / 'replacement_factory.bin').write_bytes(image)
    save(output / 'boot_manifest.json', boot_meta)
    save(output / 'app_manifest.json', app_meta)
    ColdPanel = model_class(args.harness)
    from emulate_usb_controller import MILESTONES
    from emulate_usb_update import FW, RAM, BASE, STAGING, MODE_OBJECT, FLASH_OBJECT, STOP
    from flash_usb import make_stream
    from unicorn import UC_HOOK_CODE
    from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_LR, UC_ARM_REG_CPSR, UC_ARM_REG_SP
    stream = make_stream(image, sha(image))
    manifest = dict(stock_sha256=sha(FW), image_sha256=sha(image),
                    boot_region_sha256=sha(boot), application_sha256=sha(app),
                    image_bytes=len(image), stream_sha256=sha(stream),
                    stream_bytes=len(stream), initial_eeprom_marker='00',
                    initial_boot_latch=0, baseline_panel_input='08',
                    scope='Offline stock reset/button/USB full-image update; no hardware access')
    save(output / 'manifest.json', manifest)

    class FullPanel(ColdPanel):
        def __init__(self, samples, expected_mode):
            self.expected_mode = expected_mode
            self.entry = None
            self.staged = False
            self.reached_new_reset = False
            self.return_landings = []
            self.exit_tail = deque(maxlen=64)
            self.exit_hook = None
            self.cache_invalidations = []
            self.pending_cache_invalidation = False
            self.saw_icache_instruction = False
            self.next_progress = 0
            super().__init__(samples)
            self.high_speed = True
            self.bulk_stream = stream
            # Observe milestones, not every instruction; firmware is unchanged.
            self.uc.hook_del(self.code_hook)
            hooks = set(MILESTONES) | {0x20005a84, 0x1800, 0x1804, 0x1bc,
                                       0x1c0, 0x1c4, 0x100}
            for address in hooks:
                self.uc.hook_add(UC_HOOK_CODE, self.code, begin=address, end=address)

        def run_cold(self, budget=3000000000):
            super().run_cold(budget=budget)
            if self.pending_cache_invalidation:
                # Unicorn retains translated old flash code across the model's
                # SPI mem_write, and does not evict it for ARM's I-cache MCR.
                # Pause AFTER executing that unchanged CPU instruction, evict
                # translations, then re-fetch the following instruction. This
                # models its effect; it does not patch firmware or redirect PC.
                pc = self.uc.reg_read(UC_ARM_REG_PC)
                assert pc == 0x1c4 and self.saw_icache_instruction
                self.uc.ctl_remove_cache(0, len(image))
                self.pending_cache_invalidation = False
                self.cache_invalidations.append(dict(instruction_pc='0x1c0',
                    resume_pc=hex(pc), translation_range=['0x0', hex(len(image))]))
                self.uc.emu_start(pc, STOP, count=budget)

        def code(self, cpu, address, size, user):
            if self.final_status_acknowledged and not self.cache_invalidations:
                if address == 0x1c0:
                    assert bytes(cpu.mem_read(address, 4)) == FW[address:address+4]
                    assert FW[address:address+4] == bytes.fromhex('150f07ee')
                    self.saw_icache_instruction = True
                if address == 0x1c4:
                    assert self.saw_icache_instruction
                    self.pending_cache_invalidation = True
                    cpu.emu_stop()
                    return
            if self.final_status_acknowledged and address in (0x1800, 0x1804, 0x1ac, 0x1bc, 0x100):
                self.return_landings.append(hex(address))
                if address == 0x100:
                    self.reached_new_reset = True
                    cpu.emu_stop()
                    return
                if address == 0x1ac:
                    # Execute the new compatibility branch; do not stop at the
                    # parent's old-stock normal-boot continuation boundary.
                    return
            super().code(cpu, address, size, user)
            if address == 0x2000351c:
                self.entry = dict(mode=self.word(MODE_OBJECT + 12),
                                  marker=hex(self.eeprom[9]),
                                  prior_eeprom_writes=list(self.eeprom_writes),
                                  flash_unchanged=bytes(cpu.mem_read(0, len(FW))) == FW)
                assert self.entry == dict(mode=self.expected_mode, marker='0x0',
                                          prior_eeprom_writes=[], flash_unchanged=True)
            if address == 0x20003368:
                assert bytes(cpu.mem_read(STAGING, len(image))) == image
                self.staged = True
                print(f'Mode {self.expected_mode}: all 2 MiB staged exactly; programming', flush=True)
            if address == 0x200030f8 and self.exit_hook is None:
                self.exit_hook = cpu.hook_add(UC_HOOK_CODE, self.observe_exit)

        def observe_exit(self, cpu, address, size, user):
            self.exit_tail.append(hex(address))

        def log(self, event, **fields):
            if event == 'firmware':
                self.trace.append(dict(event=event, **fields))
                print(f'Mode {self.expected_mode}: {fields["name"]}', flush=True)
            if event == 'usb_packet' and fields['endpoint'] == 1:
                if self.bulk_position >= self.next_progress:
                    print(f'Mode {self.expected_mode}: received {self.bulk_position:,}/{len(stream):,} stream bytes', flush=True)
                    self.next_progress = self.bulk_position + 1048576

    reports = []
    final_flash = None
    for mode, samples in ((3, [0x20]*128 + [0]*4),
                          (4, [0x20]*128 + [0x60]*16 + [0x20]*16 + [0]*4)):
        emu = FullPanel(samples, mode)
        started = time.monotonic()
        try:
            emu.run_cold(budget=3000000000)
        except Exception as error:
            failed_flash = bytes(emu.uc.mem_read(0, len(image)))
            failure = dict(error=repr(error), mode=mode, entry=emu.entry,
                           pc=hex(emu.uc.reg_read(UC_ARM_REG_PC)),
                           lr=hex(emu.uc.reg_read(UC_ARM_REG_LR)),
                           sp=hex(emu.uc.reg_read(UC_ARM_REG_SP)),
                           cpsr=hex(emu.uc.reg_read(UC_ARM_REG_CPSR)),
                           final_flash_sha256=sha(failed_flash), image_sha256=sha(image),
                           complete_staging_buffer_matches=emu.staged,
                           flash_matches_target=failed_flash == image,
                           host_final_status=emu.last_status,
                           final_status_acknowledged=emu.final_status_acknowledged,
                           erase_count=len(emu.erases), program_count=len(emu.programs),
                           return_landings=emu.return_landings,
                           exit_instruction_tail=list(emu.exit_tail), milestones=emu.trace)
            save(output / f'mode{mode}_failure.json', failure)
            print(json.dumps(failure, indent=2), flush=True)
            raise
        final_flash = bytes(emu.uc.mem_read(0, len(image)))
        assert emu.entry and emu.entry['mode'] == mode
        assert emu.last_status == 'C0' and emu.final_status_acknowledged
        assert emu.bulk_position == len(stream) and emu.staged
        assert final_flash == image
        assert emu.erases == [dict(address=hex(a), length=65536) for a in range(0, len(image), 65536)]
        assert len(emu.programs) == 512
        assert [int(x['address'], 16) for x in emu.programs] == list(range(0, len(image), 4096))
        assert emu.eeprom[9] == 0x55
        assert emu.word(FLASH_OBJECT + 0x20dc) == 4
        assert bytes(emu.uc.mem_read(BASE + 0x1000, 0x6540)) == bytes(RAM[0x1000:0x7540])
        assert emu.reached_new_reset
        assert emu.return_landings == ['0x1804', '0x1ac', '0x1bc', '0x100']
        assert '0x20001e10' in emu.indicator_pcs
        descriptor = next(bytes.fromhex(x['data']) for x in emu.responses if x['name'] == 'device_descriptor')
        vid, pid = struct.unpack_from('<HH', descriptor, 8)
        assert (vid, pid, emu.printer_interface) == (0x04a9, 0x12fe, 0)
        report = dict(mode=mode, all_assertions_passed=True, entry=emu.entry,
                      start_pc='0x0', stop_pc=hex(emu.uc.reg_read(UC_ARM_REG_PC)),
                      final_flash_sha256=sha(final_flash), image_sha256=sha(image),
                      stream_bytes_sent=emu.bulk_position, stream_sha256=sha(stream),
                      complete_staging_buffer_matches=True, host_final_status=emu.last_status,
                      final_status_acknowledged=emu.final_status_acknowledged,
                      usb=dict(vid=hex(vid), pid=hex(pid), printer_interface=emu.printer_interface,
                               max_packet=emu.bulk_max_packet),
                      erase_count=len(emu.erases), program_count=len(emu.programs),
                      erases=emu.erases, programs=emu.programs, eeprom_writes=emu.eeprom_writes,
                      final_eeprom_marker=hex(emu.eeprom[9]),
                      updater_code_unchanged=True, new_reset_reached=True,
                      icache_model_translation_invalidations=emu.cache_invalidations,
                      return_landings=emu.return_landings, milestones=emu.trace,
                      elapsed_seconds=round(time.monotonic()-started, 3))
        save(output / f'mode{mode}.json', report)
        reports.append(report)
        print(f'PASS mode {mode}: full flash exact, 32 erases, 512 programs, C0, new reset reached', flush=True)

    # Independent cold boot of the exact flash produced by stock's writer.
    # This uses the replacement model, not an assertion of uninterrupted ASIC
    # state continuity across the old/new USB driver transition.
    sys.path.insert(0, str(project / 'bootloader/tests'))
    from model import Model, setup
    metadata = dict(layout=boot_meta['layout'], symbols=dict(
        bootloader=boot_meta['symbols']['bootloader'],
        application=app_meta['symbols']['application']))
    e = Model(image=final_flash, metadata=metadata)
    e.run(lambda x: bool(x.info()) and x.info()[2] == 2)
    assert e.identities[:2] == [0x176d, 0x12fe]
    assert 0x20020000 in e.entered
    app_info = e.info()
    assert app_info[0:3] == (0x5746474d, 2, 2)
    assert app_info[3] & 4 and app_info[5:] == (0x80, 0x10000, 0x1f0000)
    application_boot = e.summary()
    before = e.resets
    e.requests.appendleft(('reboot_boot', setup(0x40, 0x41, 1)))
    e.run(lambda x: x.resets == before + 1 and bool(x.info())
          and x.info()[2] == 1 and bool(x.info()[3] & 1), resume=True)
    assert bytes(e.uc.mem_read(0, len(image))) == image
    boot_report = dict(all_assertions_passed=True, image_sha256=sha(final_flash),
                       scope='Separate replacement-model cold boot of stock-programmed flash',
                       application_info=list(app_info), application_boot=application_boot,
                       management_reboot_into_rescue=e.summary())
    save(output / 'replacement_boot.json', boot_report)
    save(output / 'summary.json', dict(all_assertions_passed=True, **manifest,
        modes=[dict(mode=r['mode'], status=r['host_final_status'],
                    final_flash_sha256=r['final_flash_sha256'],
                    new_reset_reached=r['new_reset_reached']) for r in reports],
        replacement_cold_boot=True, app_management_reboot_into_rescue=True,
        limits=['Synthetic peripheral/USB/timer behavior',
                'ARM I-cache invalidation at 0x1c0 explicitly evicts Unicorn flash translations',
                'No physical button entry or flash test',
                'Replacement cold boot is a separate emulator instance']))
    print('PASS replacement image: boots application, reports management, reboots into held rescue', flush=True)


if __name__ == '__main__':
    main()
