import json
import logging
import time
from typing import Any

import httpx

from core.config import OLLAMA_URL, TEXT_MODEL

from core.base_module import BaseModule, ModuleResponse
from core.memory import recent_events
from core.sanitizer import validate_action, sanitize_external_text

from modules.farming import db, tools
from modules.farming import weather as wx
from modules.farming.soil import get_soil, format_soil
from modules.farming.knowledge import kb_context_for_llm, list_crops_with_kb, format_disease_summary
from modules.farming.mandi import get_prices, format_prices
from modules.farming.farming_client import (
    analyse_photo, format_diagnosis, is_running as farming_server_running,
    get_history as farming_history, ask as farming_ask,
)
from modules.farming.geocode import resolve
from modules.farming.ndvi import get_ndvi, format_ndvi_report

log = logging.getLogger(__name__)


_SYSTEM = """You are the farming advisor inside GK — a private personal assistant for Ganesh, a farmer in Maharashtra, India.
You know his plots, crops, spray schedule, and local weather. Every answer should move him toward better yield and profit.

FIRST-PRINCIPLES RULE — before choosing any action, silently ask:
  1. KNOWN: What did the user explicitly state? (crop, plot, location, date, quantity)
  2. MISSING: What crucial fact is absent that you need to give a correct answer?
  3. DERIVE: What can you conclude from known facts + profile?
  4. If a key fact is MISSING (which crop? which plot? what problem?), use {"action": "chat", "reply": "...question..."} to ask.
     Never assume or invent the missing fact.

Respond ONLY with one JSON action object. No markdown, no explanation.

Actions:
  {"action": "weather_now", "location": "<village/city or null>"}
  {"action": "weather_forecast", "location": "<village/city or null>", "days": <1-7>}
  {"action": "spray_safe_tomorrow", "location": "<village/city or null>"}
  {"action": "rainfall_history", "location": "<village/city or null>", "start": "<YYYY-MM-DD or null>", "end": "<YYYY-MM-DD or null>"}
  {"action": "soil_data", "location": "<village/city or null>", "plot": "<plot_name or null>"}
  {"action": "season_summary"}
  {"action": "list_plots"}
  {"action": "add_plot", "name": "<string>", "area_acres": <number>, "soil_type": "<string>", "location": "<village/city>"}
  {"action": "plant_crop", "plot": "<plot_name>", "crop": "<string>", "variety": "<string or null>", "planted_date": "<YYYY-MM-DD or null>"}
  {"action": "list_crops", "plot": "<plot_name or null>"}
  {"action": "harvest_crop", "crop_id": <number>, "yield_kg": <number>}
  {"action": "log_spray", "plot": "<plot_name>", "chemical": "<string>", "quantity": "<string or null>", "reason": "<string or null>", "cost": <number or null — rupees spent on this spray, if mentioned>}
  {"action": "spray_history", "plot": "<plot_name>"}
  {"action": "log_observation", "plot": "<plot_name>", "type": "disease|pest|weather_damage|growth|soil|other", "description": "<string>", "severity": "low|medium|high"}
  {"action": "open_observations"}
  {"action": "disease_info", "crop": "<crop name or null if not specified>", "condition": "<disease name or null>"}
  {"action": "crop_history", "plot": "<plot_name>"}
  {"action": "mandi_price", "commodity": "<crop name>", "district": "<district or null>"}
  {"action": "ndvi_health", "location": "<village/city or null>", "plot": "<plot_name or null>"}
  {"action": "analysis_history", "limit": <number, default 10>}
  {"action": "chat", "reply": "<response for conversational or ambiguous queries>"}

Examples (follow this format exactly):
  User: weather today at my farm
  → {"action": "weather_now", "location": null}

  User: will it rain tomorrow — is it safe to spray?
  → {"action": "spray_safe_tomorrow", "location": null}

  User: can I spray tomorrow
  → {"action": "spray_safe_tomorrow", "location": null}

  User: spray safe check Solapur
  → {"action": "spray_safe_tomorrow", "location": "Solapur"}

  User: 7 day forecast for Solapur
  → {"action": "weather_forecast", "location": "Solapur", "days": 7}

  User: what disease affects pomegranate in monsoon
  → {"action": "disease_info", "crop": "pomegranate", "condition": null}

  User: what is today's pomegranate price at mandi
  → {"action": "mandi_price", "commodity": "pomegranate", "district": null}

  User: onion rates in Solapur
  → {"action": "mandi_price", "commodity": "onion", "district": "Solapur"}

  User: my plants have yellow spots, what disease is it?
  → {"action": "disease_info", "crop": null, "condition": "yellow spots"}

  User: log copper spray on gk_north plot, 250g per 15L
  → {"action": "log_spray", "plot": "gk_north", "chemical": "Copper Oxychloride", "quantity": "250g/15L", "reason": null}"""


