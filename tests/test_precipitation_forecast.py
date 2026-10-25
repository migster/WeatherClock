import calendar
import contextlib
import io
import math
import struct
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import forecast


def grid(values, unit="wmoUnit:percent"):
    return {"properties": {
        "temperature": {
            "uom": "wmoUnit:degC",
            "values": [{"validTime": "2026-10-25T16:00:00Z/PT2H", "value": 25}],
        },
        "probabilityOfPrecipitation": {
            "uom": unit,
            "values": [{"validTime": interval, "value": value} for interval, value in values],
        },
    }}


class ForecastTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(forecast, "time", SimpleNamespace(
            mktime=calendar.timegm, localtime=time.gmtime
        ))
        clock.start()
        self.addCleanup(clock.stop)

    def parse(self, data, date=(2026, 10, 25)):
        return forecast.parse_weather_forecast(data, date, -5, True)

    def test_every_probability_boundary(self):
        for probability, row in [
            (None, None), (0, None), (0.01, 0), (9.999, 0), (10, 1),
            (29.999, 1), (30, 2), (49.999, 2), (50, 3), (59.999, 3),
            (60, 4), (69.999, 4), (70, 5), (79.999, 5), (80, 6),
            (89.999, 6), (90, 7), (100, 7),
        ]:
            with self.subTest(probability=probability):
                self.assertEqual(forecast.precipitation_row(probability), row)

    def test_invalid_probabilities_are_errors_not_dry_forecasts(self):
        for probability in (-1, 100.1, "60", math.nan, math.inf):
            with self.subTest(probability=probability), self.assertRaises(ValueError):
                forecast.precipitation_row(probability)

    def test_shared_fetch_shape_interval_expansion_units_and_gaps(self):
        data = grid([
            ("2026-10-25T16:00:00Z/PT2H", 60),
            ("2026-10-25T18:00:00Z/PT1H", 0),
            ("2026-10-25T19:00:00Z/PT1H", None),
        ])
        result = self.parse(data)
        self.assertEqual(result["date"], (2026, 10, 25))
        self.assertEqual(result["temperatures"][12:14], [77, 77])
        self.assertEqual(len(result["precipitation_probabilities"]), 24)
        self.assertEqual(result["precipitation_probabilities"][12:16], [60, 60, 0, None])
        self.assertTrue(all(v is None for v in result["precipitation_probabilities"][:12]))

    def test_all_zero_is_valid_data(self):
        result = self.parse(grid([("2026-10-25T04:00:00Z/P1D", 0)]))
        self.assertEqual(result["precipitation_probabilities"], [0] * 24)

    def test_midnight_filters_previous_and_following_day(self):
        result = self.parse(grid([("2026-10-26T03:00:00Z/PT3H", 80)]))
        self.assertEqual(result["precipitation_probabilities"], [None] * 23 + [80])

    def test_precipitation_and_temperature_share_dst_gap_and_repeated_hour(self):
        for date, values, expected in [
            ((2026, 3, 8), [
                ("2026-03-08T06:00:00Z/PT1H", 20),
                ("2026-03-08T07:00:00Z/PT1H", 80),
            ], [20, None, 80]),
            ((2026, 11, 1), [
                ("2026-11-01T05:00:00Z/PT1H", 20),
                ("2026-11-01T06:00:00Z/PT1H", 80),
            ], [80, None, None]),
        ]:
            with self.subTest(date=date):
                result = forecast.parse_precipitation_forecast(grid(values), date, -5, True)
                self.assertEqual(result[1:4], expected)

    def test_missing_or_invalid_rain_logs_error_without_breaking_temperature(self):
        missing = grid([])
        del missing["properties"]["probabilityOfPrecipitation"]
        for data in [
            missing, grid([]), grid([("2026-10-25T16:00:00Z/PT1H", None)]),
            grid([("2026-10-25T16:00:00Z/PT1H", 101)]),
            grid([("2026-10-25T16:00:00Z/PT1H", 50)], "wmoUnit:mm"),
            grid([("invalid/PT1H", 50)]),
        ]:
            with self.subTest(data=data), contextlib.redirect_stdout(io.StringIO()) as output:
                result = self.parse(data)
                self.assertEqual(result["temperatures"][12], 77)
                self.assertEqual(result["precipitation_probabilities"], [None] * 24)
                self.assertIn("Precipitation forecast unavailable", output.getvalue())

    def test_temperature_thresholds_unchanged_including_32_bit_conversion(self):
        for value, row in [
            (-10, 0), (30, 0), (39.9, 0), (40, 1), (50, 2),
            (60, 3), (70, 4), (80, 5), (90, 6), (99.9, 6), (100, 7), (120, 7),
        ]:
            with self.subTest(value=value):
                self.assertEqual(forecast.temperature_row(value), row)
        for fahrenheit in range(40, 101, 10):
            celsius = struct.unpack("f", struct.pack("f", (fahrenheit - 32) * 5 / 9))[0]
            data = grid([])
            data["properties"]["temperature"]["values"][0]["value"] = celsius
            result = forecast.parse_temperature_forecast(data, (2026, 10, 25), -5, True)
            self.assertEqual(result["temperatures"][12], fahrenheit)

    def test_merging_preserves_zero_not_missing_and_never_crosses_dates(self):
        previous = self.parse(grid([("2026-10-25T04:00:00Z/PT2H", 0)]))
        incoming = self.parse(grid([("2026-10-25T04:00:00Z/PT6H", 90)]))
        result = forecast.merge_past_forecasts(previous, incoming, 4)
        self.assertEqual(result["precipitation_probabilities"][:6], [0, 0, 90, 90, 90, 90])
        incoming = {"date": (2026, 10, 26), "precipitation_probabilities": [None] * 24}
        self.assertEqual(
            forecast.merge_past_forecasts(previous, incoming, 4)["precipitation_probabilities"],
            [None] * 24,
        )


if __name__ == "__main__":
    unittest.main()
