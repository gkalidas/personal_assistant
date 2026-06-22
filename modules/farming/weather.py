"""
Open-Meteo weather integration — free, no API key, ECMWF-backed.
Covers: current conditions, 7-day forecast, spray-safe check, ERA5 historical rainfall.
Cache: 6-hour SQLite cache in farming.db to avoid hammering the API.
"""

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from modules.farming.geocode import resolve

FARMING_DB = os.getenv("FARMING_DB", "farming.db")
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"
CACHE_TTL_H  = 6

# Default: Pune, Maharashtra
DEFAULT_LAT = 18.52
DEFAULT_LON = 73.85

_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow",
    80: "slight showers", 81: "moderate showers", 82: "violent showers",
    95: "thunderstorm", 96: "thunderstorm + hail", 99: "thunderstorm + heavy hail",
}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _ensure_cache_table() -> None:
    """Create the weather_cache table if absent."""
    with sqlite3.connect(FARMING_DB) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS weather_cache (
                cache_key  TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL,
                data_json  TEXT NOT NULL
            )
        """)


def _get_cached(key: str) -> dict | None:
    """Return cached weather data for a key if within the 6h TTL, else None."""
    try:
        with sqlite3.connect(FARMING_DB) as c:
            row = c.execute(
                "SELECT fetched_at, data_json FROM weather_cache WHERE cache_key=?", (key,)
            ).fetchone()
        if not row:
            return None
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() / 3600
        return json.loads(row[1]) if age_h < CACHE_TTL_H else None
    except Exception:
        return None


def _save_cache(key: str, data: dict) -> None:
    """Write weather data to the cache (best-effort)."""
    try:
        with sqlite3.connect(FARMING_DB) as c:
            c.execute(
                "INSERT OR REPLACE INTO weather_cache VALUES (?,?,?)",
                (key, datetime.now(timezone.utc).isoformat(), json.dumps(data)),
            )
    except Exception:
        pass


def _get(url: str, params: dict) -> dict:
    """HTTP GET a JSON weather endpoint and return the parsed body."""
    resp = httpx.get(url, params=params, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


# ── Coords resolver ───────────────────────────────────────────────────────────

def _coords(
    location: str | None,
    lat: float | None,
    lon: float | None,
    name: str | None = None,
) -> tuple[float, float, str]:
    """Return (lat, lon, display_name). Geocodes if location string given, falls back to default."""
    if lat is not None and lon is not None:
        # Use explicit name if provided (e.g. from user profile), else geocode back or use coords
        return lat, lon, name or location or f"{lat:.2f},{lon:.2f}"
    if location:
        result = resolve(location)
        if result:
            return result
    return DEFAULT_LAT, DEFAULT_LON, "Pune"


# ── Public API ────────────────────────────────────────────────────────────────

def current_conditions(
    location: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Current weather: temp, humidity, wind, rain, sky description."""
    _ensure_cache_table()
    rlat, rlon, name = _coords(location, lat, lon, name)
    cache_key = f"current:{rlat:.4f},{rlon:.4f}"
    cached = _get_cached(cache_key)
    if cached:
        # Override stale coordinate-string location with real name if available
        if name:
            return {**cached, "location": name}
        return cached

    data = _get(FORECAST_URL, {
        "latitude": rlat, "longitude": rlon,
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
        "timezone": "Asia/Kolkata",
    })
    c = data.get("current", {})
    result = {
        "location": name,
        "temperature_c": c.get("temperature_2m"),
        "humidity_pct": c.get("relative_humidity_2m"),
        "rain_mm": c.get("precipitation", 0),
        "wind_kmh": c.get("wind_speed_10m"),
        "description": _WMO.get(c.get("weather_code", 0), "unknown"),
        "weather_code": c.get("weather_code"),
    }
    _save_cache(cache_key, result)
    return result


def forecast(
    location: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    days: int = 7,
    name: str | None = None,
) -> dict[str, Any]:
    """Daily forecast: rain, temp, soil moisture, evapotranspiration."""
    _ensure_cache_table()
    rlat, rlon, name = _coords(location, lat, lon, name)
    cache_key = f"forecast:{rlat:.4f},{rlon:.4f}:{days}"
    cached = _get_cached(cache_key)
    if cached:
        if name:
            return {**cached, "location": name}
        return cached

    data = _get(FORECAST_URL, {
        "latitude": rlat, "longitude": rlon,
        "daily": ",".join([
            "precipitation_sum", "rain_sum", "precipitation_probability_max",
            "temperature_2m_max", "temperature_2m_min",
            "et0_fao_evapotranspiration", "windspeed_10m_max",
        ]),
        "hourly": "soil_moisture_0_to_1cm,soil_temperature_0cm",
        "timezone": "Asia/Kolkata",
        "forecast_days": days,
    })
    result = _parse_daily_forecast(data, name)
    _save_cache(cache_key, result)
    return result