def _profile_summary(context: dict) -> str:


    """Render a short farm/profile context block for the LLM system prompt."""
    farm = context.get("default_farm", {})
    profile = context.get("profile", {})
    name = profile.get("name") or profile.get("alias") or "Ganesh"
    lines = [f"Farmer: {name}"]
    if farm:
        owner = farm.get("owner") or name
        label = farm.get("label", "")
        owner_str = f" (owner: {owner})" if owner.lower() != name.lower() else ""
        lines.append(
            f"Active farm: {label}{owner_str} — {farm.get('primary_location', '')}, "
            f"{farm.get('district', '')}, {farm.get('state', '')} — "
            f"{farm.get('area_acres', '?')} acres, {farm.get('soil_type', '')} soil, "
            f"{farm.get('irrigation_type', '')} irrigation"
        )
        crops = [farm.get("primary_crop")] + (farm.get("other_crops") or [])
        lines.append(f"Crops: {', '.join(c for c in crops if c)}")
    farms = profile.get("farms", [])
    active_label = farm.get("label") if farm else None
    other = [f["label"] for f in farms if f["label"] != active_label]
    if other:
        lines.append(f"Other farms: {', '.join(other)}")
    return "\n".join(lines)


def _recent_history(limit: int = 4) -> list[dict]:
    """Fetch last N farming query/response pairs for conversation context."""
    try:
        evts = recent_events(module="farming", limit=limit * 2)
        pairs = []
        for e in reversed(evts):
            q = e.get("query", "").strip()
            r = e.get("response", "").strip()
            if q and r and len(pairs) < limit:
                pairs.append({"role": "user",      "content": q})
                pairs.append({"role": "assistant",  "content": sanitize_external_text(r, label="mem:farming")})
        return pairs
    except Exception:
        return []


def _call_llm(query: str, context: dict) -> dict:


    """Call the farming LLM with profile + recent history and return the parsed action dict."""
    from datetime import date as _date
    extras = "\n" + _profile_summary(context)
    extras += f"\nToday: {_date.today().isoformat()}"

    try:
        plot_list = tools.list_plots()
        if plot_list:
            extras += f"\nKnown plots: {', '.join(p['name'] for p in plot_list)}"
    except Exception:
        pass

    extras += f"\nCrops with disease KB: {', '.join(list_crops_with_kb())}"

    history = _recent_history(limit=4)
    payload = {
        "model": TEXT_MODEL,
        "messages": (
            [{"role": "system", "content": _SYSTEM + extras}]
            + history
            + [{"role": "user", "content": query}]
        ),
        "stream": False,
        "format": "json",
        "think": False,  # disable qwen3 extended thinking for faster JSON output
    }
    t0 = time.monotonic()
    resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=120.0)
    resp.raise_for_status()
    result = json.loads(resp.json()["message"]["content"])
    ms = int((time.monotonic() - t0) * 1000)
    log.debug("LLM %dms → action=%s", ms, result.get("action"))
    if ms > 30_000:
        log.warning("slow LLM response: %dms for query=%r", ms, query[:60])
    return result


def _fmt_forecast(days: list[dict]) -> str:


    """Format a forecast dict into a short human-readable line."""
    lines = []
    for d in days:
        rain = d.get("rain_mm") or 0
        prob = d.get("rain_probability_pct") or 0
        tmax = d.get("temp_max_c")
        tmin = d.get("temp_min_c")
        lines.append(f"  {d['date']}: rain {rain}mm ({prob}% chance), {tmin}–{tmax}°C")
    return "\n".join(lines)


_PERSONAL_REFS = {
    "farm", "my farm", "our farm", "the farm",
    "here", "home", "my home", "my place",
    "field", "my field", "plot", "my plot",
    "location", "my location", "current location",
    "barloni farm", "gk farm",
}

# Farm label / owner words that resolve to a specific farm in the profile
_FARM_OWNER_WORDS = {
    # populated at import time from profile — see _resolve_named_farm()
}


