# WeatherClock - CircuitPython Temperature Forecast Clock

A WiFi-connected clock that displays **today's hourly temperature forecast in
degrees Fahrenheit** on three 8x8 bicolor LED matrices. It uses the free NOAA
National Weather Service (NWS) API, not the former tide-prediction API.

## Reading the Display

Each column represents a fixed local hour, from midnight on the left to 11 PM
on the right. **One LED per available hour** indicates its predicted temperature;
columns are not filled bars.

| Physical LED row | Temperature (Fahrenheit) |
|---|---|
| Top (row 7) | 100 F and above |
| Second from top (row 6) | 90 to below 100 F |
| Row 5 | 80 to below 90 F |
| Row 4 | 70 to below 80 F |
| Row 3 | 60 to below 70 F |
| Row 2 | 50 to below 60 F |
| Row 1 | 40 to below 50 F |
| Bottom (row 0) | 30 to below 40 F; colder temperatures also clamp here |

The scale is **fixed**, not normalized to the day's minimum and maximum.
For example, 79.9 F uses row 4; 80 F uses row 5.

| Matrix | I2C address | Hours, left to right |
|---|---|---|
| Left | `0x70` | 00-07 |
| Center | `0x71` | 08-15 |
| Right | `0x72` | 16-23 |

- **Yellow:** the current hour's forecast.
- **Red:** other available hourly forecasts.
- **Green:** cached forecasts for today after a refresh failure; the current hour
  remains yellow.
- **Blank column:** no forecast is available for that hour. Missing values are
  never treated as zero degrees.

NWS forecasts may omit hours that have already passed. At startup those columns
stay blank. While the clock remains running, it retains the predictions it
previously received for elapsed hours on the same day. Those retained values are
**forecasts, not observed temperatures**. This cache is in RAM and is lost on
restart. Yesterday's temperatures are never reused as today's forecast.

## Hardware

- CircuitPython-compatible WiFi microcontroller. The existing hardware setup was
  tested on an **Adafruit Feather ESP32 V2 with CircuitPython 10.0.3**.
- Three HT16K33 **8x8 bicolor** LED matrices at addresses `0x70`, `0x71`, `0x72`.
- I2C wiring and an internet connection.

The raw NWS grid response can be hundreds of kilobytes and is decoded in memory.
A board with sufficient free heap is required; lower-memory boards may report
`MemoryError`. The temperature conversion has host-side tests, but the new
forecast path still needs an on-device smoke test for available heap, network
behavior, and physical LED orientation.

## Installation

1. Install CircuitPython for your board.
2. Install the libraries in `requirements.txt` using the Adafruit CircuitPython
   bundle matching your firmware, or use `circup`:

   ```sh
   pip install circup
   circup install adafruit_ht16k33 adafruit_requests adafruit_ntp
   ```

   Required libraries in `CIRCUITPY/lib/` include:

   - `adafruit_ht16k33/`
   - `adafruit_requests.mpy`
   - `adafruit_ntp.mpy`
   - `adafruit_bus_device/`
   - `adafruit_connection_manager.mpy`

3. Copy **both `code.py` and `forecast.py`** into the root of `CIRCUITPY`.
4. Configure `settings.toml` as below, then restart the board.

## Configuration

```toml
WIFI_SSID = "your_wifi_network"
WIFI_PASSWORD = "your_wifi_password"

# Optional fallback networks, tried in order.
# WIFI_SSID_2 = "another_network"
# WIFI_PASSWORD_2 = "another_password"
# WIFI_SSID_3 = "third_network"
# WIFI_PASSWORD_3 = "third_password"

# Default: Miami, FL.
WEATHER_LATITUDE = "25.6936"
WEATHER_LONGITUDE = "-80.4517"

# NWS requires an application-identifying User-Agent. Contact info is optional.
WEATHER_USER_AGENT = "WeatherClock/1.0"

NTP_SERVER = "time.google.com"
TIMEZONE_OFFSET = -5
DST_AUTO = "1"

LED_BRIGHTNESS = "0.3"
DISPLAY_DUMP = "0"
```

### Location and Time Zone

Set `WEATHER_LATITUDE` and `WEATHER_LONGITUDE` to your desired location within
NWS forecast coverage, primarily the United States and its territories.
The former `TIDE_STATION` setting is no longer used. Station `8726724` is
**Clearwater Beach**, not St. Petersburg as previously documented.

Also set the time zone for that location:

- `TIMEZONE_OFFSET`: standard-time offset from UTC, e.g. Eastern `-5`, Central
  `-6`, Mountain `-7`, Pacific `-8`.
- `DST_AUTO = "1"`: add one hour during US daylight saving time.
- `DST_AUTO = "0"`: disable daylight saving adjustments, e.g. for most of Arizona
  or Hawaii. The clock does not infer a time zone from the coordinates.

The device synchronizes its local clock using NTP at startup and daily at 3 AM,
and checks daylight saving transitions in the main loop.
Forecast validity timestamps are converted from their UTC offsets into the
configured local time. Spring's nonexistent hour remains blank; fall's repeated
hour shares one column and the later forecast interval wins. The display always
has 24 fixed hour columns.

