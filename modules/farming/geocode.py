"""Location name → (lat, lon, canonical_name) via Open-Meteo geocoding API."""

import httpx

_cache: dict[str, tuple[float, float, str]] = {}


def resolve(location: str) -> tuple[float, float, str] | None:
    """Return (lat, lon, canonical_name) or None if not found."""
    if not location:
        return None
    key = location.lower().strip()
    if key in _cache:
        return _cache[key]
    try:
        r = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en"},
            timeout=8.0,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        loc = results[0]
        result = (loc["latitude"], loc["longitude"], loc.get("name", location))
        _cache[key] = result
        return result
    except Exception:
        return None
