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
        try:
            self.now = next(self.moments)
        except StopIteration:
            raise StopLoop()


def moment(day=24, hour=12, minute=0, second=0):
    return calendar.timegm((2026, 10, day, hour, minute, second))


def sample(day=24, value=75):
    return {
        "date": (2026, 10, day),
        "temperatures": [value] * 24,
        "precipitation_probabilities": [60] * 24,
    }


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

    def test_precipitation_bands_zero_missing_current_and_other_hours(self):
        data = sample()
        probabilities = [None] * 24
        for hour, probability in [
            (0, 0), (1, 0.1), (2, 9.9), (3, 10), (4, 29.9), (5, 30),
            (6, 49.9), (7, 50), (8, 59.9), (9, 60), (10, 69.9), (11, 70),
            (12, 79.9), (13, 80), (14, 89.9), (15, 90), (16, 100), (23, 20),
        ]:
            probabilities[hour] = probability
        data["precipitation_probabilities"] = probabilities
        self.display.display_precipitation_on_matrices(data)
        heights = [
            [0, 1, 1, 2, 2, 3, 3, 4],
            [4, 5, 5, 6, 6, 7, 7, 8],
            [8, 0, 0, 0, 0, 0, 0, 2],
        ]
        matrices = (self.display.matrix1, self.display.matrix2, self.display.matrix3)
        for matrix_index, matrix in enumerate(matrices):
            self.assertEqual(matrix.pixels, {
                (x, y): 3 if matrix_index == 1 and x == 4 else 1
                for x, height in enumerate(heights[matrix_index])
                for y in range(height)
            })
        self.display.display_precipitation_on_matrices(data, stale=True)
        for matrix_index, matrix in enumerate(matrices):
            self.assertEqual(matrix.pixels, {
                (x, y): 3 if matrix_index == 1 and x == 4 else 2
                for x, height in enumerate(heights[matrix_index])
                for y in range(height)
            })
        data["precipitation_probabilities"][12] = 0
        self.display.display_precipitation_on_matrices(data)
        self.assertNotIn((4, 0), self.display.matrix2.pixels)
        self.assertTrue(all(x != 4 for x, y in self.display.matrix2.pixels))

    def test_switching_views_clears_bars_without_changing_temperature_points(self):
        data = sample(value=75)
        data["precipitation_probabilities"] = [100] * 24
        self.display.display_precipitation_on_matrices(data)
        matrices = (self.display.matrix1, self.display.matrix2, self.display.matrix3)
        self.assertTrue(all(len(matrix.pixels) == 64 for matrix in matrices))
        self.display.display_on_matrices(data)
        for matrix_index, matrix in enumerate(matrices):
            self.assertEqual(matrix.pixels, {
                (x, 4): 3 if matrix_index == 1 and x == 4 else 1 for x in range(8)
            })
        data["precipitation_probabilities"] = [1] * 24
        self.display.display_precipitation_on_matrices(data)
        for matrix_index, matrix in enumerate(matrices):
            self.assertEqual(matrix.pixels, {
                (x, 0): 3 if matrix_index == 1 and x == 4 else 1 for x in range(8)
            })

    def test_rainfall_midnight_unavailable_and_dry_day_are_distinct(self):
        data = sample()
        data["precipitation_probabilities"] = [0] * 24
        self.display.show_error_on_matrices = Mock()
        self.display.display_precipitation_on_matrices(data)
        self.display.show_error_on_matrices.assert_not_called()
        self.assertTrue(all(not m.pixels for m in (
            self.display.matrix1, self.display.matrix2, self.display.matrix3
        )))
        data["precipitation_probabilities"] = [None] * 24
        self.display.display_precipitation_on_matrices(data)
        self.display.show_error_on_matrices.assert_called_once()
        self.display.display_precipitation_on_matrices(sample(day=23))
        self.assertTrue(all(not m.pixels for m in (
            self.display.matrix1, self.display.matrix2, self.display.matrix3
        )))

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
            ]}, "probabilityOfPrecipitation": {"uom": "wmoUnit:percent", "values": [
                {"validTime": "2026-10-24T16:00:00Z/PT2H", "value": 60}
            ]}}},
        ])
        with patch.object(app.gc, "mem_free", return_value=1000000, create=True):
            data = self.display.fetch_temperature_data(max_retries=1)
        self.assertEqual(data["temperatures"][12:14], [77, 77])
        self.assertEqual(data["precipitation_probabilities"][12:14], [60, 60])
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
                     "display_precipitation_on_matrices", "show_error_on_matrices",
                     "show_safe_mode_on_matrices"):
            setattr(self.display, name, Mock())
        self.rendered_views = []
        self.display.display_on_matrices.side_effect = lambda *args, **kwargs: self.rendered_views.append(
            (app.time.monotonic(), "temperature")
        )
        self.display.display_precipitation_on_matrices.side_effect = lambda *args, **kwargs: self.rendered_views.append(
            (app.time.monotonic(), "precipitation")
        )
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

    def test_switches_at_ten_seconds_without_refetching(self):
        self.run_cycles(
            [moment(second=s) for s in (0, 9, 10, 19, 20, 29, 30)],
            [sample()],
        )
        self.assertEqual(self.display.display_on_matrices.call_count, 2)
        self.assertEqual(self.display.display_precipitation_on_matrices.call_count, 2)
        self.display.fetch_temperature_data.assert_called_once()
        labels = [call.args[0] for call in self.display.show_api_status.call_args_list]
        self.assertEqual(labels, ["fetching", "ok"])
        self.assertEqual(self.rendered_views, [
            (moment(second=0), "temperature"), (moment(second=10), "precipitation"),
            (moment(second=20), "temperature"), (moment(second=30), "precipitation"),
        ])

    def test_switch_interval_is_configurable(self):
        with patch.object(app, "DISPLAY_SWITCH_INTERVAL", 3):
            self.run_cycles([moment(second=s) for s in (0, 2, 3, 5, 6)], [sample()])
        self.assertEqual(self.display.display_on_matrices.call_count, 2)
        self.assertEqual(self.display.display_precipitation_on_matrices.call_count, 1)
        self.display.fetch_temperature_data.assert_called_once()

    def test_missed_switches_advance_without_replaying_frames(self):
        self.run_cycles(
            [moment(second=s) for s in (0, 35, 39, 40)],
            [sample()],
        )
        self.assertEqual(self.rendered_views, [
            (moment(), "temperature"), (moment(second=35), "precipitation"),
            (moment(second=40), "temperature"),
        ])
        self.display.fetch_temperature_data.assert_called_once()

    def test_switching_continues_during_refresh_retry_wait(self):
        self.run_cycles(
            [moment(), moment(hour=13), moment(hour=13, second=10),
             moment(hour=13, second=20)],
            [sample(), None],
        )
        self.display.fetch_temperature_data.assert_called()
        self.assertEqual(self.display.fetch_temperature_data.call_count, 2)
        self.display.display_precipitation_on_matrices.assert_called_once()
        self.assertTrue(self.display.display_precipitation_on_matrices.call_args.kwargs["stale"])
        self.assertTrue(self.display.display_on_matrices.call_args.kwargs["stale"])

    def test_future_rain_updates_but_past_predictions_and_zero_are_preserved(self):
        old, incoming = sample(), sample()
        old["precipitation_probabilities"][0] = 0
        incoming["precipitation_probabilities"] = [90] * 24
        self.run_cycles([moment(), moment(hour=13)], [old, incoming])
        result = self.display.display_on_matrices.call_args.args[0]
        self.assertEqual(result["precipitation_probabilities"], [0] + [60] * 12 + [90] * 11)


if __name__ == "__main__":
    unittest.main()
