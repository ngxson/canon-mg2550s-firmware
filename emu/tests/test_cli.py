"""Input validation and CLI failure propagation, without a physical device."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

EMU = Path(__file__).resolve().parents[1]
STOCK = EMU.parent / 'dumps/mg2550s_w25q16cv_full.bin'


class CommandTests(unittest.TestCase):
    def run_cli(self, *args, optimize=False):
        return subprocess.run([sys.executable, *(['-O'] if optimize else []),
                               str(EMU / 'run.py'), *map(str, args)],
                              capture_output=True, text=True, cwd='/tmp')

    def test_help_from_another_directory(self):
        result = self.run_cli('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('replacement-firmware', result.stdout)

    def test_reject_other_firmware_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / 'wrong.bin'
            image.write_bytes(bytes(0x200000))
            output = Path(temp) / 'report.json'
            result = self.run_cli('startup', '--firmware', image, '--output', output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('SHA-256', result.stderr)
            self.assertFalse(output.exists())
            self.assertEqual(image.read_bytes(), bytes(0x200000))

    def test_report_cannot_overwrite_firmware_or_eeprom(self):
        result = self.run_cli('startup', '--output', STOCK)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('must not overwrite', result.stderr)
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / 'eeprom.bin'
            image.write_bytes(bytes(1024))
            alias = Path(temp) / 'alias.json'
            alias.symlink_to(image)
            result = self.run_cli('home', '--eeprom', image, '--output', alias)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('must not overwrite', result.stderr)
            self.assertEqual(image.read_bytes(), bytes(1024))

    def test_bad_eeprom_size_and_incompatible_options(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / 'short.bin'
            image.write_bytes(bytes(32))
            result = self.run_cli('home', '--eeprom', image)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('1024-byte', result.stderr)
        result = self.run_cli('home', '--eeprom', '/unused', '--no-completion')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires --feedback backoff', result.stderr)
        result = self.run_cli('startup', '--matrix', '--stall-transfer', '1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--matrix uses fixed', result.stderr)

    def test_disabled_assertions_are_rejected(self):
        result = self.run_cli('startup', optimize=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('assertions must stay enabled', result.stderr)

    def test_failed_scenario_returns_failure_and_keeps_report(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'failed.json'
            result = self.run_cli('startup', '--budget', '1', '--verify', '--output', output)
            self.assertNotEqual(result.returncode, 0)
            report = json.loads(output.read_text())
            self.assertFalse(report['scenario_assertions_passed'])
            self.assertTrue(report['run']['validation_error'])


if __name__ == '__main__':
    unittest.main()
