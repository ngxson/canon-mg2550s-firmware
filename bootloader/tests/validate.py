#!/usr/bin/env python3
"""Run source-built firmware through the public, self-contained ARM model."""
import hashlib
import json
from pathlib import Path
import struct
import zlib
from model import Model, ROOT, BUILD, setup, TICKS_PER_MS

STAY = ('stay', setup(0x40, 0x40))
def begin(data): return ('begin', setup(0x40, 0x43, len(data)&65535, len(data)>>16))
def info_status(e, status): return bool(e.info()) and e.info()[5] == status
def app_mode(e): return bool(e.info()) and e.info()[2] == 2
POWER_BLINK = [(0, 0x10, 0, 0), (0, 0, 0, 0)]
APP_SEQUENCE = [(0, 0x10, 0, 0), (0x20, 0, 0, 0),
                (0, 0, 0x20, 0), (0, 0, 0, 0x10)]


def main():
    image = (BUILD/'factory.bin').read_bytes()
    app = (BUILD/'application.bin').read_bytes()
    loader = (BUILD/'bootloader.bin').read_bytes()
    report = {'factory_sha256': hashlib.sha256(image).hexdigest(), 'cases': {}}

    def record(name, emulator):
        # Inspect committed MMIO frames, not the firmware's sequence variable.
        assert all(frame in POWER_BLINK for frame in emulator.led_patterns['bootloader'])
        report['cases'][name] = emulator.summary(); print('PASS', name, flush=True)

    def repack(data, version=2):
        data = bytearray(data)
        struct.pack_into('<II', data, 4, version, len(data)-32)
        struct.pack_into('<I', data, 20, zlib.crc32(data[32:]))
        struct.pack_into('<I', data, 28, zlib.crc32(data[:28]))
        return bytes(data)

    e = Model(); e.run(app_mode)
    assert e.identities[:2] == [0x176d, 0x12fe]
    # The first-stage entry time includes its boot setup; the stage-2 counter is
    # restarted before its five-second window. Test the actual C decision too.
    assert e.entry_times[1] >= TICKS_PER_MS*5000
    record('five_second_window_then_distinct_application', e)

    for speed in (False, True):
        e = Model(actions=[[STAY]], speed=speed, coalesced=True)
        e.run(lambda x: bool(x.info()) and bool(x.info()[3]&1) and x.counter >= TICKS_PER_MS*10000)
        assert 0x20020000 not in e.entered
        assert e.led_patterns['bootloader'][:8] == POWER_BLINK*4
        record('stay_full_speed' if not speed else 'stay_high_speed_coalesced', e)

    e = Model(); e.run(lambda x: app_mode(x) and len(x.led_patterns['application']) >= 8)
    assert e.led_patterns['application'][:8] == APP_SEQUENCE*2
    before = len(e.led_patterns['bootloader'])
    e.requests.appendleft(('reboot_boot', setup(0x40, 0x41, 1)))
    e.run(lambda x: x.resets == 2 and bool(x.info()) and x.info()[2] == 1 and
          bool(x.info()[3]&1) and len(x.led_patterns['bootloader']) >= before+8, resume=True)
    assert e.led_patterns['bootloader'][before:before+8] == POWER_BLINK*4
    record('demo_four_led_sequence_usb_reboot_to_power_blink', e)

    invalid = bytearray(image); invalid[0x10000] ^= 1
    e = Model(image=bytes(invalid)); e.run(lambda x: bool(x.info()) and x.counter >= TICKS_PER_MS*10000)
    assert 0x20020000 not in e.entered and e.info()[3]&1
    record('invalid_application_stays_in_rescue', e)

    e = Model(host=False); e.run(lambda x: 0x20020000 in x.entered)
    record('no_usb_host_still_launches_app', e)

    for bad in (False, True):
        data = bytearray(app)
        if bad: data[-1] ^= 1
        e = Model(actions=[[STAY, begin(data)]], bulk=bytes(data))
        e.run(lambda x: info_status(x, 0x90 if bad else 0xc0))
        if bad: assert not e.erases and not e.programs
        else:
            assert bytes(e.uc.mem_read(0x10000, len(app))) == app
            assert e.programs[-1] == 0x10000
        record('reject_bad_usb_crc' if bad else 'usb_application_update_header_last', e)

    e = Model(actions=[[STAY, begin(app)]], bulk=app[:64])
    e.run(lambda x: info_status(x, 0x91))
    assert not e.erases
    record('partial_usb_upload_times_out_before_erase', e)

    e = Model(i2c_image=app)
    e.run(lambda x: x.results == [0xc0])
    assert e.programs[-1] == 0x10000
    record('stage0_ignores_main_image_bootloader_accepts_i2c', e)

    e = Model(actions=[[STAY, begin(app)]], bulk=app, corrupt=True)
    e.run(lambda x: info_status(x, 0xa3))
    record('flash_readback_error', e)

    e = Model(i2c_image=app, cut_after=1); e.run(lambda x: False)
    cut = bytes(e.uc.mem_read(0, 0x200000))
    restarted = Model(image=cut); restarted.run(lambda x: bool(x.info()) and x.counter >= TICKS_PER_MS*10000)
    assert 0x20020000 not in restarted.entered
    record('power_loss_before_header_commit_stays_rescue', restarted)
    recovered = Model(image=cut, i2c_image=app); recovered.run(lambda x: x.results == [0xc0])
    record('i2c_recovery_after_power_loss', recovered)

    e = Model(actions=[[STAY, ('reboot_app', setup(0x40, 0x41, 2))]])
    e.run(app_mode); assert e.resets == 2
    record('usb_acknowledged_reboot_to_application', e)

    e = Model(); e.run(app_mode)
    e.requests.appendleft(('reboot_boot', setup(0x40, 0x41, 1)))
    e.run(lambda x: x.resets == 2 and bool(x.info()) and x.info()[2] == 1 and bool(x.info()[3]&1), resume=True)
    record('main_app_usb_reboot_to_held_bootloader', e)

    e = Model(); e.run(app_mode); e.i2c_image = app
    e.run(lambda x: x.results == [0xc0], resume=True)
    assert e.resets == 2
    record('main_app_i2c_offer_reboots_and_updates_without_usb_command', e)

    e = Model(actions=[[STAY]]); e.run(lambda x: bool(x.info()) and bool(x.info()[3]&1))
    e.command = 4; e.run(app_mode, resume=True)
    assert e.results == [0xc0] and e.resets == 2
    record('i2c_reboot_to_application', e)

    e = Model(actions=[[STAY, ('reboot_app', setup(0x40, 0x41, 2))]], hold_reboot_ack=True)
    e.run(lambda x: x.counter>=TICKS_PER_MS*10000)
    assert e.resets==1 and 0x20020000 not in e.entered
    record('no_reboot_before_status_ack', e)

    for length in (0, 31, 0x1f0001):
        e = Model(actions=[[STAY, ('invalid_begin', setup(0x40,0x43,length&65535,length>>16))]])
        e.run(lambda x: bool(x.info()) and 'invalid_begin' in x.stalls)
        assert not e.erases and not e.programs
        record(f'reject_begin_length_{length}', e)

    wrong = bytearray(app); struct.pack_into('<I',wrong,12,0x20010000)
    struct.pack_into('<I',wrong,28,zlib.crc32(wrong[:28]))
    e = Model(actions=[[STAY,begin(wrong)]],bulk=bytes(wrong))
    e.run(lambda x: info_status(x,0x90)); assert not e.erases
    record('reject_valid_crc_wrong_load_address',e)

    e = Model(actions=[[STAY,begin(app),('busy_reboot',setup(0x40,0x41,2)),
                        ('busy_begin',setup(0x40,0x43,len(app)))]],bulk=app)
    e.run(lambda x: info_status(x,0xc0))
    assert e.stalls==['busy_reboot','busy_begin'] and e.resets==1
    record('reject_reboot_and_second_upload_while_busy',e)

    e = Model(actions=[[STAY,begin(app)]],bulk=app[:64]); e.run(lambda x: x.bulk_position==64)
    e.i2c_image=app
    e.run(lambda x: x.counter>=TICKS_PER_MS*20000,resume=True)
    assert not e.erases and not e.results
    e.run(lambda x: x.results==[0xc0],resume=True)
    record('i2c_does_not_interleave_with_owned_usb_transfer',e)

    e = Model(i2c_image=app,corrupt_frame=True)
    e.run(lambda x: app_mode(x))
    assert not e.erases and not e.programs
    record('corrupt_i2c_offer_cannot_modify_flash',e)

    legacy = repack(app, version=1)
    e = Model(actions=[[STAY,begin(legacy)]],bulk=legacy)
    e.run(lambda x: info_status(x,0x90)); assert not e.erases
    record('reject_128k_layout_package_without_erasing',e)

    large = repack(app.ljust(0x11000, b'\xa5'))
    e = Model(actions=[[STAY,begin(large)]],bulk=large)
    e.run(lambda x: info_status(x,0xc0),budget=90000000)
    assert e.erases == [0x10000,0x20000] and e.erase_sizes == [0x10000]*2
    assert e.programs[-1] == 0x10000 and bytes(e.uc.mem_read(0x10000,len(large))) == large
    record('application_crosses_64k_boundary_preserving_boot_region',e)

    broken = bytearray(image); broken[0x4000] ^= 1
    e = Model(image=bytes(broken),i2c_image=loader,maintenance=True)
    e.run(lambda x: x.results == [0xc0])
    assert e.erases == [0x4000,0x5000,0x6000] and e.erase_sizes == [0x1000]*3
    assert e.programs[-1] == 0x4000 and bytes(e.uc.mem_read(0x4000,len(loader))) == loader
    e.run(app_mode,resume=True)
    record('stage0_recovers_second_stage_using_4k_erase',e)

    for name,data in [('legacy',repack(loader,version=1)),
                      ('oversize',repack(loader.ljust(0xc004,b'\xff')))]:
        e = Model(i2c_image=data,maintenance=True)
        e.run(lambda x: x.results == [0x90])
        assert not e.erases and not e.programs
        record('stage0_rejects_'+name+'_maintenance_package',e)

    e = Model(i2c_image=loader,maintenance=True,cut_after=1)
    e.run(lambda x: False)
    interrupted = bytes(e.uc.mem_read(0,0x200000))
    assert interrupted[:0x4000] == image[:0x4000] and interrupted[0x10000:] == image[0x10000:]
    e = Model(image=interrupted,i2c_image=loader,maintenance=True)
    e.run(lambda x: x.results == [0xc0])
    record('stage0_recovers_after_interrupted_second_stage_update',e)

    report['all_assertions_passed'] = True
    (BUILD/'validation.json').write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__': main()
