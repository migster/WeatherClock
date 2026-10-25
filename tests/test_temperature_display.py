import calendar
import contextlib
import importlib.util
import io
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import forecast


def load_application():
    modules = {name: types.ModuleType(name) for name in (
        "wifi", "socketpool", "adafruit_requests", "board", "rtc", "adafruit_ntp",
        "microcontroller", "watchdog", "adafruit_ht16k33", "adafruit_ht16k33.matrix",
    )}
    modules["adafruit_ht16k33.matrix"].Matrix8x8x2 = Mock()
    modules["microcontroller"].watchdog = Mock()
    modules["watchdog"].WatchDogMode = types.SimpleNamespace(RESET=1)
    spec = importlib.util.spec_from_file_location("weatherclock_application", ROOT / "code.py")
    app = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules), contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(app)
    return app


app = load_application()


class Matrix:
    LED_RED, LED_GREEN, LED_YELLOW = 1, 2, 3

    def __init__(self):
        self.pixels = {}

    def fill(self, color):
        self.pixels.clear()

    def __setitem__(self, position, color):
        self.pixels[position] = color


class StopLoop(BaseException):
    pass


class Clock:
    def __init__(self, moments):
        self.moments = iter(moments)
        self.now = next(self.moments)
        self.sleeps = 0

    def localtime(self):
        return time.gmtime(self.now)

    def mktime(self, value):
        return calendar.timegm(value)

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        if self.sleeps % 3 == 0:
            try:
                self.now = next(self.moments)
            except StopIteration:
                raise StopLoop()


def moment(day=24, hour=12, minute=0):
    return calendar.timegm((2026, 10, day, hour, minute, 0))


