"""Location name → (lat, lon, canonical_name) via Open-Meteo geocoding API.
Includes a local fallback for Maharashtra farming locations not in the geocoder database."""

import httpx

_cache: dict[str, tuple[float, float, str]] = {}

# Local fallback for important farming talukas/villages the geocoder doesn't know
_LOCAL_MAP: dict[str, tuple[float, float, str]] = {
    "pandharpur":    (17.6794, 75.3296, "Pandharpur"),
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
    "latur":         (18.4088, 76.5604, "Latur"),
    "udgir":         (18.3939, 77.1183, "Udgir"),
    "ausa":          (18.2500, 76.5167, "Ausa"),
    "nilanga":       (17.7667, 76.7500, "Nilanga"),
    "wadi":          (17.0500, 76.9833, "Wadi"),
    "parbhani":      (19.2666, 76.7833, "Parbhani"),
    "jalna":         (19.8347, 75.8816, "Jalna"),
    "akola":         (20.7002, 77.0082, "Akola"),
    "amravati":      (20.9333, 77.7500, "Amravati"),
    "chandrapur":    (19.9615, 79.2961, "Chandrapur"),
    "gadchiroli":    (20.1833, 80.0000, "Gadchiroli"),
    "gondia":        (21.4620, 80.1964, "Gondia"),
    "kolhapur":      (16.7000, 74.2333, "Kolhapur"),
    "sangli":        (16.8667, 74.5667, "Sangli"),
    "satara":        (17.6856, 74.0111, "Satara"),
    "ratnagiri":     (16.9902, 73.3120, "Ratnagiri"),
    "sindhudurg":    (16.3500, 73.8833, "Sindhudurg"),
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
