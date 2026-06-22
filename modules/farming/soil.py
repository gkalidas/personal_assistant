"""
SoilGrids (ISRIC) soil properties — free, no API key, 250m resolution, global.
Returns: pH, nitrogen, organic carbon, clay %, sand % at 0–30cm depth.
"""

import logging
from typing import Any
import httpx

from modules.farming.geocode import resolve

log = logging.getLogger(__name__)

_URL        = "https://rest.isric.org/soilgrids/v2.0/properties/query"
_PROPERTIES = ["phh2o", "nitrogen", "ocd", "clay", "sand"]
# SoilGrids valid depth labels for 0–30cm surface layer
_DEPTHS     = ["0-5cm", "5-15cm", "15-30cm"]


def _avg(vals: list[float]) -> float:
    """Mean of a list of floats, or 0.0 when empty."""
    return sum(vals) / len(vals) if vals else 0.0


def _resolve_coords(location, lat, lon) -> tuple[float, float, str] | dict:
    """Return (lat, lon, name) for the request, or an {'error': ...} dict."""
    if lat is not None and lon is not None:
        return lat, lon, (location or f"{lat:.2f},{lon:.2f}")
    if not location:
        return {"error": "Provide a location name or lat/lon coordinates"}
    result = resolve(location)
    if not result:
        return {"error": f"Could not resolve location: '{location}'"}
    return result


def _fetch_soil(lat: float, lon: float) -> dict | None:
    """Query SoilGrids for the 0–30cm properties. Returns the JSON, or None on error."""
    # SoilGrids needs repeated params (property=x&property=y), so use list-of-tuples.
    params = [("lon", lon), ("lat", lat), ("value", "mean")]
    params += [("property", p) for p in _PROPERTIES]
    params += [("depth", d) for d in _DEPTHS]
    try:
        resp = httpx.get(_URL, params=params, timeout=20.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("SoilGrids API error: %s", e)
        return None


def _parse_soil(data: dict, name: str) -> dict[str, Any]:
    """Aggregate SoilGrids layers into a detail dict with a derived soil type."""
    accum: dict[str, list[float]] = {}
    for layer in data.get("properties", {}).get("layers", []):
        prop_name = layer.get("name", "")
        for d in layer.get("depths", []):
            if d.get("label") in _DEPTHS:
                val = d.get("values", {}).get("mean")
                if val is not None:
                    accum.setdefault(prop_name, []).append(val)

    detail: dict[str, Any] = {"location": name, "depth": "0-30cm"}
    for key, src, div in [("ph", "phh2o", 10), ("nitrogen_g_kg", "nitrogen", 100),
                          ("organic_carbon_g_kg", "ocd", 10),
                          ("clay_pct", "clay", 10), ("sand_pct", "sand", 10)]:
        if src in accum:
            detail[key] = round(_avg(accum[src]) / div, 2 if div == 100 else 1)

    if len(detail) <= 2:
        return {"error": "No soil data returned for this location", "location": name}

    clay, sand = detail.get("clay_pct", 0), detail.get("sand_pct", 0)
    if clay > 40:
        detail["soil_type_estimate"] = "clay — retains moisture, watch for fungal diseases"
    elif sand > 60:
        detail["soil_type_estimate"] = "sandy — drains fast, increase irrigation frequency"
    else:
        detail["soil_type_estimate"] = "loamy — balanced drainage"
    return detail


def get_soil(
    location: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> dict[str, Any]:
    """Fetch soil properties for a location. Pass lat/lon directly or a location name."""
    coords = _resolve_coords(location, lat, lon)
    if isinstance(coords, dict):
        return coords                       # error dict
    lat, lon, name = coords

    data = _fetch_soil(lat, lon)
    if data is None:
        return {"error": "SoilGrids API error", "location": name}
    return _parse_soil(data, name)


def format_soil(data: dict) -> str:
    """Format a soil-data dict into a human-readable multi-line block."""
    if "error" in data:
        return f"Soil data unavailable: {data['error']}"
    loc = data.get("location", "")
    lines = [f"Soil at {loc} (0–30 cm):"]
    if "ph" in data:
        lines.append(f"  pH: {data['ph']}")
    if "nitrogen_g_kg" in data:
        lines.append(f"  Nitrogen: {data['nitrogen_g_kg']} g/kg")
    if "organic_carbon_g_kg" in data:
        lines.append(f"  Organic carbon: {data['organic_carbon_g_kg']} g/kg")
    if "clay_pct" in data:
        lines.append(f"  Clay: {data['clay_pct']}%")
    if "sand_pct" in data:
        lines.append(f"  Sand: {data['sand_pct']}%")
    if "soil_type_estimate" in data:
        lines.append(f"  Type: {data['soil_type_estimate']}")
    return "\n".join(lines)
