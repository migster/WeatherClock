"""Weather forecast parsing shared by the clock and host-side tests."""

import math
import time


def temperature_row(fahrenheit):
    """Map Fahrenheit to a fixed LED row: below 40 -> 0, 100+ -> 7."""
    if not isinstance(fahrenheit, (int, float)) or not math.isfinite(fahrenheit):
        raise ValueError("Temperature must be a finite number")
    return max(0, min(7, int((fahrenheit - 30) // 10)))


def precipitation_row(probability):
    """Map a positive percentage to an LED row; zero/missing stays dark."""
    if probability is None:
        return None
    if (
        not isinstance(probability, (int, float))
        or not math.isfinite(probability)
        or not 0 <= probability <= 100
    ):
        raise ValueError("Precipitation probability must be between 0 and 100")
    if probability == 0:
        return None
    for row, threshold in enumerate((10, 30, 50, 60, 70, 80, 90)):
        if probability < threshold:
            return row
    return 7


def parse_timestamp(value):
    """Decode an ISO 8601 timestamp into UTC seconds using CircuitPython time."""
    if not isinstance(value, str) or len(value) < 20 or value[10] != "T":
        raise ValueError("Invalid forecast timestamp: {}".format(value))
    seconds = time.mktime((
        int(value[:4]), int(value[5:7]), int(value[8:10]),
        int(value[11:13]), int(value[14:16]), int(value[17:19]),
        0, 0, -1,
    ))
    if value.endswith("Z"):
        return seconds
    offset = value[-6:]
    if offset[0] not in ("+", "-") or offset[3] != ":":
        raise ValueError("Forecast timestamp requires a UTC offset")
    offset_seconds = (int(offset[1:3]) * 60 + int(offset[4:6])) * 60
    return seconds - (offset_seconds if offset[0] == "+" else -offset_seconds)


def parse_duration(value):
    """Decode NWS day/hour/minute/second ISO 8601 durations."""
    if not value.startswith("P"):
        raise ValueError("Invalid forecast duration: {}".format(value))
    seconds = 0
    number = ""
    in_time = False
    for char in value[1:]:
        if char == "T" and not number and not in_time:
            in_time = True
        elif char.isdigit() or char == ".":
            number += char
        else:
            units = {"H": 3600, "M": 60, "S": 1} if in_time else {"D": 86400}
            if not number or char not in units:
                raise ValueError("Unsupported forecast duration: {}".format(value))
            seconds += float(number) * units[char]
            number = ""
    if number or seconds <= 0:
        raise ValueError("Invalid forecast duration: {}".format(value))
    return seconds


def local_forecast_time(utc_seconds, standard_offset, dst_auto):
    """Convert UTC to configured local time, including US DST transitions."""
    offset = standard_offset * 3600
    if dst_auto:
        year = time.localtime(utc_seconds + offset).tm_year
        march_first = time.localtime(time.mktime((year, 3, 1, 0, 0, 0, 0, 0, -1)))
        november_first = time.localtime(time.mktime((year, 11, 1, 0, 0, 0, 0, 0, -1)))
        march_sunday = 1 + (6 - march_first.tm_wday) % 7 + 7
        november_sunday = 1 + (6 - november_first.tm_wday) % 7
        start = time.mktime((year, 3, march_sunday, 2, 0, 0, 0, 0, -1)) - offset
        end = time.mktime((year, 11, november_sunday, 2, 0, 0, 0, 0, -1)) - offset - 3600
        if start <= utc_seconds < end:
            offset += 3600
    return time.localtime(utc_seconds + offset)


def expand_hourly_values(periods, date, standard_offset, dst_auto):
    """Expand interval values into local hour columns without inventing gaps."""
    values = [None] * 24
    for period in periods:
        value = period["value"]
        if value is None:
            continue
        start_text, duration_text = period["validTime"].split("/")
        start = parse_timestamp(start_text)
        end = start + parse_duration(duration_text)
        # Select hourly samples inside the half-open validity interval.
        sample = math.ceil(start / 3600) * 3600
        while sample < end:
            local = local_forecast_time(sample, standard_offset, dst_auto)
            if (local.tm_year, local.tm_mon, local.tm_mday) == date:
                values[local.tm_hour] = value
            sample += 3600
    return values


def parse_temperature_forecast(data, date, standard_offset, dst_auto):
    """Return 24 Fahrenheit values for a local calendar day; gaps stay None."""
    temperature = data["properties"]["temperature"]
    unit = temperature["uom"]
    if unit not in ("wmoUnit:degC", "wmoUnit:degF"):
        raise ValueError("Unsupported temperature unit: {}".format(unit))
    temperatures = expand_hourly_values(temperature["values"], date, standard_offset, dst_auto)
    for hour, value in enumerate(temperatures):
        if value is not None:
            temperature_row(value)
            # Remove conversion noise, including CircuitPython's 32-bit float error.
            temperatures[hour] = round(value * 9 / 5 + 32, 4) if unit == "wmoUnit:degC" else value
    if all(value is None for value in temperatures):
        raise ValueError("No temperature forecast available for the local day")
    return {"date": date, "temperatures": temperatures}


def parse_precipitation_forecast(data, date, standard_offset, dst_auto):
    """Read percentages for their enclosing forecast intervals, not hourly odds."""
    precipitation = data["properties"]["probabilityOfPrecipitation"]
    if precipitation["uom"] != "wmoUnit:percent":
        raise ValueError("Unsupported precipitation probability unit: {}".format(precipitation["uom"]))
    probabilities = expand_hourly_values(precipitation["values"], date, standard_offset, dst_auto)
    for probability in probabilities:
        precipitation_row(probability)
    if all(value is None for value in probabilities):
        raise ValueError("No precipitation forecast available for the local day")
    return probabilities


def parse_weather_forecast(data, date, standard_offset, dst_auto):
    """Keep temperature working if precipitation is unavailable or malformed."""
    result = parse_temperature_forecast(data, date, standard_offset, dst_auto)
    try:
        result["precipitation_probabilities"] = parse_precipitation_forecast(
            data, date, standard_offset, dst_auto
        )
    except (KeyError, TypeError, ValueError) as error:
        print("Precipitation forecast unavailable: {}".format(error))
        result["precipitation_probabilities"] = [None] * 24
    return result


def merge_past_forecasts(previous, incoming, current_hour):
    """Retain today's elapsed-hour predictions for both views, never yesterday's."""
    if previous is not None and previous["date"] == incoming["date"]:
        for field in ("temperatures", "precipitation_probabilities"):
            if field in previous and field in incoming:
                for hour in range(current_hour):
                    if previous[field][hour] is not None:
                        incoming[field][hour] = previous[field][hour]
    return incoming
