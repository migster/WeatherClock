import wifi
import socketpool
import ssl
import adafruit_requests
import time
import board
from adafruit_ht16k33.matrix import Matrix8x8x2
import os
import rtc
import adafruit_ntp
import gc
import microcontroller
from microcontroller import watchdog as wdt
from watchdog import WatchDogMode
from forecast import local_forecast_time, merge_past_forecasts, parse_temperature_forecast, temperature_row

# Load WiFi credentials from settings.toml (primary + optional fallbacks).
# Add WIFI_SSID_2/WIFI_PASSWORD_2, WIFI_SSID_3/... for more networks.
WIFI_NETWORKS = []
for _ssid_key, _pw_key in (
    ('WIFI_SSID', 'WIFI_PASSWORD'),
    ('WIFI_SSID_2', 'WIFI_PASSWORD_2'),
    ('WIFI_SSID_3', 'WIFI_PASSWORD_3'),
):
    _ssid = os.getenv(_ssid_key)
    if _ssid:
        WIFI_NETWORKS.append((_ssid, os.getenv(_pw_key)))


def connect_wifi():
    """Try each configured network in order; return the SSID we connect to."""
    last_error = None
    for ssid, password in WIFI_NETWORKS:
        try:
            print(f"Connecting to WiFi: {ssid}...")
            wifi.radio.connect(ssid, password)
            print(f"Connected to {ssid}")
            return ssid
        except Exception as e:
            last_error = e
            print(f"Could not connect to {ssid}: {e}")
    raise last_error if last_error else RuntimeError("No WiFi networks configured")
WEATHER_LATITUDE = float(os.getenv('WEATHER_LATITUDE', '25.6936'))
WEATHER_LONGITUDE = float(os.getenv('WEATHER_LONGITUDE', '-80.4517'))
if not (-90 <= WEATHER_LATITUDE <= 90 and -180 <= WEATHER_LONGITUDE <= 180):
    raise ValueError("Weather coordinates are out of range")
WEATHER_USER_AGENT = os.getenv('WEATHER_USER_AGENT', 'WeatherClock/1.0')

# Handle TIMEZONE_OFFSET with error checking
try:
    tz_val = os.getenv('TIMEZONE_OFFSET', '-5')  # Default as string
    print(f"TIMEZONE_OFFSET raw value: {tz_val}")
    TIMEZONE_OFFSET = int(float(tz_val))  # Convert via float first to handle decimals
    print(f"TIMEZONE_OFFSET parsed: {TIMEZONE_OFFSET}")
except (ValueError, AttributeError) as e:
    print(f"Error parsing TIMEZONE_OFFSET: {e}")
    TIMEZONE_OFFSET = -5  # Default to EST

# Handle LED_BRIGHTNESS with error checking
try:
    brightness_str = os.getenv('LED_BRIGHTNESS', '1.0')
    LED_BRIGHTNESS = float(brightness_str)
    LED_BRIGHTNESS = max(0.0, min(1.0, LED_BRIGHTNESS))  # Clamp to valid range
    print(f"LED_BRIGHTNESS loaded: {LED_BRIGHTNESS}")
except (ValueError, TypeError) as e:
    print(f"Error parsing LED_BRIGHTNESS, using default: {e}")
    LED_BRIGHTNESS = 1.0

try:
    NTP_SERVER = os.getenv('NTP_SERVER', 'time.google.com')
except Exception as e:
    print(f"Error with NTP_SERVER: {e}")
    NTP_SERVER = 'time.google.com'

# US DST auto-adjust: when enabled, TIMEZONE_OFFSET is treated as
# standard-time offset and +1 is added during DST (2nd Sun Mar - 1st Sun Nov).
DST_AUTO = os.getenv('DST_AUTO', '1') == '1'


# Serial "display mirror": set DISPLAY_DUMP="0" in settings.toml to silence.
DISPLAY_DUMP = os.getenv('DISPLAY_DUMP', '1') == '1'

API_URL = "https://api.weather.gov"

