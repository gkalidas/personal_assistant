"""
NDVI crop health via NASA MODIS Web Service (MOD13Q1 — 16-day composite, 250m).

Free, no API key, no registration. Direct from NASA ORNL DAAC.
Data is 16-day composite updated ~monthly. Covers Barloni and any lat/lon.

NDVI scale factor: raw_value × 0.0001 → range [-1.0 to 1.0]
  < 0.1  — bare soil / water
  0.1–0.3 — sparse / stressed vegetation
  0.3–0.5 — moderate crop cover
  0.5–0.7 — healthy dense crop
  > 0.7  — very healthy / dense vegetation
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://modis.ornl.gov/rst/api/v1"
_PRODUCT  = "MOD13Q1"          # MODIS Terra 16-day NDVI, 250m resolution
_BAND     = "250m_16_days_NDVI"
_SCALE    = 0.0001             # NASA MODIS scale factor
_FILL_VAL = -3000              # MODIS fill value (no data)

# Barloni default
_DEFAULT_LAT = 18.1617
_DEFAULT_LON  = 75.4218

# Cache in SQLite (same personal_assistant.db, new table)
_DB_PATH = Path(__file__).parent.parent.parent / "personal_assistant.db"
_CACHE_TTL = 12 * 3600  # 12 hours — MODIS data is 16-day composite, no need to refetch often


def _conn():
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS ndvi_cache (
                key        TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                fetched_at REAL NOT NULL
            )
        """)


def _cache_get(key: str) -> dict | None:
    try:
        with _conn() as c:
            row = c.execute("SELECT data, fetched_at FROM ndvi_cache WHERE key=?", (key,)).fetchone()
            if row and (time.time() - row["fetched_at"]) < _CACHE_TTL:
                return json.loads(row["data"])
    except Exception:
        pass
    return None


def _cache_set(key: str, data: dict):
    try:
        _ensure_table()
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO ndvi_cache (key, data, fetched_at) VALUES (?,?,?)",
                (key, json.dumps(data), time.time()),
            )
    except Exception as e:
        log.debug("ndvi cache write failed: %s", e)


def _modis_date(dt: datetime) -> str:
    """Convert datetime to MODIS date string A{year}{doy}."""
    doy = dt.timetuple().tm_yday
    return f"A{dt.year}{doy:03d}"


def _interp_ndvi(ndvi: float) -> str:
    """Human-readable NDVI interpretation."""
    if ndvi < 0.1:
        return "bare soil / no vegetation"
    if ndvi < 0.3:
        return "sparse / stressed vegetation"
    if ndvi < 0.5:
        return "moderate crop cover"
    if ndvi < 0.7:
        return "healthy dense crop"
    return "very healthy / lush vegetation"


def _health_color(ndvi: float) -> str:
    if ndvi < 0.1: return "danger"
    if ndvi < 0.3: return "warn"
    if ndvi < 0.5: return "ok"
    return "good"


def get_ndvi(lat: float = _DEFAULT_LAT, lon: float = _DEFAULT_LON,
             weeks_back: int = 6) -> dict:
    """
    Fetch recent NDVI values for a location from NASA MODIS.

    Returns dict with:
      latest_ndvi, latest_date, interpretation, history (list of {date, ndvi}),
      lat, lon, source
    """
    cache_key = f"ndvi:{lat:.4f},{lon:.4f}:{weeks_back}"
    cached = _cache_get(cache_key)
    if cached:
        cached["from_cache"] = True
        return cached

    try:
        # Get last N weeks of dates
        end_dt   = datetime.now()
        start_dt = end_dt - timedelta(weeks=weeks_back * 2)  # overshoot — MODIS is 16-day
        start_md = _modis_date(start_dt)
        end_md   = _modis_date(end_dt)

        url = (f"{_BASE_URL}/{_PRODUCT}/subset"
               f"?latitude={lat}&longitude={lon}"
               f"&band={_BAND}"
               f"&startDate={start_md}&endDate={end_md}"
               f"&kmAboveBelow=0&kmLeftRight=0")

        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        scale = float(data.get("scale", _SCALE))
        history = []
        for entry in data.get("subset", []):
            raw = entry.get("data", [None])[0]
            if raw is None or raw <= _FILL_VAL:
                continue
            ndvi = round(raw * scale, 4)
            history.append({
                "date":  entry["calendar_date"],
                "ndvi":  ndvi,
                "label": _interp_ndvi(ndvi),
                "color": _health_color(ndvi),
            })

        if not history:
            return {"error": "No NDVI data available for this location / period"}

        latest = history[-1]
        result = {
            "latest_ndvi":     latest["ndvi"],
            "latest_date":     latest["date"],
            "interpretation":  latest["label"],
            "health_color":    latest["color"],
            "history":         history,
            "lat":             lat,
            "lon":             lon,
            "source":          "NASA MODIS MOD13Q1 (250m, 16-day composite)",
            "from_cache":      False,
            "fetched_at":      datetime.now().isoformat(),
        }
        _cache_set(cache_key, result)
        log.info("NDVI fetched: lat=%.4f lon=%.4f ndvi=%.3f (%s)",
                 lat, lon, latest["ndvi"], latest["label"])
        return result

    except httpx.TimeoutException:
        return {"error": "NASA MODIS API timeout — try again"}
    except Exception as e:
        log.error("NDVI fetch failed: %s", e)
        return {"error": str(e)}


def format_ndvi_report(data: dict, plot_name: str = "Barloni farm") -> str:
    """Format NDVI data for display in CLI or dashboard."""
    if "error" in data:
        return f"NDVI unavailable: {data['error']}"

    ndvi = data["latest_ndvi"]
    bar_len = max(1, int(ndvi * 20))
    bar = "█" * bar_len + "░" * (20 - bar_len)

    lines = [
        f"NDVI — {plot_name}  ({data['latest_date']})",
        f"  {bar}  {ndvi:.3f}",
        f"  {data['interpretation']}",
        "",
        "RECENT TREND",
    ]
    for h in data["history"][-6:]:
        mini = "█" * max(1, int(h["ndvi"] * 10))
        lines.append(f"  {h['date']}  {mini:<10}  {h['ndvi']:.3f}  {h['label']}")

    if not data.get("from_cache"):
        lines.append(f"\nSource: {data['source']}")

    return "\n".join(lines)
