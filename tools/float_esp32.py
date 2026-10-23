#!/usr/bin/env python3
"""Leave ESP32-C3 GPIO4/5 floating and verify through ROM register access.

Optional independent check after an I2C session. Sends no I2C transactions and
writes no flash. Leaves the ESP32 in ROM so its application cannot change pins.
Requires esptool 4.x, or the existing PlatformIO tool-esptoolpy package.
"""
import argparse
import json
from pathlib import Path
import sys

try:
    from esptool.targets.esp32c3 import ESP32C3ROM
except ImportError:
    sys.path.insert(0, str(Path.home() / '.platformio/packages/tool-esptoolpy'))
    from esptool.targets.esp32c3 import ESP32C3ROM

# ESP-IDF ESP32-C3 gpio_reg.h / io_mux_reg.h.
GPIO, IOMUX = 0x60004000, 0x60009000
PINS = (4, 5)
MASK = (1 << 4) | (1 << 5)


def float_pins(esp):
    esp.write_reg(GPIO + 0x28, MASK)  # GPIO_ENABLE_W1TC: release both outputs.
    for pin in PINS:
        address = IOMUX + 4 * (pin + 1)
        value = esp.read_reg(address)
        # GPIO function, input enabled, no internal pull-up or pull-down.
        esp.write_reg(address, (value & ~(0x7000 | 0x180)) | 0x1200)
        esp.write_reg(GPIO + 0x554 + 4 * pin, 0x280)
    enable = esp.read_reg(GPIO + 0x20)
    assert enable & MASK == 0, 'GPIO output still enabled'
    result = {'gpio_output_enable': hex(enable), 'pins': {}}
    for pin in PINS:
        value = esp.read_reg(IOMUX + 4 * (pin + 1))
        assert value & 0x180 == 0, 'Internal pull still enabled'
        assert value & 0x7200 == 0x1200, 'Pin not configured as GPIO input'
        result['pins'][str(pin)] = {'iomux': hex(value), 'output_enabled': False,
                                   'pullup_enabled': False, 'pulldown_enabled': False}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='/dev/cu.usbmodem1101')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise SystemExit('Report already exists; choose a new filename')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {'port': args.port, 'i2c_transactions': 0, 'eeprom_data_writes': 0,
              'esp_flash_writes': 0}
    esp = ESP32C3ROM(args.port, 115200)
    try:
        esp.connect()
        report['chip'] = esp.get_chip_description()
        report['final_gpio_state'] = float_pins(esp)
        report['left_in_rom_bootloader'] = True
        print(json.dumps(report, indent=2))
    except BaseException as error:
        report['error'] = str(error)
        raise
    finally:
        esp._port.close()
        args.report.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