def _resolve_named_farm(query: str, profile: dict) -> dict | None:
    """Return a farm dict from profile if the query names that farm explicitly.

    Matches on farm label (e.g. 'mugdha_farm') or on the owner's first name
    when the owner differs from the default farmer name.
    """
    q = query.lower()
    farmer_name = (profile.get("name") or profile.get("alias") or "gk").lower()
    default_label = (profile.get("preferences", {}).get("default_farm") or "").lower()
    for farm in profile.get("farms", []):
        label = farm["label"].lower().replace("_", " ")
        owner = (farm.get("owner") or "").lower()
        is_default = farm["label"].lower() == default_label
        if label in q:
            return farm
        # Match owner name only for non-default farms whose owner differs from GK
        if not is_default and owner and owner != farmer_name and owner in q:
            return farm
    return None


def _loc(
    raw: str | None,
    _lat: float | None,
    _lon: float | None,
    _name: str | None = None,
) -> tuple[str | None, float | None, float | None]:
    """Resolve location to (display_name, lat, lon).
    Personal references and failed geocodes fall back to profile coords.
    Returns profile name instead of None so weather output shows 'Barloni' not coordinates."""
    if not raw or raw.lower().strip() in _PERSONAL_REFS:
        return _name, _lat, _lon
    result = resolve(raw)
    if result is None:
        # Geocoding failed — use profile coords silently
        return _name, _lat, _lon
    lat, lon, canon = result
    return canon, lat, lon


# ── Action handlers (one per farming action) ──────────────────────────────────

def _h_add_plot(action, _lat, _lon, _name):

    """Handler: create a plot from the action fields."""
    loc = action.get("location", "")
    lat = lon = None
    if loc:
        coords = resolve(loc)
        if coords:
            lat, lon, _ = coords
    r = tools.add_plot(action["name"], action.get("area_acres"), action.get("soil_type"), lat, lon)
    area = f" ({action['area_acres']} acres)" if action.get("area_acres") else ""
    loc_str = f" — {loc}" if loc else ""
    return f"Plot added: {action['name']}{area}{loc_str}", r

def _h_list_plots(action, _lat, _lon, _name):

    """Handler: list all plots."""
    plots = tools.list_plots()
    if not plots:
        return "No plots yet. Try: 'add my north field, 3 acres, black soil, Solapur'", None
    lines = ["Your plots:"]
    for p in plots:
        area = f" — {p['area_acres']} acres" if p.get("area_acres") else ""
        soil = f", {p['soil_type']}" if p.get("soil_type") else ""
        lines.append(f"  {p['name']}{area}{soil}")
    return "\n".join(lines), plots

def _h_plant_crop(action, _lat, _lon, _name):

    """Handler: plant a crop on the named plot."""
    r = tools.plant_crop(action["plot"], action["crop"], action.get("variety"),
                         action.get("planted_date"), action.get("expected_harvest"))
    if "error" in r:
        return r["error"], None
    harvest = f", harvest expected {action['expected_harvest']}" if action.get("expected_harvest") else ""
    return f"Planted {action['crop']} in {action['plot']} on {r['planted']}{harvest}", r

def _h_list_crops(action, _lat, _lon, _name):

    """Handler: list crops, optionally for one plot."""
    crops = tools.list_crops(action.get("plot"))
    if not crops:
        return "No active crops found.", None
    lines = ["Active crops:"]
    for c in crops:
        h = f" (harvest: {c['expected_harvest']})" if c.get("expected_harvest") else ""
        v = f" / {c['variety']}" if c.get("variety") else ""
        lines.append(f"  {c['plot_name']} → {c['crop_name']}{v}{h}")
    return "\n".join(lines), crops

def _h_harvest_crop(action, _lat, _lon, _name):

    """Handler: mark a crop harvested."""
    r = tools.update_crop_status(action["crop_id"], "harvested", action.get("yield_kg"))
    yield_str = f" — {action['yield_kg']}kg yield" if action.get("yield_kg") else ""
    return f"Crop #{action['crop_id']} marked as harvested{yield_str}", r

