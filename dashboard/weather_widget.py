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
    """Map an Open-Meteo weather code to a human description."""
    return _WX_DESC.get(code, f"Code {code}")


def _deg_dir(deg: float) -> str:
    """Convert a wind direction in degrees to an 8-point compass label."""
    return _DIRS[round(deg / 45) % 8]


def _at(seq, i, default="--", nd=1):
    """Safely round seq[i] to nd places, returning default if out of range."""
    return round(seq[i], nd) if i < len(seq) else default


def _parse_forecast(d: dict) -> list[dict]:
    """Build the 7-day forecast list from an Open-Meteo 'daily' block."""
    forecast = []
    for i, day in enumerate(d.get("time", [])):
        forecast.append({
            "date":      day,
            "desc":      _wcode(d["weathercode"][i] if i < len(d.get("weathercode", [])) else 0),
            "t_max":     _at(d.get("temperature_2m_max", []), i),
            "t_min":     _at(d.get("temperature_2m_min", []), i),
            "rain_pct":  d["precipitation_probability_max"][i] if i < len(d.get("precipitation_probability_max", [])) else 0,
            "wind_kmh":  _at(d.get("wind_speed_10m_max", []), i),
            "precip_mm": _at(d.get("precipitation_sum", []), i, default=0),
        })
    return forecast


class WeatherCache:
    TTL = 300   # 5 minutes

    def __init__(self, lat: float = 18.1617, lon: float = 75.4218,
                 location: str = "Barloni, Solapur"):
        """Init the weather cache for a location (default: Barloni, Solapur)."""
        self.lat = lat
        self.lon = lon
        self.location = location
        self._data: dict = {}
        self._fetched_at: float = 0.0

    def get(self) -> dict:
        """Return cached weather if fresh (within TTL), else fetch anew."""
        age = time.time() - self._fetched_at
        if self._data and age < self.TTL:
            return {**self._data, "age_min": int(age / 60)}
        return self._fetch()

    def _build_current(self, c: dict, forecast: list[dict]) -> dict:
        """Assemble the current-weather display dict from Open-Meteo 'current' data."""
        return {
            "temp_c":        round(c["temperature_2m"], 1),
            "feels_like_c":  round(c.get("apparent_temperature", c["temperature_2m"]), 1),
            "humidity":      c.get("relative_humidity_2m", 0),
            "wind_kmh":      round(c.get("wind_speed_10m", 0), 1),
            "wind_dir":      _deg_dir(c.get("wind_direction_10m", 0)),
            "description":   _wcode(c.get("weathercode", 0)),
            "visibility_km": round(c.get("visibility", 10000) / 1000, 1),
            "location":      self.location,
            "updated_at":    time.strftime("%H:%M"),
            "age_min":       0,
            "forecast":      forecast,
        }

    def _fetch(self) -> dict:
        """Fetch current + 7-day forecast from Open-Meteo; falls back to last data on error."""
        try:
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={self.lat}&longitude={self.lon}"
                "&current=temperature_2m,relative_humidity_2m,wind_speed_10m,"
                "wind_direction_10m,weathercode,apparent_temperature,visibility"
                "&daily=weathercode,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,wind_speed_10m_max,precipitation_sum"
                "&wind_speed_unit=kmh&forecast_days=7"
                "&timezone=Asia%2FKolkata"
            )
            r = httpx.get(url, timeout=10.0)
            r.raise_for_status()
            resp = r.json()
            forecast = _parse_forecast(resp.get("daily", {}))

            self._fetched_at = time.time()
            self._data = self._build_current(resp["current"], forecast)
            log.info("weather+forecast: %s°C %s, %d days",
                     self._data["temp_c"], self._data["description"], len(forecast))
            return self._data

        except Exception as e:
            log.error("weather fetch failed: %s", e)
            if self._data:
                return {**self._data, "age_min": int((time.time() - self._fetched_at) / 60)}
            return {
                "temp_c": "--", "feels_like_c": "--", "humidity": "--",
                "wind_kmh": "--", "wind_dir": "--", "description": "Unavailable",
                "visibility_km": "--", "location": self.location,
                "updated_at": "--", "age_min": 0, "forecast": [],
            }