### Brightness and Serial Mirror

`LED_BRIGHTNESS` controls all three matrices from `0.0` (off) to `1.0` (full).
Set `DISPLAY_DUMP = "1"` to print timestamped, colored 24x8 snapshots to the serial
console at display changes. Rows print top to bottom; physical row 0 is at the
bottom. Set it to `"0"` to disable the snapshots. The serial forecast chart and
numeric Fahrenheit readings are still logged on successful fetches.

## Data Fetching and Updates

1. Look up `https://api.weather.gov/points/{latitude},{longitude}`.
2. Follow its `properties.forecastGridData` URL. This works for coastal marine
   grids as well as land grids; coastal locations may not support the separate
   `/forecast/hourly` endpoint.
3. Read `properties.temperature`, expand each `validTime` interval into its
   covered hours, convert Celsius to Fahrenheit when necessary, and keep only
   the current local calendar day.

The point-to-grid mapping is cached in RAM and refreshed daily or after a
request failure. The API requires no key or paid subscription; requests include
the configured `User-Agent`.

- **Startup:** fetch today's forecast.
- **Hourly:** refresh the forecast and update the current-hour highlight.
- **Midnight:** clear the previous day's data and fetch the new day's forecast.
- **Failed fetch:** retry up to three times with watchdog-fed backoff. After
  these attempts, retry after 5, 15, or 30 minutes as failures accumulate.
  A new hour also triggers a refresh.
- **Recovery:** immediately redraw fresh data after a successful retry.

The main loop checks approximately every 30 seconds. Network requests and their
immediate retries can delay updates while in progress.

### Failure Indicators

- During a fetch, the top-right pixel of the left matrix briefly shows status:
  yellow while fetching, green for success, red for failure. The next chart
  redraw restores this pixel to its forecast role.
- If today's cached forecast exists, a failed refresh displays it in green.
- Without usable data for today, a red X appears on the center matrix.
- After five failed fetch cycles, yellow borders indicate safe mode.
- Successful fetching exits safe mode and restores the normal chart.

The boot sequence is unchanged: corner LEDs show matrix/WiFi/NTP progress, then
the date (`MM / DD`) and time (`HH : MM`) each appear for about three seconds.

## API Example

NWS raw grid temperature data has this shape:

```json
{
  "properties": {
    "temperature": {
      "uom": "wmoUnit:degC",
      "values": [
        {
          "validTime": "2026-10-24T18:00:00+00:00/PT2H",
          "value": 25
        }
      ]
    }
  }
}
```

This predicts 77 F throughout the two-hour interval starting at 18:00 UTC.
With Eastern daylight time configured, it lights row 4 in the 14:00 and 15:00
columns. Intervals exclude their end time. Null values leave hours blank.

Rainfall is **not displayed** in this version.

Official references:

- [NWS API documentation](https://www.weather.gov/documentation/services-web-api)
- [Default location lookup](https://api.weather.gov/points/25.6936,-80.4517)

## Project Structure

```text
WeatherClock/
|-- code.py                       # CircuitPython application and matrix rendering
|-- forecast.py                   # Forecast parsing, time conversion, fixed bands
|-- settings.toml                 # WiFi, coordinates, time zone, display settings
|-- requirements.txt              # CircuitPython libraries
|-- README.md
`-- tests/
    |-- test_forecast.py           # Host-side automated tests
    |-- test_temperature_display.py # Host-side mocked hardware/network tests
    |-- serial_temperature_display.py # CircuitPython serial-only forecast check
    |-- blinktest.py               # Hardware LED test
    |-- matrixtest.py              # Hardware matrix test
    `-- simple_tide_display bar chart.py # Historical tide prototype, not the clock
```

The historical tide bar-chart prototype is retained for reference only. It does
not use the new forecast configuration or temperature bands.

## Validation and Troubleshooting

Run automated tests on a computer with Python 3 (no CircuitPython packages needed):

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

For a serial-only check on the board, copy `tests/serial_temperature_display.py`
beside `code.py` and `forecast.py`, then run it manually from the CircuitPython
REPL. It uses the same settings and forecast-fetching code without initializing
the LED matrices. Normal startup still runs `code.py`.

For hardware checks, use `tests/blinktest.py` and `tests/matrixtest.py`.

| Problem | Check |
|---|---|
| Matrix setup failed | I2C wiring, bicolor matrix type, unique addresses |
| Network setup failed | WiFi settings and signal |
| NWS HTTP error | Connectivity, coordinates, API coverage/status; see logged status and URL |
| No forecast for local day | Time/NTP, time-zone settings, NWS data availability |
| Missing early-hour columns | Expected after startup if NWS no longer supplies those hours |
| Unexpected hour placement | `TIMEZONE_OFFSET` and `DST_AUTO` must match the forecast location |
| `MemoryError` | Free heap/board capacity for the raw grid JSON response |
| Time sync failed | NTP access; the existing system clock is retained and the error is logged |

Do not commit or share WiFi passwords.
