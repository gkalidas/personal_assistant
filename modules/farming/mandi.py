"""
Mandi (APMC) live prices via data.gov.in commodity market API.
Free, updated daily, covers all Maharashtra APMCs.

API key: set MANDI_API_KEY in environment, or falls back to the public demo key
(demo key rate-limited but works for personal use).
"""

import os
import time
from datetime import datetime
from typing import Any

import httpx

# Cache results for 30 minutes — prices update once daily, no need to hammer the API
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 1800

_BASE = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"
_DEMO_KEY = "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"

# Common crop name aliases → canonical API names
_CROP_ALIASES: dict[str, str] = {
    "pomegranate":  "Pomegranate",
    "anar":         "Pomegranate",
    "dalimb":       "Pomegranate",
    "onion":        "Onion",
    "kanda":        "Onion",
    "pyaz":         "Onion",
    "sugarcane":    "Sugarcane",
    "ganna":        "Sugarcane",
    "banana":       "Banana",
    "kela":         "Banana",
    "wheat":        "Wheat",
    "gehu":         "Wheat",
    "soybean":      "Soybean",
    "soya":         "Soybean",
    "cotton":       "Cotton",
    "kapas":        "Cotton",
    "tur":          "Tur Dal (Arhar)",
    "arhar":        "Tur Dal (Arhar)",
    "jowar":        "Jowar(Sorghum)",
    "bajra":        "Bajra(Pearl Millet/Cumbu)",
    "maize":        "Maize",
    "makka":        "Maize",
    "tomato":       "Tomato",
    "tamatar":      "Tomato",
    "grape":        "Grapes",
    "grapes":       "Grapes",
    "orange":       "Orange",
}


def _api_key() -> str:
    return os.getenv("MANDI_API_KEY", _DEMO_KEY)


def get_prices(
    commodity: str,
    state: str = "Maharashtra",
    district: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Fetch live APMC prices for a commodity. Results cached 30 minutes."""
    canonical = _CROP_ALIASES.get(commodity.lower().strip(), commodity.title())
    cache_key = f"{canonical}|{state}|{district or ''}"

    cached_at, cached_data = _cache.get(cache_key, (0.0, {}))
    if cached_data and (time.monotonic() - cached_at) < _CACHE_TTL:
        return cached_data

    params: list[tuple[str, Any]] = [
        ("api-key", _api_key()),
        ("format", "json"),
        ("filters[state]", state),
        ("filters[commodity]", canonical),
        ("limit", limit),
    ]
    if district:
        params.append(("filters[district]", district))

    try:
        resp = httpx.get(_BASE, params=params, timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": f"Mandi API error: {e}", "commodity": canonical}

    records = data.get("records", [])
    if not records:
        return {
            "error": f"No mandi data for {canonical} in {state}" +
                     (f" / {district}" if district else ""),
            "commodity": canonical,
        }

    # Sort by arrival_date desc, then modal_price desc
    def _sort_key(r: dict) -> tuple:
        try:
            dt = datetime.strptime(r.get("arrival_date", "01/01/2000"), "%d/%m/%Y")
        except Exception:
            dt = datetime.min
        return (dt, r.get("modal_price", 0))

    records.sort(key=_sort_key, reverse=True)

    result = {
        "commodity": canonical,
        "state": state,
        "date": records[0].get("arrival_date", ""),
        "records": records,
        "total_available": data.get("total", len(records)),
    }
    _cache[cache_key] = (time.monotonic(), result)
    return result


def format_prices(data: dict) -> str:
    if "error" in data:
        return f"Mandi prices unavailable: {data['error']}"

    commodity = data["commodity"]
    date = data.get("date", "today")
    records = data.get("records", [])

    lines = [f"Mandi prices — {commodity} ({date}):"]
    lines.append(f"  {'Market':<40}  {'Min':>7}  {'Max':>7}  {'Modal':>7}")
    lines.append("  " + "-" * 68)

    for r in records[:8]:
        market = f"{r.get('market', '?')} ({r.get('district', '')})"
        if len(market) > 40:
            market = market[:38] + ".."
        mn = r.get("min_price", 0)
        mx = r.get("max_price", 0)
        mod = r.get("modal_price", 0)
        lines.append(f"  {market:<40}  ₹{mn:>6,}  ₹{mx:>6,}  ₹{mod:>6,}")

    total = data.get("total_available", len(records))
    if total > len(records):
        lines.append(f"\n  (showing {len(records)} of {total} markets)")

    # Add a summary stat
    modals = [r["modal_price"] for r in records if r.get("modal_price")]
    if modals:
        avg = int(sum(modals) / len(modals))
        hi  = max(modals)
        lo  = min(modals)
        lines.append(f"\n  Price range: ₹{lo:,} – ₹{hi:,}  |  Avg modal: ₹{avg:,}/quintal")

    return "\n".join(lines)
