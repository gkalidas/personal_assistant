"""Location name → (lat, lon, canonical_name) via Open-Meteo geocoding API.
Includes a local fallback for Maharashtra farming locations not in the geocoder database."""

import httpx

_cache: dict[str, tuple[float, float, str]] = {}

# Local fallback for important farming talukas/villages the geocoder doesn't know
_LOCAL_MAP: dict[str, tuple[float, float, str]] = {
    "madha":         (17.9019, 75.5119, "Madha"),
    "madha solapur": (17.9019, 75.5119, "Madha"),
    "barloni":       (18.1617, 75.4218, "Barloni"),
    "malshiras":     (17.8500, 74.9167, "Malshiras"),
    "mohol":         (17.9667, 75.7167, "Mohol"),
    "akkalkot":      (17.5667, 76.2000, "Akkalkot"),
    "mangalvedha":   (17.5217, 75.4583, "Mangalvedha"),
    "buldhana":      (20.5333, 76.1833, "Buldhana"),
    "chikhli":       (20.3564, 76.2667, "Chikhli"),
    "khamgaon":      (20.7103, 76.5594, "Khamgaon"),
    "shrigonda":     (18.6203, 74.7014, "Shrigonda"),
    "rahuri":        (19.3917, 74.6486, "Rahuri"),
    "kopargaon":     (19.8878, 74.4819, "Kopargaon"),
    "yeola":         (20.0439, 74.4867, "Yeola"),
    # Additional district HQs not in geocoder DB
    "jalgaon":       (21.0077, 75.5626, "Jalgaon"),
    "nanded":        (19.1383, 77.3210, "Nanded"),
    "osmanabad":     (18.1867, 76.0389, "Osmanabad"),
    "beed":          (18.9891, 75.7601, "Beed"),
    "hingoli":       (19.7167, 77.1500, "Hingoli"),
    "washim":        (20.1167, 77.1333, "Washim"),
    "yavatmal":      (20.3888, 78.1204, "Yavatmal"),
}


def _query(name: str) -> tuple[float, float, str] | None:
    """Single geocoding API call. Returns result or None."""
    try:
        r = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 1, "language": "en", "countryCode": "IN"},
            timeout=8.0,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        loc = results[0]
        return (loc["latitude"], loc["longitude"], loc.get("name", name))
    except Exception:
        return None


def resolve(location: str) -> tuple[float, float, str] | None:
    """Return (lat, lon, canonical_name) or None if not found.
    Order: local fallback map → geocoding API (full name) → city-only fallback.
    Narrows search to India (countryCode=IN) to avoid foreign city collisions."""
    if not location:
        return None
    key = location.lower().strip()
    if key in _cache:
        return _cache[key]

    # Check local map first (small talukas/villages the geocoder doesn't know)
    # Try exact key, then city-part only
    local = _LOCAL_MAP.get(key)
    if not local and "," in key:
        city_key = key.split(",")[0].strip()
        local = _LOCAL_MAP.get(city_key)
    if local:
        _cache[key] = local
        return local

    # Try full string first, then city-part only if comma-separated
    attempts = [location]
    if "," in location:
        city_part = location.split(",")[0].strip()
        if city_part and city_part.lower() != key:
            attempts.append(city_part)

    for name in attempts:
        result = _query(name)
        if result:
            _cache[key] = result
            return result

    _cache[key] = None  # type: ignore[assignment]  # negative-cache to skip future calls
    return None