def _parse_daily_forecast(data: dict, name: str) -> dict[str, Any]:
    """Turn an Open-Meteo forecast response into {location, days[], soil_*_now}."""
    daily = data.get("daily", {})
    dates = daily.get("time", [])

    def col(key: str) -> list:
        """Return the daily column for `key`, padded to the date count."""
        return daily.get(key) or [None] * len(dates)

    keys = {
        "rain_mm": "rain_sum", "precipitation_mm": "precipitation_sum",
        "rain_probability_pct": "precipitation_probability_max",
        "temp_max_c": "temperature_2m_max", "temp_min_c": "temperature_2m_min",
        "evapotranspiration_mm": "et0_fao_evapotranspiration", "wind_max_kmh": "windspeed_10m_max",
    }
    cols = {out: col(src) for out, src in keys.items()}
    result_days = [{"date": d, **{out: cols[out][i] for out in keys}} for i, d in enumerate(dates)]

    hourly = data.get("hourly", {})
    return {
        "location": name,
        "days": result_days,
        "soil_moisture_now": (hourly.get("soil_moisture_0_to_1cm") or [None])[0],
        "soil_temp_now_c": (hourly.get("soil_temperature_0cm") or [None])[0],
    }


def _hourly_rain(rlat: float, rlon: float, target_date: str) -> list[dict]:
    """Fetch hourly precipitation for a specific date. Returns list of {hour, rain_mm, prob_pct}."""
    cache_key = f"hourly_rain:{rlat:.4f},{rlon:.4f}:{target_date}"
    _ensure_cache_table()
    cached = _get_cached(cache_key)
    if cached:
        return cached

    data = _get(FORECAST_URL, {
        "latitude": rlat, "longitude": rlon,
        "hourly": "precipitation,precipitation_probability",
        "timezone": "Asia/Kolkata",
        "forecast_days": 3,
    })

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    rain_vals = hourly.get("precipitation", [])
    prob_vals = hourly.get("precipitation_probability", [])

    result = []
    for i, t in enumerate(times):
        if t.startswith(target_date):
            hour = t[11:16]  # "HH:MM"
            result.append({
                "hour": hour,
                "rain_mm": rain_vals[i] if i < len(rain_vals) else 0,
                "prob_pct": prob_vals[i] if i < len(prob_vals) else 0,
            })

    _save_cache(cache_key, result)
    return result


