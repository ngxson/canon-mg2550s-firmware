"""Validate inputs before executing address-specific stock experiments."""
import hashlib
from pathlib import Path

DEFAULT_FIRMWARE = Path(__file__).resolve().parents[2] / 'dumps/mg2550s_w25q16cv_full.bin'
STOCK_SHA256 = '4b032367cff5976aeb426c8723e08d3c431a38e622e245a614791a3e5067e533'


def load_firmware(path):
    path = Path(path).resolve()
    image = path.read_bytes()
    if len(image) != 0x200000 or hashlib.sha256(image).hexdigest() != STOCK_SHA256:
        raise ValueError('Stock experiments require the documented 2 MiB firmware '
                         f'(SHA-256 {STOCK_SHA256}); hooks are specific to that image')
    return path, image


def load_eeprom(path):
    path = Path(path).resolve()
    image = path.read_bytes()
    if len(image) != 1024:
        raise ValueError('EEPROM input must be a raw 1024-byte 24C08 image')
    return path, image
