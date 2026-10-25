"""Run on CircuitPython to check the forecast without connected LED matrices.

Copy this script beside code.py and forecast.py on CIRCUITPY and run it manually.
"""

from code import TemperatureDisplay, WIFI_NETWORKS


class SerialTemperatureDisplay(TemperatureDisplay):
    def setup_matrices(self):
        self.matrix1 = None
        self.matrix2 = None
        self.matrix3 = None


def main():
    if not WIFI_NETWORKS:
        raise RuntimeError("Configure WIFI_SSID and WIFI_PASSWORD in settings.toml")
    display = SerialTemperatureDisplay()
    forecast = display.fetch_temperature_data()
    if forecast is None:
        raise RuntimeError("Failed to get temperature forecast; see serial log")
    display.display_ascii_chart(forecast)


if __name__ == "__main__":
    main()