def _h_log_spray(action, _lat, _lon, _name):

    """Handler: log a spray (auto-logs cost to finance when given)."""
    r = tools.log_spray(action["plot"], action["chemical"], action.get("quantity"), action.get("reason"))
    if "error" in r:
        return r["error"], None
    qty = f" ({action['quantity']})" if action.get("quantity") else ""
    reason = f" — {action['reason']}" if action.get("reason") else ""
    note = f"Spray logged: {action['chemical']}{qty} on {action['plot']}{reason}"
    # Auto-log finance expense if user mentioned a cost
    cost = action.get("cost")
    if cost:
        try:
            from modules.finance.tools import add_transaction
            desc = f"{action['chemical']}{qty} — {action['plot']}"
            add_transaction(float(cost), "expense", "farming", desc)
            note += f"\n  ₹{cost:.0f} logged as farming expense"
        except Exception as e:
            log.warning("farming→finance auto-log failed: %s", e)
    return note, r

def _h_spray_history(action, _lat, _lon, _name):

    """Handler: show recent spray history for a plot."""
    logs = tools.spray_history(action.get("plot", ""))
    if not logs:
        return "No spray logs found.", None
    lines = [f"Spray history — {action.get('plot', '')}:"]
    for s in logs[:10]:
        qty = f" ({s['quantity']})" if s.get("quantity") else ""
        lines.append(f"  {s['date']}: {s['chemical']}{qty}")
    return "\n".join(lines), logs

def _h_log_observation(action, _lat, _lon, _name):

    """Handler: record a field observation on a plot."""
    r = tools.log_observation(action["plot"], action["type"], action["description"],
                              action.get("severity"))
    if "error" in r:
        return r["error"], None
    sev = f" [{action['severity']}]" if action.get("severity") else ""
    return f"Observation logged on {action['plot']}: {action['type']}{sev}", r

def _h_open_observations(action, _lat, _lon, _name):

    """Handler: list unresolved observations."""
    obs = tools.open_observations(action.get("plot"))
    if not obs:
        return "No open observations.", None
    lines = ["Open observations:"]
    for o in obs:
        sev  = f" [{o['severity']}]" if o.get("severity") else ""
        desc = sanitize_external_text(o["description"][:80], label="obs:display")
        lines.append(f"  {o['date']} | {o['plot_name']} | {o['type']}{sev}: {desc}")
    return "\n".join(lines), obs

def _h_mandi_price(action, _lat, _lon, _name):

    """Handler: fetch live APMC/mandi prices for a commodity."""
    crop = action.get("commodity") or action.get("crop")
    if not crop:
        known = list_crops_with_kb()
        return (
            f"Which crop price do you want? e.g. pomegranate, onion, sugarcane.\n"
            f"Crops I know: {', '.join(known)}"
        ), None
    data = get_prices(crop, district=action.get("district") or None)
    return format_prices(data), data

def _h_ndvi_health(action, _lat, _lon, _name):

    """Handler: fetch NDVI crop-health for a location/plot."""
    loc_raw = action.get("location") or "barloni"
    name, lat, lon = _loc(loc_raw, _lat, _lon, _name)
    if lat is None or lon is None:
        from modules.farming.geocode import _LOCAL_MAP
        lat, lon, name = _LOCAL_MAP.get("barloni", (18.1617, 75.4218, "Barloni"))
    data = get_ndvi(lat=lat, lon=lon)
    return format_ndvi_report(data, plot_name=name or "Barloni farm"), data

def _h_disease_info(action, _lat, _lon, _name):

    """Handler: return knowledge-base disease info for a crop."""
    crop = action.get("crop") or None
    if not crop:
        known = list_crops_with_kb()
        return (
            f"Which crop are you asking about? I have disease information for: {', '.join(known) or 'pomegranate'}.\n"
            "You can also share a photo for diagnosis."
        ), None
    condition = action.get("condition", "")
    if condition:
        return format_disease_summary(crop, condition), None
    kb = kb_context_for_llm(crop)
    return (kb if kb else f"No knowledge base found for {crop}."), None

def _h_diagnose_photo(action, _lat, _lon, _name):

    """Handler: diagnose a crop disease from a photo."""
    if not farming_server_running():
        kb = kb_context_for_llm(action.get("crop", "pomegranate"))
        return (
            "Farming server not running — can't analyse photos right now.\n"
            f"Start it: cd /home/ganesh/projects/farming && python main.py\n\n"
            f"Meanwhile, here's the disease KB:\n{kb[:500]}..."
        ), None
    plot = tools.get_plot(action.get("plot", "")) if action.get("plot") else None
    result = analyse_photo(
        action["image_path"], action.get("crop", "pomegranate"),
        location="", plot_id=plot["id"] if plot else None,
    )
    return format_diagnosis(result), result