def spray_safe_tomorrow(
    location: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Is tomorrow safe to spray? Checks rain, rain probability, wind, with hourly breakdown."""
    rlat, rlon, name = _coords(location, lat, lon, name)
    fc = forecast(lat=rlat, lon=rlon, days=2, name=name)
    tomorrow = fc["days"][1] if len(fc["days"]) > 1 else fc["days"][0]
    curr = current_conditions(lat=rlat, lon=rlon, name=name)

    rain_mm   = tomorrow.get("rain_mm") or 0
    rain_prob = tomorrow.get("rain_probability_pct") or 0
    wind      = tomorrow.get("wind_max_kmh") or 0
    humidity  = curr.get("humidity_pct") or 0
    temp_max  = tomorrow.get("temp_max_c") or 0

    # Hourly breakdown for tomorrow
    hourly = _hourly_rain(rlat, rlon, tomorrow["date"])
    rainy_hours = [h for h in hourly if (h["rain_mm"] or 0) >= 0.1 or (h["prob_pct"] or 0) >= 40]
    dry_windows = [h["hour"] for h in hourly if (h["rain_mm"] or 0) < 0.1 and (h["prob_pct"] or 0) < 40]

    reasons = _spray_unsafe_reasons(rain_mm, rain_prob, wind, humidity, temp_max)
    return {
        "date": tomorrow["date"],
        "safe_to_spray": not reasons,
        "reasons": reasons if reasons else ["conditions look good"],
        "rain_mm": rain_mm,
        "rain_probability_pct": rain_prob,
        "wind_kmh": wind,
        "humidity_pct": humidity,
        "temp_max_c": temp_max,
        "hourly_rain": hourly,
        "rainy_hours": [h["hour"] for h in rainy_hours],
        "dry_windows": dry_windows,
    }


def _spray_unsafe_reasons(rain_mm, rain_prob, wind, humidity, temp_max) -> list[str]:
    """Return the reasons tomorrow is unsafe to spray (empty list = safe)."""
    reasons = []
    if rain_mm >= 2.0:
        reasons.append(f"rain expected {rain_mm}mm total (washes off contact sprays)")
    if rain_prob >= 30:
        reasons.append(f"rain probability {rain_prob}%")
    if wind >= 20:
        reasons.append(f"wind {wind}km/h (spray drift risk)")
    if humidity > 85:
        reasons.append(f"humidity {humidity}% today (delay contact sprays)")
    if temp_max > 35:
        reasons.append(f"max temp {temp_max}°C tomorrow (phytotoxicity risk 11am–3pm)")
    return reasons


def crop_history(
    lat: float,
    lon: float,
    planted_date: str,
    location_name: str = "",
) -> dict[str, Any]:
    """ERA5 historical weather from planting date to today. Summarises risk periods."""
    planted_at = date.fromisoformat(planted_date)
    today = date.today()

    cache_key = f"history:{lat:.4f},{lon:.4f}:{planted_date}"
    _ensure_cache_table()

    cached = _get_cached(cache_key)
    if cached and cached.get("_fetched_date") == today.isoformat():
        return cached

    data = _get(ARCHIVE_URL, {
        "latitude": lat, "longitude": lon,
        "start_date": planted_date,
        "end_date": today.isoformat(),
        "daily": ",".join([
            "temperature_2m_max", "temperature_2m_min",
            "precipitation_sum", "relative_humidity_2m_max", "weather_code",
        ]),
        "timezone": "Asia/Kolkata",
    })

    result = {
        "location": location_name,
        "planted_at": planted_date,
        "days_since_plant": (today - planted_at).days,
        **_crop_weather_stats(data),
        "_fetched_date": today.isoformat(),
    }
    _save_cache(cache_key, result)
    return result


def _crop_weather_stats(data: dict) -> dict[str, Any]:
    """Compute rain/temp/humidity risk stats from an ERA5 daily archive response."""
    daily   = data.get("daily", {})
    rain    = daily.get("precipitation_sum", [])
    t_max   = daily.get("temperature_2m_max", [])
    hum_max = daily.get("relative_humidity_2m_max", [])
    return {
        "total_rain_mm": round(sum(r for r in rain if r is not None), 1),
        "avg_max_temp_c": round(sum(t for t in t_max if t is not None) / max(len(t_max), 1), 1),
        "humid_days_over80": sum(1 for h in hum_max if h is not None and h > 80),
        "heavy_rain_days_over20mm": sum(1 for r in rain if r is not None and r > 20),
        "last_7d_rain_mm": round(sum(r for r in rain[-7:] if r is not None), 1),
        "last_7d_humid_days": sum(1 for h in hum_max[-7:] if h is not None and h > 80),
    }


def historical_rainfall(
    location: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    start: str | None = None,
    end: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Monthly rainfall totals for a date range. Default: current year to 5 days ago.
    ERA5 archive has a ~5-day lag — requesting recent dates causes a 400 error."""
    today = date.today()
    # ERA5 archive data lags ~5 days; cap end_date to avoid 400 errors
    safe_end = today - timedelta(days=5)
    start = start or f"{today.year}-01-01"
    end   = end   or safe_end.isoformat()
    # If caller passed an end date that's too recent, cap it
    if end > safe_end.isoformat():
        end = safe_end.isoformat()
    # If start > end (e.g. querying first week of January), return empty
    if start > end:
        return {"location": "", "start": start, "end": end,
                "monthly_mm": {}, "total_mm": 0.0}
    rlat, rlon, name = _coords(location, lat, lon, name)

    data = _get(ARCHIVE_URL, {
        "latitude": rlat, "longitude": rlon,
        "start_date": start, "end_date": end,
        "daily": "precipitation_sum,rain_sum",
        "timezone": "Asia/Kolkata",
    })

    monthly = _monthly_rainfall(data)
    return {
        "location": name,
        "start": start,
        "end": end,
        "monthly_mm": monthly,
        "total_mm": round(sum(monthly.values()), 1),
    }


def _monthly_rainfall(data: dict) -> dict[str, float]:
    """Aggregate an ERA5 daily rain_sum response into {YYYY-MM: mm}."""
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    rain  = daily.get("rain_sum", [0.0] * len(dates))
    monthly: dict[str, float] = {}
    for d, r in zip(dates, rain):
        month = d[:7]
        monthly[month] = round(monthly.get(month, 0.0) + (r or 0.0), 1)
    return monthly
