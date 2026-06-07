"""
SoilGrids (ISRIC) soil properties — free, no API key, 250m resolution, global.
Returns: pH, nitrogen, organic carbon, clay %, sand % at 0–30cm depth.
"""

from typing import Any
import httpx

from modules.farming.geocode import resolve

_URL        = "https://rest.isric.org/soilgrids/v2.0/properties/query"
_PROPERTIES = ["phh2o", "nitrogen", "ocd", "clay", "sand"]
# SoilGrids valid depth labels for 0–30cm surface layer
_DEPTHS     = ["0-5cm", "5-15cm", "15-30cm"]


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
        # SoilGrids requires repeated params for multiple properties/depths;
        # use list-of-tuples so httpx sends property=x&property=y (not property[]=x).
        params = [("lon", lon), ("lat", lat), ("value", "mean")]
        for p in _PROPERTIES:
            params.append(("property", p))
        for d in _DEPTHS:
            params.append(("depth", d))

        resp = httpx.get(_URL, params=params, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": f"SoilGrids API error: {e}"}

    props = data.get("properties", {}).get("layers", [])
    # Aggregate mean values across the three 0–30cm sub-layers
    accum: dict[str, list[float]] = {}
    for layer in props:
        prop_name = layer.get("name", "")
        for d in layer.get("depths", []):
            if d.get("label") in _DEPTHS:
                val = d.get("values", {}).get("mean")
                if val is not None:
                    accum.setdefault(prop_name, []).append(val)

    detail: dict[str, Any] = {"location": name, "depth": "0-30cm"}

    def _avg(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    if "phh2o" in accum:
        detail["ph"] = round(_avg(accum["phh2o"]) / 10, 1)
    if "nitrogen" in accum:
        detail["nitrogen_g_kg"] = round(_avg(accum["nitrogen"]) / 100, 2)
    if "ocd" in accum:
        detail["organic_carbon_g_kg"] = round(_avg(accum["ocd"]) / 10, 2)
    if "clay" in accum:
        detail["clay_pct"] = round(_avg(accum["clay"]) / 10, 1)
    if "sand" in accum:
        detail["sand_pct"] = round(_avg(accum["sand"]) / 10, 1)

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