def _h_weather_now(action, _lat, _lon, _name):

    """Handler: current weather for the farm."""
    loc, lat, lon = _loc(action.get("location"), _lat, _lon, _name)
    data = wx.current_conditions(lat=lat, lon=lon, name=loc)
    return (
        f"Current weather at {data['location']}:\n"
        f"  {data['description']}, {data['temperature_c']}°C\n"
        f"  Humidity: {data['humidity_pct']}%  |  Wind: {data['wind_kmh']} km/h  |  Rain: {data['rain_mm']}mm"
    ), data

def _h_weather_forecast(action, _lat, _lon, _name):

    """Handler: multi-day weather forecast for the farm."""
    loc, lat, lon = _loc(action.get("location"), _lat, _lon, _name)
    days = action.get("days", 7)
    fc = wx.forecast(lat=lat, lon=lon, days=days, name=loc)
    lines = [f"Weather forecast — {fc['location']} ({days} days):", _fmt_forecast(fc["days"])]
    if fc.get("soil_moisture_now") is not None:
        lines.append(f"\nSoil moisture: {fc['soil_moisture_now']:.3f} m³/m³")
    return "\n".join(lines), fc

def _h_spray_safe_tomorrow(action, _lat, _lon, _name):

    """Handler: assess whether tomorrow is safe to spray."""
    loc, lat, lon = _loc(action.get("location"), _lat, _lon, _name)
    result = wx.spray_safe_tomorrow(lat=lat, lon=lon, name=loc)
    status = "SAFE to spray" if result["safe_to_spray"] else "NOT safe to spray"
    lines = [f"Tomorrow ({result['date']}): {status}", f"  {chr(10).join(result['reasons'])}",
             f"\n  Total rain expected: {result['rain_mm']}mm"]
    rainy = result.get("rainy_hours", [])
    dry   = result.get("dry_windows", [])
    if rainy:
        lines.append(f"  Rain expected: {', '.join(rainy)}")
    if dry:
        morning   = [h for h in dry if h < "12:00"]
        afternoon = [h for h in dry if "12:00" <= h < "17:00"]
        evening   = [h for h in dry if h >= "17:00"]
        windows   = []
        if morning:   windows.append(f"morning ({morning[0]}–{morning[-1]})")
        if afternoon: windows.append(f"afternoon ({afternoon[0]}–{afternoon[-1]})")
        if evening:   windows.append(f"evening ({evening[0]}–{evening[-1]})")
        lines.append(f"  Dry windows: {', '.join(windows) if windows else 'none'}")
    hourly = result.get("hourly_rain", [])
    if hourly:
        peak = max(hourly, key=lambda h: h.get("rain_mm") or 0)
        if (peak.get("rain_mm") or 0) > 0:
            lines.append(f"  Peak rain: {peak['rain_mm']}mm at {peak['hour']}")
    return "\n".join(lines), result

def _h_rainfall_history(action, _lat, _lon, _name):

    """Handler: historical monthly rainfall for the farm."""
    loc, lat, lon = _loc(action.get("location"), _lat, _lon, _name)
    r = wx.historical_rainfall(lat=lat, lon=lon, name=loc,
                               start=action.get("start"), end=action.get("end"))
    lines = [f"Rainfall — {r['location']} ({r['start']} to {r['end']}) — total: {r['total_mm']}mm"]
    for month, mm in r["monthly_mm"].items():
        lines.append(f"  {month}: {mm}mm")
    return "\n".join(lines), r

def _h_crop_history(action, _lat, _lon, _name):

    """Handler: weather/risk history since a crop was planted."""
    plot = tools.get_plot(action.get("plot", ""))
    if not plot:
        return f"Plot '{action.get('plot')}' not found.", None
    if not plot.get("lat") or not plot.get("lon"):
        return f"Plot '{plot['name']}' has no coordinates. Add it with a location name.", None
    crops = tools.list_crops(plot["name"])
    if not crops:
        return f"No active crops on {plot['name']} to get history for.", None
    planted = crops[0].get("planted_date")
    if not planted:
        return f"No planting date recorded for crops on {plot['name']}.", None
    h = wx.crop_history(plot["lat"], plot["lon"], planted, plot["name"])
    return (
        f"Weather history for {plot['name']} since planting ({planted}, {h['days_since_plant']} days ago):\n"
        f"  Total rain: {h['total_rain_mm']}mm\n"
        f"  Avg max temp: {h['avg_max_temp_c']}°C\n"
        f"  High-humidity days (>80%): {h['humid_days_over80']}\n"
        f"  Heavy rain days (>20mm): {h['heavy_rain_days_over20mm']}\n"
        f"  Last 7 days: {h['last_7d_rain_mm']}mm rain, {h['last_7d_humid_days']} humid days"
    ), h