# 3x5 pixel font for digits 0-9 and colon/slash
# Each character is 3 columns wide, 5 rows tall, stored as column bitmasks (LSB = top row)
FONT_3X5 = {
    '0': (0x1F, 0x11, 0x1F),
    '1': (0x00, 0x1F, 0x00),
    '2': (0x1D, 0x15, 0x17),
    '3': (0x15, 0x15, 0x1F),
    '4': (0x07, 0x04, 0x1F),
    '5': (0x17, 0x15, 0x1D),
    '6': (0x1F, 0x15, 0x1D),
    '7': (0x01, 0x01, 0x1F),
    '8': (0x1F, 0x15, 0x1F),
    '9': (0x17, 0x15, 0x1F),
    '/': (0x18, 0x04, 0x03),
    ':': (0x00, 0x0A, 0x00),
}

class TemperatureDisplay:
    def __init__(self):
        self.pool = None          # Socket pool for NTP re-sync
        self.last_ntp_sync = None # Track last NTP sync date
        self._wdt_feed_warned = False  # One-shot log guard for WDT feed errors
        self.forecast_url = None
        self.forecast_url_date = None
        self.clock_offset = None
        self.setup_watchdog()
        self.setup_matrices()
        self.setup_network()
        
    def setup_watchdog(self):
        """Initialize watchdog timer to auto-reboot on hangs"""
        try:
            wdt.timeout = 60  # Reboot if not fed within 60 seconds
            wdt.mode = WatchDogMode.RESET
            print("Watchdog timer enabled (60s timeout)")
        except Exception as e:
            print(f"Watchdog setup failed: {e}")
        
    def setup_matrices(self):
        """Initialize the LED matrices"""
        try:
            print("Setting up LED matrices...")
            i2c = board.I2C()
            
            # Initialize the three 8x8 matrices with brightness parameter
            self.matrix1 = Matrix8x8x2(i2c, address=0x70, brightness=LED_BRIGHTNESS)  # First 8 hours (left)
            self.matrix2 = Matrix8x8x2(i2c, address=0x71, brightness=LED_BRIGHTNESS)  # Middle 8 hours (center) 
            self.matrix3 = Matrix8x8x2(i2c, address=0x72, brightness=LED_BRIGHTNESS)  # Last 8 hours (right)
            
            print(f"LED brightness set to {LED_BRIGHTNESS:.2f}")
            
            # Clear all matrices
            self.clear_matrices()
            
            # Stage 1: matrices alive — light corner pixel on each matrix
            self.matrix1[0, 0] = self.matrix1.LED_GREEN
            self.matrix2[0, 0] = self.matrix2.LED_GREEN
            self.matrix3[0, 0] = self.matrix3.LED_GREEN
            print("LED matrices initialized successfully")
            self.dump_display("boot: matrices alive")
            
        except Exception as e:
            print(f"Matrix setup failed: {e}")
            self.matrix1 = None
            self.matrix2 = None
            self.matrix3 = None
        
    def setup_network(self):
        """Initialize WiFi connection and requests session"""
        try:
            has_matrices = self.matrix1 and self.matrix2 and self.matrix3
            
            # Stage 2: WiFi connecting — yellow spinner on matrix2
            if has_matrices:
                self.matrix1[0, 0] = self.matrix1.LED_GREEN   # Stage 1 done
                self.matrix2[0, 0] = self.matrix2.LED_YELLOW  # WiFi in progress
                self.dump_display("boot: wifi connecting")

            connect_wifi()

            # Stage 2 done — WiFi connected
            if has_matrices:
                self.matrix2[0, 0] = self.matrix2.LED_GREEN
                self.dump_display("boot: wifi connected")

            self.pool = socketpool.SocketPool(wifi.radio)
            self.requests = adafruit_requests.Session(self.pool, ssl.create_default_context())
            
            # Stage 3: NTP syncing — yellow on matrix3
            if has_matrices:
                self.matrix3[0, 0] = self.matrix3.LED_YELLOW
                self.dump_display("boot: ntp syncing")

            print("Syncing NTP...")
            self.sync_time(self.pool)
            self.last_ntp_sync = time.localtime().tm_mday
            
            # Stage 3 done — all green
            if has_matrices:
                self.matrix3[0, 0] = self.matrix3.LED_GREEN
                self.dump_display("boot: all stages green")

            time.sleep(1)  # Brief pause so you can see all-green
            
            # Show date/time on matrices to confirm sync
            self.show_boot_info()
            
            gc.collect()
            print(f"Free memory after setup: {gc.mem_free()} bytes")
            
        except Exception as e:
            print(f"Network setup failed: {e}")
            # Show red on whichever stage failed
            if self.matrix1 and self.matrix2 and self.matrix3:
                for m in [self.matrix1, self.matrix2, self.matrix3]:
                    if m[0, 0] != m.LED_GREEN:
                        m[0, 0] = m.LED_RED
                self.dump_display("boot: setup error")
            
    def check_wifi_connection(self):
        """Check if WiFi is still connected"""
        try:
            return wifi.radio.connected
        except Exception:
            return False
            
    def reconnect_wifi(self, max_attempts=3):
        """Attempt to reconnect WiFi with retry logic"""
        for attempt in range(max_attempts):
            try:
                print(f"WiFi reconnection attempt {attempt + 1}/{max_attempts}...")
                connected_ssid = connect_wifi()

                # Recreate the requests session with new socket pool
                self.pool = socketpool.SocketPool(wifi.radio)
                self.requests = adafruit_requests.Session(self.pool, ssl.create_default_context())

                print(f"WiFi reconnected successfully to {connected_ssid}")
                return True
                
            except Exception as e:
                print(f"WiFi reconnection attempt {attempt + 1} failed: {e}")
                if attempt < max_attempts - 1:
                    wait = 5 * (attempt + 1)
                    for _ in range(wait):
                        self.feed_watchdog()
                        time.sleep(1)
                    
        print("WiFi reconnection failed after all attempts")
        return False
        
    def test_network_connectivity(self):
        """Test basic network connectivity with simple approach"""
        try:
            # Simple test - if we have WiFi and can create a socket pool, we're likely good
            if not self.check_wifi_connection():
                return False
                
            # Try to recreate session if needed
            if not hasattr(self, 'requests') or self.requests is None:
                self.pool = socketpool.SocketPool(wifi.radio)
                self.requests = adafruit_requests.Session(self.pool, ssl.create_default_context())
                
            return True
            
        except Exception as e:
            print(f"Network connectivity test failed: {e}")
            return False
            
    def sync_time(self, pool):
        """Sync local time with NTP server, auto-adjusting for US DST if enabled."""
        try:
            print(f"Syncing time with {NTP_SERVER}...")
            ntp = adafruit_ntp.NTP(pool, server=NTP_SERVER, tz_offset=0)
            utc_seconds = time.mktime(ntp.datetime)
            local = local_forecast_time(utc_seconds, TIMEZONE_OFFSET, DST_AUTO)
            rtc.RTC().datetime = local
            self.clock_offset = time.mktime(local) - utc_seconds

            current_time = time.localtime()
            print(f"Local time set to: {current_time.tm_year}-{current_time.tm_mon:02d}-{current_time.tm_mday:02d} "
                  f"{current_time.tm_hour:02d}:{current_time.tm_min:02d}:{current_time.tm_sec:02d}")

        except Exception as e:
            print(f"Time sync failed: {e}")
            print("Continuing with system time...")

    def feed_watchdog(self):
        """Feed the watchdog timer to prevent reboot"""
        try:
            wdt.feed()
        except Exception as e:
            if not self._wdt_feed_warned:
                print(f"Watchdog feed failed (further errors suppressed): {e}")
                self._wdt_feed_warned = True

    def maybe_resync_ntp(self):
        """Keep DST aligned with forecasts and re-sync NTP daily at 3 AM."""
        current_time = time.localtime()
        if self.clock_offset is not None:
            utc_seconds = time.mktime(current_time) - self.clock_offset
            local = local_forecast_time(utc_seconds, TIMEZONE_OFFSET, DST_AUTO)
            offset = time.mktime(local) - utc_seconds
            if offset != self.clock_offset:
                rtc.RTC().datetime = local
                self.clock_offset = offset
                current_time = local
                print("Adjusted clock for daylight saving transition")
        if current_time.tm_hour == 3 and self.last_ntp_sync != current_time.tm_mday:
            print("Daily NTP re-sync triggered...")
            try:
                if not self.check_wifi_connection():
                    self.reconnect_wifi()
                if self.pool is None:
                    self.pool = socketpool.SocketPool(wifi.radio)
                self.sync_time(self.pool)
                self.last_ntp_sync = current_time.tm_mday
                print("NTP re-sync complete")
            except Exception as e:
                print(f"NTP re-sync failed: {e}")

    def dump_display(self, label=""):
        """Print a colored 24x8 snapshot of all three matrices to serial."""
        if not DISPLAY_DUMP:
            return
        if not (self.matrix1 and self.matrix2 and self.matrix3):
            return
        try:
            t = time.localtime()
            ts = f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}"
            print(f"--- dump[{ts}] {label} ---")
            RESET = "\x1b[0m"
            # 0=OFF 1=RED 2=GREEN 3=YELLOW (HT16K33 Matrix8x8x2 values)
            colors = {0: "\x1b[38;5;238m", 1: "\x1b[91m",
                      2: "\x1b[92m", 3: "\x1b[93m"}
            # Iterate y from 7 down to 0 so the dump matches physical display
            # orientation (matrix row 0 is at the bottom of the real panel).
            for y in range(7, -1, -1):
                row = ""
                for mi, m in enumerate((self.matrix1, self.matrix2, self.matrix3)):
                    if mi:
                        row += RESET + "|"
                    for x in range(8):
                        try:
                            c = m[x, y]
                        except Exception:
                            c = 0
                        row += colors.get(c, "") + "\u2588\u2588"
                print(row + RESET)
        except Exception as e:
            print(f"dump_display error: {e}")

    def _draw_char(self, matrix, char, x_offset, y_offset, color):
        """Draw a single 3x5 character on a matrix at the given offset"""
        cols = FONT_3X5.get(char)
        if not cols:
            return
        for cx, col_bits in enumerate(cols):
            for ry in range(5):
                if col_bits & (1 << ry):
                    px = x_offset + cx
                    py = y_offset + (4 - ry)  # Flip vertically so top row draws at higher Y
                    if 0 <= px < 8 and 0 <= py < 8:
                        matrix[px, py] = color

    def _draw_string(self, matrix, text, y_offset, color):
        """Draw a short string centered on an 8x8 matrix"""
        # Each char is 3px wide + 1px gap
        total_width = len(text) * 4 - 1
        x_start = max(0, (8 - total_width) // 2)
        for i, ch in enumerate(text):
            self._draw_char(matrix, ch, x_start + i * 4, y_offset, color)

    def show_boot_info(self):
        """Show date then time across all 3 matrices on boot"""
        if not (self.matrix1 and self.matrix2 and self.matrix3):
            print("Cannot show boot info - matrices not available")
            return

        current_time = time.localtime()
        month_str = f"{current_time.tm_mon:02d}"
        day_str = f"{current_time.tm_mday:02d}"
        hour_str = f"{current_time.tm_hour:02d}"
        min_str = f"{current_time.tm_min:02d}"

        # --- Phase 1: Date across all 3 matrices (MM / DD) ---
        self.clear_matrices()
        # Matrix 1: month (MM) in green, centered
        self._draw_string(self.matrix1, month_str, 1, self.matrix1.LED_GREEN)
        # Matrix 2: slash in green, centered
        self._draw_string(self.matrix2, "/", 1, self.matrix2.LED_GREEN)
        # Matrix 3: day (DD) in green, centered
        self._draw_string(self.matrix3, day_str, 1, self.matrix3.LED_GREEN)

        print(f"Boot display: {month_str} / {day_str}")
        self.dump_display(f"boot: date {month_str}/{day_str}")
        print("Showing date for 3 seconds...")
        time.sleep(3)

        # --- Phase 2: Time across all 3 matrices (HH : MM) ---
        self.clear_matrices()
        # Matrix 1: hours (HH) in green, centered
        self._draw_string(self.matrix1, hour_str, 1, self.matrix1.LED_GREEN)
        # Matrix 2: colon in green, centered
        self._draw_string(self.matrix2, ":", 1, self.matrix2.LED_GREEN)
        # Matrix 3: minutes (MM) in green, centered
        self._draw_string(self.matrix3, min_str, 1, self.matrix3.LED_GREEN)

        print(f"Boot display: {hour_str} : {min_str}")
        self.dump_display(f"boot: time {hour_str}:{min_str}")
        print("Showing time for 3 seconds...")
        time.sleep(3)

        self.clear_matrices()
        self.dump_display("boot: cleared")
        print("Boot sequence complete, loading temperature forecast...")
            
    def show_api_status(self, status):
        """Show API fetch status on matrix1 corner pixel.
        'fetching' = yellow, 'ok' = green, 'fail' = red, 'clear' = off"""
        if not self.matrix1:
            return
        if status == 'fetching':
            self.matrix1[7, 7] = self.matrix1.LED_YELLOW
        elif status == 'ok':
            self.matrix1[7, 7] = self.matrix1.LED_GREEN
        elif status == 'fail':
            self.matrix1[7, 7] = self.matrix1.LED_RED
        elif status == 'clear':
            self.matrix1[7, 7] = 0
        self.dump_display(f"api status: {status}")

    def request_json(self, url):
        """Fetch NWS JSON and always release the response socket."""
        self.feed_watchdog()
        response = self.requests.get(
            url,
            headers={"User-Agent": WEATHER_USER_AGENT, "Accept": "application/geo+json"},
            timeout=20,
        )
        try:
            if response.status_code != 200:
                raise RuntimeError("NWS request failed: {} ({})".format(response.status_code, url))
            return response.json()
        finally:
            response.close()
            self.feed_watchdog()
            gc.collect()

    def fetch_temperature_data(self, max_retries=3):
        """Fetch today's NWS grid temperatures, including coastal marine grids."""
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # Check WiFi connection first
                if not self.check_wifi_connection():
                    print("WiFi disconnected, attempting to reconnect...")
                    if not self.reconnect_wifi():
                        raise Exception("WiFi reconnection failed")
                
                # Ensure we have a valid requests session
                if not hasattr(self, 'requests') or self.requests is None:
                    print("Creating new requests session...")
                    self.pool = socketpool.SocketPool(wifi.radio)
                    self.requests = adafruit_requests.Session(self.pool, ssl.create_default_context())
                
                current_time = time.localtime()
                date = (current_time.tm_year, current_time.tm_mon, current_time.tm_mday)
                if self.forecast_url is None or self.forecast_url_date != date:
                    point_url = "{}/points/{:.4f},{:.4f}".format(
                        API_URL, WEATHER_LATITUDE, WEATHER_LONGITUDE
                    )
                    point = self.request_json(point_url)
                    self.forecast_url = point["properties"]["forecastGridData"]
                    if not self.forecast_url.startswith(API_URL + "/gridpoints/"):
                        raise ValueError("Unexpected NWS forecast grid URL")
                    self.forecast_url_date = date
                    del point
                    gc.collect()

                print(f"Fetching temperature forecast (attempt {attempt + 1}/{max_retries})...")
                data = self.request_json(self.forecast_url)
                current_time = time.localtime()
                date = (current_time.tm_year, current_time.tm_mon, current_time.tm_mday)
                try:
                    forecast = parse_temperature_forecast(data, date, TIMEZONE_OFFSET, DST_AUTO)
                finally:
                    del data
                    gc.collect()
                print("Temperature forecast received successfully")
                print(f"Free memory after fetch: {gc.mem_free()} bytes")
                return forecast
                    
            except Exception as e:
                last_error = e
                print(f"Attempt {attempt + 1} failed: {e}")
                self.forecast_url = None
                
                # On socket errors, try to recreate the session
                if "socket" in str(e).lower():
                    print("Socket error detected, recreating session...")
                    try:
                        self.pool = socketpool.SocketPool(wifi.radio)
                        self.requests = adafruit_requests.Session(self.pool, ssl.create_default_context())
                    except Exception as session_error:
                        print(f"Session recreation failed: {session_error}")
                
                if attempt < max_retries - 1:
                    # Exponential backoff: 30s, 60s, 120s
                    wait_time = 30 * (2 ** attempt)
                    print(f"Waiting {wait_time} seconds before retry...")
                    for _ in range(wait_time // 10):
                        self.feed_watchdog()
                        time.sleep(10)
                    
        print(f"All {max_retries} attempts failed. Last error: {last_error}")
        return None
    
    def clear_matrices(self):
        """Clear all LED matrices"""
        if self.matrix1:
            self.matrix1.fill(0)
        if self.matrix2:
            self.matrix2.fill(0)
        if self.matrix3:
            self.matrix3.fill(0)
    
    def display_on_matrices(self, forecast, stale=False):
        """Display fixed Fahrenheit bands in midnight-to-11-PM columns."""
        if not forecast or not (self.matrix1 and self.matrix2 and self.matrix3):
            print("Cannot display on matrices - data or matrices not available")
            return

        current_time = time.localtime()
        self.clear_matrices()
        if forecast["date"] != (current_time.tm_year, current_time.tm_mon, current_time.tm_mday):
            print("Refusing to display a forecast from a different calendar day")
            return
        current_hour = current_time.tm_hour
        matrices = [self.matrix1, self.matrix2, self.matrix3]
        for hour, temperature in enumerate(forecast["temperatures"]):
            if temperature is None:
                continue
            matrix = matrices[hour // 8]
            color = matrix.LED_GREEN if stale else matrix.LED_RED
            if hour == current_hour:
                color = matrix.LED_YELLOW
            matrix[hour % 8, temperature_row(temperature)] = color
        self.dump_display(f"{'stale ' if stale else ''}temperature chart (current hour {current_hour:02d})")
    
    def show_error_on_matrices(self):
        """Show error pattern on matrices when data fetch fails"""
        if not (self.matrix1 and self.matrix2 and self.matrix3):
            return
            
        # Clear matrices
        self.clear_matrices()
        
        # Show red X pattern on middle matrix to indicate error
        matrix = self.matrix2
        for i in range(8):
            matrix[i, i] = matrix.LED_RED      # Main diagonal
            matrix[i, 7-i] = matrix.LED_RED    # Counter diagonal

        print("Error pattern displayed on LED matrices")
        self.dump_display("error X pattern")
        
    def show_safe_mode_on_matrices(self):
        """Show safe mode pattern for extended failures"""
        if not (self.matrix1 and self.matrix2 and self.matrix3):
            return
            
        # Clear matrices
        self.clear_matrices()
        
        # Show yellow warning pattern on all matrices
        matrices = [self.matrix1, self.matrix2, self.matrix3]
        for matrix in matrices:
            # Show blinking border pattern
            for i in range(8):
                matrix[0, i] = matrix.LED_YELLOW    # Top row
                matrix[7, i] = matrix.LED_YELLOW    # Bottom row
                matrix[i, 0] = matrix.LED_YELLOW    # Left column
                matrix[i, 7] = matrix.LED_YELLOW    # Right column

        print("Safe mode pattern displayed on LED matrices")
        self.dump_display("safe mode border")
    
    def run_continuous(self):
        """Refresh forecasts hourly while keeping the local-day chart aligned."""
        print("Starting Temperature Forecast Clock (Continuous Mode)...")
        forecast = None
        last_display_hour = None
        last_attempt_hour = None
        next_fetch = 0
        consecutive_failures = 0
        data_is_stale = False

        while True:
            try:
                self.feed_watchdog()
                self.maybe_resync_ntp()
                current_time = time.localtime()
                current_hour = current_time.tm_hour
                current_date = (current_time.tm_year, current_time.tm_mon, current_time.tm_mday)
                hour_key = current_date + (current_hour,)
                if forecast is not None and forecast["date"] != current_date:
                    forecast = None
                    self.clear_matrices()
                    next_fetch = 0
                    last_display_hour = None

                if time.monotonic() >= next_fetch or hour_key != last_attempt_hour:
                    last_attempt_hour = hour_key
                    self.show_api_status('fetching')
                    incoming = self.fetch_temperature_data()
                    current_time = time.localtime()
                    current_hour = current_time.tm_hour
                    current_date = (current_time.tm_year, current_time.tm_mon, current_time.tm_mday)
                    hour_key = current_date + (current_hour,)
                    if forecast is not None and forecast["date"] != current_date:
                        forecast = None
                    if incoming is not None and incoming["date"] != current_date:
                        print("Forecast day changed during fetch; retrying for the new day")
                        incoming = None
                    self.show_api_status('ok' if incoming is not None else 'fail')
                    if incoming is not None:
                        forecast = merge_past_forecasts(forecast, incoming, current_hour)
                        consecutive_failures = 0
                        data_is_stale = False
                        next_fetch = time.monotonic() + 3600
                        self.display_ascii_chart(forecast)
                    else:
                        consecutive_failures += 1
                        data_is_stale = True
                        print(f"Temperature forecast failed (failure #{consecutive_failures})")
                        if consecutive_failures <= 3:
                            retry_delay = 300
                        elif consecutive_failures <= 6:
                            retry_delay = 900
                        else:
                            retry_delay = 1800
                        next_fetch = time.monotonic() + retry_delay
                        print(f"Retrying in {retry_delay // 60} minutes...")
                    last_display_hour = None

                if hour_key != last_display_hour:
                    if consecutive_failures >= 5:
                        self.show_safe_mode_on_matrices()
                    elif forecast is not None:
                        self.display_on_matrices(forecast, stale=data_is_stale)
                    else:
                        self.show_error_on_matrices()
                    last_display_hour = hour_key

                gc.collect()
                for _ in range(3):
                    self.feed_watchdog()
                    time.sleep(10)
            except Exception as e:
                print(f"Error in main loop: {e}")
                consecutive_failures += 1
                self.show_error_on_matrices()
                data_is_stale = True
                next_fetch = time.monotonic() + min(300 * consecutive_failures, 1800)
                last_display_hour = None
                for _ in range(3):
                    self.feed_watchdog()
                    time.sleep(10)
    
    def display_ascii_chart(self, forecast):
        """Mirror the fixed temperature bands and missing hours in the console."""
        if not forecast:
            print("No temperature forecast to display")
            return
        temperatures = forecast["temperatures"]
        print("\n" + "="*60)
        print("24-HOUR TEMPERATURE FORECAST (F)")
        print("="*60)
        labels = ("<40", "40-49", "50-59", "60-69", "70-79", "80-89", "90-99", "100+")
        for row in range(7, -1, -1):
            line = f"{labels[row]:>5} |"
            for temperature in temperatures:
                line += " *" if temperature is not None and temperature_row(temperature) == row else "  "
            print(line)
        print("      +" + "-" * 48)
        print("       " + "".join(f"{hour:02d}" for hour in range(24)))
        print("\nDate: {:04d}-{:02d}-{:02d}".format(*forecast["date"]))
        print(f"Location: {WEATHER_LATITUDE}, {WEATHER_LONGITUDE}")
        print("Units: degrees Fahrenheit; blank columns = unavailable")
        for hour, temperature in enumerate(temperatures):
            value = "unavailable" if temperature is None else f"{temperature:.1f} F"
            print(f"{hour:02d}:00: {value}")
    
    def run_once(self):
        """Fetch and display today's temperature forecast once."""
        print("Starting Temperature Forecast Clock (Single Run)...")
        
        self.show_api_status('fetching')
        forecast = self.fetch_temperature_data()
        if forecast is not None:
            self.show_api_status('ok')
            self.display_ascii_chart(forecast)
            self.display_on_matrices(forecast)
        else:
            self.show_api_status('fail')
            print("Failed to get temperature forecast")
            self.show_error_on_matrices()

def main():
    """Entry point for simple version"""
    # Check WiFi credentials
    if not WIFI_NETWORKS:
        print("ERROR: Please update WiFi credentials in settings.toml!")
        print("Add your WIFI_SSID and WIFI_PASSWORD to settings.toml")
        return
    
    temperature_display = TemperatureDisplay()
    
    # Run in continuous mode for clock-like behavior
    temperature_display.run_continuous()

if __name__ == "__main__":
    main()