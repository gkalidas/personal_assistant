"""
Live weather from Open-Meteo (same source as farming module).
Cached for 5 minutes; stale data shown with age if fetch fails.
"""

import logging
import time
import httpx

log = logging.getLogger(__name__)

_WX_DESC = {
    0: "Clear Sky",       1: "Mainly Clear",    2: "Partly Cloudy",  3: "Overcast",
    45: "Fog",            48: "Icy Fog",
    51: "Light Drizzle",  53: "Drizzle",         55: "Heavy Drizzle",
    61: "Light Rain",     63: "Rain",            65: "Heavy Rain",
    71: "Light Snow",     73: "Snow",            75: "Heavy Snow",
    77: "Snow Grains",
    80: "Light Showers",  81: "Showers",         82: "Heavy Showers",
    85: "Snow Showers",   86: "Heavy Snow Showers",
    95: "Thunderstorm",   96: "T-Storm + Hail",  99: "Severe T-Storm",
}

_DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _wcode(code: int) -> str:
    return _WX_DESC.get(code, f"Code {code}")


def _deg_dir(deg: float) -> str:
    return _DIRS[round(deg / 45) % 8]


class WeatherCache:
    TTL = 300   # 5 minutes

    def __init__(self, lat: float = 17.6667, lon: float = 75.8000,
                 location: str = "Barloni, Solapur"):
        self.lat = lat
        self.lon = lon
        self.location = location
        self._data: dict = {}
        self._fetched_at: float = 0.0

    def get(self) -> dict:
        age = time.time() - self._fetched_at
        if self._data and age < self.TTL:
            return {**self._data, "age_min": int(age / 60)}
        return self._fetch()

    def _fetch(self) -> dict:
        try:
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={self.lat}&longitude={self.lon}"
                "&current=temperature_2m,relative_humidity_2m,wind_speed_10m,"
                "wind_direction_10m,weathercode,apparent_temperature,visibility"
                "&wind_speed_unit=kmh&forecast_days=1"
            )
            r = httpx.get(url, timeout=10.0)
            r.raise_for_status()
            c = r.json()["current"]

            self._fetched_at = time.time()
            self._data = {
                "temp_c":       round(c["temperature_2m"], 1),
                "feels_like_c": round(c.get("apparent_temperature", c["temperature_2m"]), 1),
                "humidity":     c.get("relative_humidity_2m", 0),
                "wind_kmh":     round(c.get("wind_speed_10m", 0), 1),
                "wind_dir":     _deg_dir(c.get("wind_direction_10m", 0)),
                "description":  _wcode(c.get("weathercode", 0)),
                "visibility_km":round(c.get("visibility", 10000) / 1000, 1),
                "location":     self.location,
                "updated_at":   time.strftime("%H:%M"),
                "age_min":      0,
            }
            log.info("weather: %s°C %s", self._data["temp_c"], self._data["description"])
            return self._data

        except Exception as e:
            log.error("weather fetch failed: %s", e)
            if self._data:
                age = int((time.time() - self._fetched_at) / 60)
                return {**self._data, "age_min": age}
            return {
                "temp_c": "--", "feels_like_c": "--", "humidity": "--",
                "wind_kmh": "--", "wind_dir": "--", "description": "Unavailable",
                "visibility_km": "--", "location": self.location,
                "updated_at": "--", "age_min": 0,
            }