def _h_soil_data(action, _lat, _lon, _name):

    """Handler: soil properties for the farm location."""
    plot_name = action.get("plot")
    if plot_name:
        plot = tools.get_plot(plot_name)
        if plot and plot.get("lat"):
            data = get_soil(lat=plot["lat"], lon=plot["lon"], location=plot.get("name", plot_name))
            return format_soil(data), data
    loc, lat, lon = _loc(action.get("location"), _lat, _lon, _name)
    data = get_soil(lat=lat, lon=lon, location=loc)
    return format_soil(data), data

def _h_season_summary(action, _lat, _lon, _name):

    """Handler: season-wide farm summary."""
    s = tools.season_summary()
    return (
        f"Season summary:\n"
        f"  Plots: {s['plots']}\n"
        f"  Active crops: {s['active_crops']}\n"
        f"  Harvested: {s['harvested_crops']} ({s['total_yield_kg']}kg total yield)\n"
        f"  Open observations: {s['open_observations']}\n"
        f"  Sprays this month: {s['sprays_this_month']}"
    ), s

def _h_analysis_history(action, _lat, _lon, _name):

    """Handler: list recent disease analyses from the farming server."""
    limit = int(action.get("limit", 10))
    rows = farming_history(limit=limit)
    if not rows:
        return "No analysis history found (farming server may be offline or no analyses run yet).", None
    lines = [f"Last {min(limit, len(rows))} disease analyses:"]
    for r in rows:
        crop = r.get("crop", "?")
        loc  = r.get("location", "")
        diag = r.get("visual_diag") or r.get("condition") or "—"
        date = (r.get("created_at") or "")[:10]
        loc_str = f" | {loc}" if loc else ""
        lines.append(f"  {date}  {crop}{loc_str} → {diag[:60]}")
    return "\n".join(lines), rows

def _h_chat(action, _lat, _lon, _name):

    """Handler: return the LLM chat reply verbatim."""
    return action.get("reply", ""), None


# Dispatch table — add new farming actions here (one line each)
_DISPATCH: dict[str, Any] = {
    "add_plot":           _h_add_plot,
    "list_plots":         _h_list_plots,
    "plant_crop":         _h_plant_crop,
    "list_crops":         _h_list_crops,
    "harvest_crop":       _h_harvest_crop,
    "log_spray":          _h_log_spray,
    "spray_history":      _h_spray_history,
    "log_observation":    _h_log_observation,
    "open_observations":  _h_open_observations,
    "mandi_price":        _h_mandi_price,
    "ndvi_health":        _h_ndvi_health,
    "disease_info":       _h_disease_info,
    "diagnose_photo":     _h_diagnose_photo,
    "weather_now":        _h_weather_now,
    "weather_forecast":   _h_weather_forecast,
    "spray_safe_tomorrow":_h_spray_safe_tomorrow,
    "rainfall_history":   _h_rainfall_history,
    "crop_history":       _h_crop_history,
    "soil_data":          _h_soil_data,
    "season_summary":     _h_season_summary,
    "summary":            _h_season_summary,
    "analysis_history":   _h_analysis_history,
    "chat":               _h_chat,
}


def _execute(action: dict, context: dict | None = None) -> tuple[str, dict | None]:


    """Dispatch a validated action to its handler and return (text, data)."""
    a    = action.get("action")
    farm = (context or {}).get("default_farm", {})
    _lat  = farm.get("lat") or farm.get("latitude") or None
    _lon  = farm.get("lon") or farm.get("longitude") or None
    _name = (farm.get("city") or farm.get("primary_location")
             or farm.get("location") or farm.get("label") or None)
    fn = _DISPATCH.get(a)
    if fn:
        return fn(action, _lat, _lon, _name)
    return f"Unknown farming action: {a}", None


