"""
SoilGrids (ISRIC) soil properties — free, no API key, 250m resolution, global.
Returns: pH, nitrogen, organic carbon, clay %, sand % at 0–30cm depth.
"""

from typing import Any
import httpx

from modules.farming.geocode import resolve

_URL        = "https://rest.isric.org/soilgrids/v2.0/properties/query"
_PROPERTIES = ["phh2o", "nitrogen", "ocd", "clay", "sand"]
_DEPTH      = "0-30cm"


def get_soil(
    location: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> dict[str, Any]:
    """Fetch soil properties for a location. Pass lat/lon directly or a location name."""
    name = ""
    if lat is None or lon is None:
        if not location:
            return {"error": "Provide a location name or lat/lon coordinates"}
        result = resolve(location)
        if not result:
            return {"error": f"Could not resolve location: '{location}'"}
        lat, lon, name = result
    else:
        name = location or f"{lat:.2f},{lon:.2f}"

    try:
        resp = httpx.get(
            _URL,
            params={
                "lon": lon, "lat": lat,
                "property": _PROPERTIES,
                "depth": _DEPTH,
                "value": "mean",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": f"SoilGrids API error: {e}"}

    props = data.get("properties", {}).get("layers", [])
    detail: dict[str, Any] = {"location": name, "depth": _DEPTH}

    for layer in props:
        prop_name = layer.get("name", "")
        for d in layer.get("depths", []):
            if d.get("label") == _DEPTH:
                val = d.get("values", {}).get("mean")
                if val is None:
                    continue
                if prop_name == "phh2o":
                    detail["ph"] = round(val / 10, 1)
                elif prop_name == "nitrogen":
                    detail["nitrogen_g_kg"] = round(val / 100, 2)
                elif prop_name == "ocd":
                    detail["organic_carbon_g_kg"] = round(val / 10, 2)
                elif prop_name == "clay":
                    detail["clay_pct"] = round(val / 10, 1)
                elif prop_name == "sand":
                    detail["sand_pct"] = round(val / 10, 1)

    if len(detail) <= 2:
        return {"error": "No soil data returned for this location", "location": name}

    # derive soil type from clay/sand ratio
    clay = detail.get("clay_pct", 0)
    sand = detail.get("sand_pct", 0)
    if clay > 40:
        detail["soil_type_estimate"] = "clay — retains moisture, watch for fungal diseases"
    elif sand > 60:
        detail["soil_type_estimate"] = "sandy — drains fast, increase irrigation frequency"
    else:
        detail["soil_type_estimate"] = "loamy — balanced drainage"

    return detail


def format_soil(data: dict) -> str:
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
