import calendar
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


def grid(values, unit="wmoUnit:degF"):
    return {"properties": {"temperature": {"uom": unit, "values": [
        {"validTime": interval, "value": value} for interval, value in values
    ]}}}


class ForecastTests(unittest.TestCase):
    def setUp(self):
        # CircuitPython has no OS time zone: mktime/localtime use RTC calendar time.
        self.clock = patch.object(forecast, "time", SimpleNamespace(
            mktime=calendar.timegm, localtime=time.gmtime
        ))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_every_band_boundary_and_clamping(self):
        for value, row in [
            (-100, 0), (29.9, 0), (30, 0), (39.999, 0),
            (40, 1), (49.999, 1), (50, 2), (59.999, 2),
            (60, 3), (69.999, 3), (70, 4), (79.999, 4),
            (80, 5), (89.999, 5), (90, 6), (99.999, 6),
            (100, 7), (150, 7),
        ]:
            with self.subTest(value=value):
                self.assertEqual(forecast.temperature_row(value), row)

    def test_invalid_temperatures_are_not_zero(self):
        for value in (None, "70", math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                forecast.temperature_row(value)

    def test_celsius_multi_hour_interval_and_null_gaps(self):
        result = forecast.parse_temperature_forecast(grid([
            ("2026-10-24T18:00:00+00:00/PT2H", 25),
            ("2026-10-24T20:00:00+00:00/PT1H", None),
        ], "wmoUnit:degC"), (2026, 10, 24), -5, True)
        self.assertEqual(len(result["temperatures"]), 24)
        self.assertEqual(result["temperatures"][14:17], [77, 77, None])
        self.assertTrue(all(v is None for v in result["temperatures"][:14]))

    def test_celsius_roundoff_does_not_drop_boundary_a_row(self):
        result = forecast.parse_temperature_forecast(grid([
            ("2026-10-24T18:00:00Z/PT1H", 37.77777777777777)
        ], "wmoUnit:degC"), (2026, 10, 24), 0, False)
        self.assertEqual(result["temperatures"][18], 100)
        self.assertEqual(forecast.temperature_row(result["temperatures"][18]), 7)

    def test_circuitpython_float_precision_at_all_temperature_boundaries(self):
        for fahrenheit in range(40, 101, 10):
            celsius = struct.unpack("f", struct.pack("f", (fahrenheit - 32) * 5 / 9))[0]
            result = forecast.parse_temperature_forecast(grid([
                ("2026-10-24T18:00:00Z/PT1H", celsius)
            ], "wmoUnit:degC"), (2026, 10, 24), 0, False)
            with self.subTest(fahrenheit=fahrenheit):
                self.assertEqual(
                    forecast.temperature_row(result["temperatures"][18]),
                    forecast.temperature_row(fahrenheit),
                )

    def test_utc_offset_and_midnight_date_filter(self):
        result = forecast.parse_temperature_forecast(grid([
            ("2026-10-25T03:00:00+00:00/PT3H", 85),
        ]), (2026, 10, 24), -5, True)
        self.assertEqual(result["temperatures"][23], 85)
        self.assertTrue(all(v is None for v in result["temperatures"][:23]))
        self.assertEqual(
            forecast.parse_timestamp("2026-10-24T23:00:00-04:00"),
            forecast.parse_timestamp("2026-10-25T03:00:00Z"),
        )
        self.assertEqual(
            forecast.parse_timestamp("2026-10-25T08:30:00+05:30"),
            forecast.parse_timestamp("2026-10-25T03:00:00Z"),
        )

    def test_dst_disabled_and_winter_standard_time(self):
        for date, interval in [
            ((2026, 10, 24), "2026-10-24T18:00:00Z/PT1H"),
            ((2026, 1, 24), "2026-01-24T18:00:00Z/PT1H"),
        ]:
            result = forecast.parse_temperature_forecast(grid([(interval, 70)]), date, -5, False)
            self.assertEqual(result["temperatures"][13], 70)

    def test_spring_gap_and_fall_repeated_hour(self):
        spring = forecast.parse_temperature_forecast(grid([
            ("2026-03-08T06:00:00Z/PT1H", 60),
            ("2026-03-08T07:00:00Z/PT1H", 70),
        ]), (2026, 3, 8), -5, True)
        self.assertEqual(spring["temperatures"][1:4], [60, None, 70])
        fall = forecast.parse_temperature_forecast(grid([
            ("2026-11-01T05:00:00Z/PT1H", 60),
            ("2026-11-01T06:00:00Z/PT1H", 70),
        ]), (2026, 11, 1), -5, True)
        self.assertEqual(fall["temperatures"][1], 70)

    def test_duration_formats_and_exclusive_end(self):
        for text, seconds in [("PT2H", 7200), ("P1DT2H30M", 95400), ("PT3600S", 3600)]:
            self.assertEqual(forecast.parse_duration(text), seconds)
        result = forecast.parse_temperature_forecast(grid([
            ("2026-10-24T00:30:00Z/PT1H30M", 70)
        ]), (2026, 10, 24), 0, False)
        self.assertEqual(result["temperatures"][:3], [None, 70, None])
        for text in ("P", "PT0H", "P1M", "PT-1H", "2H"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                forecast.parse_duration(text)

    def test_missing_and_invalid_data_raise_explicit_errors(self):
        for data in [
            grid([]), grid([("2026-10-24T00:00:00Z/PT1H", None)]),
            grid([("2026-10-25T00:00:00Z/PT1H", 70)]),
            grid([("2026-10-24T00:00:00Z/PT1H", 70)], "kelvin"),
            grid([("not-a-date/PT1H", 70)]),
        ]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                forecast.parse_temperature_forecast(data, (2026, 10, 24), 0, False)

    def test_merge_preserves_elapsed_hours_but_updates_future(self):
        old = {"date": (2026, 10, 24), "temperatures": [70] * 24}
        new = {"date": (2026, 10, 24), "temperatures": [80] * 24}
        result = forecast.merge_past_forecasts(old, new, 12)
        self.assertEqual(result["temperatures"], [70] * 12 + [80] * 12)
        tomorrow = {"date": (2026, 10, 25), "temperatures": [None] * 12 + [80] * 12}
        self.assertEqual(
            forecast.merge_past_forecasts(old, tomorrow, 12)["temperatures"],
            [None] * 12 + [80] * 12,
        )


if __name__ == "__main__":
    unittest.main()