_FOLLOW_UPS = {
    "mandi_price": "Want to compare prices across more markets or check a different district?",
    "weather_forecast": "Want me to check if it's safe to spray tomorrow?",
    "weather_now": "Want the 7-day forecast or spray safety check?",
    "log_observation": "Want to log what treatment you applied?",
    "spray_history": "Noticed any missed sprays in the schedule?",
    "season_summary": "Want to see crop history or rainfall since planting?",
    "summary": "Want to see crop history or rainfall since planting?",
    "plant_crop": "Want to pull weather history since planting date?",
    "soil_data": "Want disease risk advice based on this soil type?",
    "disease_info": "Want to log a treatment or observation?",
}


# Queries that must be handled by PA's local dispatch (writes, photo diagnosis,
# mandi prices, DB lists) rather than delegated to the farming server.
_LOCAL_ONLY_KEYWORDS = {
    "photo", "image", "picture", "diagnos",       # photo analysis
    "add plot", "new plot", "register plot",      # plot creation
    "plant crop", "planted", "sow",               # crop planting
    "log spray", "spray log",                     # spray logging
    "log observation", "observation",             # field notes
    "mandi", "apmc", "market price", "rate",      # mandi — farming app has no price data
    "my plots", "list plots", "show plots",       # list from PA DB
    "my crops", "list crops", "show crops",       # list from PA DB
    "analysis history", "past analyses",          # history from farming server via client
}


def _apply_named_farm(query: str, context: dict) -> dict:
    """Switch default_farm when the query explicitly names another farm/owner."""
    named = _resolve_named_farm(query, context.get("profile", {}))
    if named and named.get("label") != context.get("default_farm", {}).get("label"):
        context = dict(context)
        context["default_farm"] = named
        log.info("farm context → %s (query named it)", named["label"])
    return context


def _is_local_only(query: str) -> bool:
    """True if the query must be served locally (writes / photo / mandi / lists)."""
    q = query.lower()
    return any(kw in q for kw in _LOCAL_ONLY_KEYWORDS)


class FarmingModule(BaseModule):
    name = "farming"
    description = (
        "Handles all farming: plots, crop tracking, spray schedules, disease and pest observations, "
        "disease diagnosis (with photo), soil data (pH, nitrogen, clay), weather forecasts, "
        "rainfall history, crop-season weather analysis, and farming advice for Maharashtra."
    )

    def __init__(self):
        """Initialize the farming module (ensures the DB schema exists)."""
        db.init()

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        """Route a farming query: named-farm switch → server delegation → local dispatch."""
        t0 = time.monotonic()
        context = _apply_named_farm(query, context)

        if not _is_local_only(query) and farming_server_running():
            resp = self._try_farming_server(query, context, t0)
            if resp is not None:
                return resp

        return self._local_dispatch(query, context, t0)

    def _try_farming_server(self, query: str, context: dict, t0: float) -> ModuleResponse | None:
        """Delegate to the farming server. Returns its response, or None to fall back."""
        farm  = context.get("default_farm", {})
        _name = (farm.get("city") or farm.get("primary_location")
                 or farm.get("location") or farm.get("label") or "")
        _crop = farm.get("primary_crop") or ""

        log.info("delegating to farming server: q=%r", query[:80])
        result = farming_ask(query=query, crop=_crop, location=_name)
        if "reply" in result:
            log.info("farming server replied in %dms", int((time.monotonic() - t0) * 1000))
            return ModuleResponse(
                text=result["reply"], module=self.name,
                data={"source": "farming_server", "modules": result.get("modules", [])},
            )
        log.warning("farming server ask failed: %s — falling back", result.get("error"))
        return None

    def _local_dispatch(self, query: str, context: dict, t0: float) -> ModuleResponse:
        """Run PA's own LLM → validate_action → dispatch path."""
        try:
            action = _call_llm(query, context)
        except Exception as e:
            log.error("LLM call failed: %s", e, exc_info=True)
            return ModuleResponse(text="I couldn't process that — please try again.", module=self.name)

        v = validate_action("farming", action)
        action_name = v.action.get("action", "unknown")
        if not v.valid:
            log.warning("action blocked action=%s errors=%s", action_name, v.errors)
            return ModuleResponse(
                text=f"Action blocked by validator: {'; '.join(v.errors)}", module=self.name)
        for w in v.warnings:
            log.warning("validator: %s", w)

        log.info("action=%s latency=%dms", action_name, int((time.monotonic() - t0) * 1000))
        text, data = _execute(v.action, context)
        return ModuleResponse(text=text, module=self.name, data=data,
                              follow_up=_FOLLOW_UPS.get(action_name))