def sample(day=24, value=75):
    return {"date": (2026, 10, day), "temperatures": [value] * 24}


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.display = app.TemperatureDisplay.__new__(app.TemperatureDisplay)
        self.display.matrix1, self.display.matrix2, self.display.matrix3 = Matrix(), Matrix(), Matrix()
        for name in ("feed_watchdog", "dump_display", "maybe_resync_ntp"):
            setattr(self.display, name, Mock())
        self.display.forecast_url = None
        self.display.forecast_url_date = None
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        for target, value in (
            ("time", Clock([moment()])), ("TIMEZONE_OFFSET", -5), ("DST_AUTO", True),
        ):
            p = patch.object(app, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(forecast, "time", types.SimpleNamespace(
            mktime=calendar.timegm, localtime=time.gmtime
        ))
        p.start()
        self.addCleanup(p.stop)

    def test_fixed_rows_hour_alignment_missing_values_and_highlight(self):
        data = sample(value=None)
        for hour, value in [(0, 30), (7, 100), (8, 90), (12, 80), (16, 40), (23, -10)]:
            data["temperatures"][hour] = value
        self.display.display_on_matrices(data)
        self.assertEqual(self.display.matrix1.pixels, {(0, 0): 1, (7, 7): 1})
        self.assertEqual(self.display.matrix2.pixels, {(0, 6): 1, (4, 5): 3})
        self.assertEqual(self.display.matrix3.pixels, {(0, 1): 1, (7, 0): 1})
        self.display.display_on_matrices(data, stale=True)
        self.assertEqual(self.display.matrix1.pixels[(0, 0)], 2)
        self.assertEqual(self.display.matrix2.pixels[(4, 5)], 3)

    def test_previous_day_is_cleared_not_relabelled(self):
        self.display.display_on_matrices(sample())
        self.display.display_on_matrices(sample(day=23))
        self.assertEqual(self.display.matrix1.pixels, {})
        self.assertEqual(self.display.matrix2.pixels, {})
        self.assertEqual(self.display.matrix3.pixels, {})

    def test_ascii_uses_same_fixed_single_point_bands(self):
        data = sample(value=None)
        data["temperatures"][12] = 100
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.display.display_ascii_chart(data)
        self.assertEqual(output.getvalue().count("*"), 1)
        self.assertIn("100+", output.getvalue())
        self.assertIn("12:00: 100.0 F", output.getvalue())
        self.assertIn("00:00: unavailable", output.getvalue())

    def test_request_headers_status_and_socket_cleanup(self):
        response = Mock(status_code=200)
        response.json.return_value = {"properties": {}}
        self.display.requests = Mock()
        self.display.requests.get.return_value = response
        self.assertEqual(self.display.request_json("https://api.weather.gov/test"), {"properties": {}})
        response.close.assert_called_once()
        self.assertEqual(
            self.display.requests.get.call_args.kwargs["headers"]["User-Agent"],
            app.WEATHER_USER_AGENT,
        )
        for status in (404, 503):
            response.reset_mock()
            response.status_code = status
            with self.assertRaises(RuntimeError):
                self.display.request_json("https://api.weather.gov/test")
            response.close.assert_called_once()
        response.reset_mock()
        response.status_code = 200
        response.json.side_effect = ValueError("invalid JSON")
        with self.assertRaises(ValueError):
            self.display.request_json("https://api.weather.gov/test")
        response.close.assert_called_once()

    def test_grid_discovery_cache_and_celsius_fetch(self):
        self.display.check_wifi_connection = Mock(return_value=True)
        self.display.requests = Mock()
        self.display.request_json = Mock(side_effect=[
            {"properties": {"forecastGridData": "https://api.weather.gov/gridpoints/TBW/57,97"}},
            {"properties": {"temperature": {"uom": "wmoUnit:degC", "values": [
                {"validTime": "2026-10-24T16:00:00Z/PT2H", "value": 25}
            ]}}},
        ])
        with patch.object(app.gc, "mem_free", return_value=1000000, create=True):
            data = self.display.fetch_temperature_data(max_retries=1)
        self.assertEqual(data["temperatures"][12:14], [77, 77])
        self.assertIn("/points/", self.display.request_json.call_args_list[0].args[0])
        self.display.request_json.reset_mock(side_effect=True)
        self.display.request_json.return_value = {
            "properties": {"temperature": {"uom": "wmoUnit:degF", "values": [
                {"validTime": "2026-10-24T16:00:00Z/PT1H", "value": 80}
            ]}}
        }
        with patch.object(app.gc, "mem_free", return_value=1000000, create=True):
            self.display.fetch_temperature_data(max_retries=1)
        self.display.request_json.assert_called_once_with("https://api.weather.gov/gridpoints/TBW/57,97")

    def test_fetch_failure_is_explicit_and_invalidates_mapping(self):
        self.display.check_wifi_connection = Mock(return_value=True)
        self.display.requests = Mock()
        self.display.forecast_url = "https://api.weather.gov/gridpoints/TBW/57,97"
        self.display.forecast_url_date = (2026, 10, 24)
        self.display.request_json = Mock(side_effect=RuntimeError("HTTP 503"))
        self.assertIsNone(self.display.fetch_temperature_data(max_retries=1))
        self.assertIsNone(self.display.forecast_url)

    def test_ntp_and_forecasts_share_the_same_local_time_conversion(self):
        utc = time.gmtime(calendar.timegm((2026, 10, 24, 16, 0, 0)))
        app.adafruit_ntp.NTP = Mock(return_value=types.SimpleNamespace(datetime=utc))
        hardware_clock = types.SimpleNamespace(datetime=None)
        app.rtc.RTC = Mock(return_value=hardware_clock)
        self.display.sync_time(None)
        self.assertEqual(hardware_clock.datetime.tm_hour, 12)
        self.assertEqual(self.display.clock_offset, -4 * 3600)

    def test_clock_adjusts_at_both_dst_transitions(self):
        for calendar_date, old_offset, expected_hour, expected_offset in (
            ((2026, 3, 8, 2, 0, 0), -5, 3, -4),
            ((2026, 11, 1, 2, 0, 0), -4, 1, -5),
        ):
            app.time = Clock([calendar.timegm(calendar_date)])
            hardware_clock = types.SimpleNamespace(datetime=None)
            app.rtc.RTC = Mock(return_value=hardware_clock)
            self.display.clock_offset = old_offset * 3600
            self.display.last_ntp_sync = calendar_date[2]
            app.TemperatureDisplay.maybe_resync_ntp(self.display)
            self.assertEqual(hardware_clock.datetime.tm_hour, expected_hour)
            self.assertEqual(self.display.clock_offset, expected_offset * 3600)

    def run_cycles(self, moments, results):
        app.time = Clock(moments)
        self.display.fetch_temperature_data = Mock(side_effect=results)
        for name in ("show_api_status", "display_on_matrices", "display_ascii_chart",
                     "show_error_on_matrices", "show_safe_mode_on_matrices"):
            setattr(self.display, name, Mock())
        with self.assertRaises(StopLoop):
            self.display.run_continuous()

    def test_hourly_refresh_stale_cache_and_immediate_recovery(self):
        self.run_cycles(
            [moment(hour=12), moment(hour=13), moment(hour=13, minute=5)],
            [sample(value=70), None, sample(value=80)],
        )
        calls = self.display.display_on_matrices.call_args_list
        self.assertEqual([c.kwargs["stale"] for c in calls], [False, True, False])
        self.assertEqual(calls[-1].args[0]["temperatures"], [70] * 13 + [80] * 11)
        self.assertEqual(self.display.fetch_temperature_data.call_count, 3)

    def test_midnight_failure_never_displays_yesterdays_data(self):
        self.run_cycles(
            [moment(hour=23), moment(day=25, hour=0)], [sample(), None]
        )
        self.display.display_on_matrices.assert_called_once()
        self.display.show_error_on_matrices.assert_called_once()

    def test_safe_mode_and_recovery(self):
        self.run_cycles(
            [moment(minute=m) for m in (0, 5, 10, 15, 30, 45)],
            [None, None, None, None, None, sample()],
        )
        self.display.show_safe_mode_on_matrices.assert_called_once()
        self.display.display_on_matrices.assert_called_once()
        self.assertFalse(self.display.display_on_matrices.call_args.kwargs["stale"])


if __name__ == "__main__":
    unittest.main()
