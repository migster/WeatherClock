import contextlib
import io
import runpy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import error_log


class LoggingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log = Path(directory.name) / "weatherclock.log"
        self.backup = Path(directory.name) / "weatherclock.log.1"
        for name, value in (
            ("LOG_PATH", str(self.log)), ("BACKUP_PATH", str(self.backup)),
            ("_write_failed", False),
            ("time", types.SimpleNamespace(
                localtime=lambda: (2026, 11, 21, 9, 0, 0, 5, 325, -1),
                monotonic=lambda: 123,
            )),
        ):
            p = patch.object(error_log, name, value)
            p.start()
            self.addCleanup(p.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_entries_survive_restart_and_include_time_uptime_and_error(self):
        error_log.log_event("Attempt 1 failed (RuntimeError): HTTP 503")
        error_log._write_failed = False
        error_log.log_event("WeatherClock starting")
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "2026-11-21 09:00:00 uptime=123s "
                         "Attempt 1 failed (RuntimeError): HTTP 503")
        self.assertIn("WeatherClock starting", lines[1])

    def test_rotation_keeps_recent_events_within_both_file_limits(self):
        with patch.object(error_log, "MAX_LOG_BYTES", 256):
            for index in range(20):
                error_log.log_event("failure {} {}".format(index, "x" * 60))
        self.assertLessEqual(self.log.stat().st_size, 256)
        self.assertLessEqual(self.backup.stat().st_size, 256)
        self.assertIn("failure 19 ", self.log.read_text())
        self.assertNotIn("failure 0 ", self.backup.read_text())
        self.assertEqual(len(list(self.log.parent.iterdir())), 2)

    def test_long_unicode_and_multiline_messages_are_bounded_valid_text(self):
        with patch.object(error_log, "MAX_MESSAGE_BYTES", 5):
            error_log.log_event("a\n" + "\u00e9" * 1000)
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        message = lines[0].split("uptime=123s ", 1)[1]
        self.assertLessEqual(len(message.encode("utf-8")), 5)
        self.assertTrue(message.startswith("a "))

    def test_read_only_and_full_storage_report_once_without_stopping_clock(self):
        for code in (30, 28):
            with self.subTest(errno=code):
                error_log._write_failed = False
                with patch("builtins.open", side_effect=OSError(code, "storage unavailable")) as file, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    error_log.log_event("first failure")
                    error_log.log_event("second failure")
                file.assert_called_once()
                self.assertEqual(output.getvalue().count("Persistent logging disabled"), 1)
                self.assertIn("second failure", output.getvalue())

    def test_boot_enables_logging_without_button_access(self):
        storage = types.ModuleType("storage")
        storage.remount = Mock()
        with patch.dict(sys.modules, {
            "storage": storage, "board": None, "digitalio": None,
        }):
            runpy.run_path(str(ROOT / "boot.py"))
        storage.remount.assert_called_once_with("/", readonly=False)


if __name__ == "__main__":
    unittest.main()
