import json
import logging
import time
from typing import Any

import httpx

from core.config import OLLAMA_URL, TEXT_MODEL

from core.base_module import BaseModule, ModuleResponse
from core.memory import recent_events
from core.sanitizer import validate_action

from modules.farming import db, tools
from modules.farming import weather as wx
from modules.farming.soil import get_soil, format_soil
from modules.farming.knowledge import kb_context_for_llm, list_crops_with_kb, format_disease_summary
from modules.farming.mandi import get_prices, format_prices
from modules.farming.farming_client import analyse_photo, format_diagnosis, is_running as farming_server_running
from modules.farming.geocode import resolve

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
  {"action": "log_spray", "plot": "<plot_name>", "chemical": "<string>", "quantity": "<string or null>", "reason": "<string or null>"}
  {"action": "spray_history", "plot": "<plot_name>"}
  {"action": "log_observation", "plot": "<plot_name>", "type": "disease|pest|weather_damage|growth|soil|other", "description": "<string>", "severity": "low|medium|high"}
  {"action": "open_observations"}
  {"action": "disease_info", "crop": "<crop name or null if not specified>", "condition": "<disease name or null>"}
  {"action": "crop_history", "plot": "<plot_name>"}
  {"action": "mandi_price", "commodity": "<crop name>", "district": "<district or null>"}
  {"action": "chat", "reply": "<response for conversational or ambiguous queries>"}

Examples (follow this format exactly):
  User: weather today at my farm
  → {"action": "weather_now", "location": null}

  User: will it rain tomorrow — is it safe to spray?
  → {"action": "spray_safe_tomorrow", "location": null}

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
    farm = context.get("default_farm", {})
    profile = context.get("profile", {})
    name = profile.get("name") or profile.get("alias") or "Ganesh"
    lines = [f"Farmer: {name}"]
    if farm:
        lines.append(
            f"Default farm: {farm.get('primary_location', '')}, {farm.get('district', '')}, "
            f"{farm.get('state', '')} — {farm.get('area_acres', '?')} acres, "
            f"{farm.get('soil_type', '')} soil, {farm.get('irrigation_type', '')} irrigation"
        )
        crops = [farm.get("primary_crop")] + (farm.get("other_crops") or [])
        lines.append(f"Crops: {', '.join(c for c in crops if c)}")
    farms = profile.get("farms", [])
    if len(farms) > 1:
        lines.append(f"Other farms: {', '.join(f['label'] for f in farms[1:])}")
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
                pairs.append({"role": "user", "content": q})
                pairs.append({"role": "assistant", "content": r})
        return pairs
    except Exception:
        return []


def _call_llm(query: str, context: dict) -> dict:
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
    "mugdha farm", "mugdha's farm", "mugdhas farm",
}


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


def _execute(action: dict, context: dict | None = None) -> tuple[str, dict | None]:
    a = action.get("action")
    farm = (context or {}).get("default_farm", {})
    _lat  = farm.get("lat") or farm.get("latitude") or None
    _lon  = farm.get("lon") or farm.get("longitude") or None
    _name = (farm.get("city") or farm.get("primary_location")
             or farm.get("location") or farm.get("label") or None)

    # ── Plot management ────────────────────────────────────────────────────────
    if a == "add_plot":
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

    if a == "list_plots":
        plots = tools.list_plots()
        if not plots:
            return "No plots yet. Try: 'add my north field, 3 acres, black soil, Solapur'", None
        lines = ["Your plots:"]
        for p in plots:
            area = f" — {p['area_acres']} acres" if p.get("area_acres") else ""
            soil = f", {p['soil_type']}" if p.get("soil_type") else ""
            lines.append(f"  {p['name']}{area}{soil}")
        return "\n".join(lines), plots

    if a == "plant_crop":
        r = tools.plant_crop(action["plot"], action["crop"], action.get("variety"),
                             action.get("planted_date"), action.get("expected_harvest"))
        if "error" in r:
            return r["error"], None
        harvest = f", harvest expected {action['expected_harvest']}" if action.get("expected_harvest") else ""
        return f"Planted {action['crop']} in {action['plot']} on {r['planted']}{harvest}", r

    if a == "list_crops":
        crops = tools.list_crops(action.get("plot"))
        if not crops:
            return "No active crops found.", None
        lines = ["Active crops:"]
        for c in crops:
            h = f" (harvest: {c['expected_harvest']})" if c.get("expected_harvest") else ""
            v = f" / {c['variety']}" if c.get("variety") else ""
            lines.append(f"  {c['plot_name']} → {c['crop_name']}{v}{h}")
        return "\n".join(lines), crops

    if a == "harvest_crop":
        r = tools.update_crop_status(action["crop_id"], "harvested", action.get("yield_kg"))
        yield_str = f" — {action['yield_kg']}kg yield" if action.get("yield_kg") else ""
        return f"Crop #{action['crop_id']} marked as harvested{yield_str}", r

    if a == "log_spray":
        r = tools.log_spray(action["plot"], action["chemical"], action.get("quantity"), action.get("reason"))
        if "error" in r:
            return r["error"], None
        qty = f" ({action['quantity']})" if action.get("quantity") else ""
        reason = f" — {action['reason']}" if action.get("reason") else ""
        return f"Spray logged: {action['chemical']}{qty} on {action['plot']}{reason}", r

    if a == "spray_history":
        logs = tools.spray_history(action.get("plot", ""))
        if not logs:
            return "No spray logs found.", None
        lines = [f"Spray history — {action.get('plot', '')}:"]
        for s in logs[:10]:
            qty = f" ({s['quantity']})" if s.get("quantity") else ""
            lines.append(f"  {s['date']}: {s['chemical']}{qty}")
        return "\n".join(lines), logs

    if a == "log_observation":
        r = tools.log_observation(action["plot"], action["type"], action["description"],
                                  action.get("severity"))
        if "error" in r:
            return r["error"], None
        sev = f" [{action['severity']}]" if action.get("severity") else ""
        return f"Observation logged on {action['plot']}: {action['type']}{sev}", r

    if a == "open_observations":
        obs = tools.open_observations(action.get("plot"))
        if not obs:
            return "No open observations.", None
        lines = ["Open observations:"]
        for o in obs:
            sev = f" [{o['severity']}]" if o.get("severity") else ""
            lines.append(f"  {o['date']} | {o['plot_name']} | {o['type']}{sev}: {o['description'][:80]}")
        return "\n".join(lines), obs

    # ── Mandi (APMC) prices ────────────────────────────────────────────────────
    if a == "mandi_price":
        crop = action.get("commodity") or action.get("crop")
        if not crop:
            known = list_crops_with_kb()
            return (
                f"Which crop price do you want? e.g. pomegranate, onion, sugarcane.\n"
                f"Crops I know: {', '.join(known)}"
            ), None
        district = action.get("district") or None
        data = get_prices(crop, district=district)
        return format_prices(data), data

    # ── Disease knowledge base ─────────────────────────────────────────────────
    if a == "disease_info":
        crop = action.get("crop") or None
        if not crop:
            known = list_crops_with_kb()
            crops_str = ", ".join(known) if known else "pomegranate"
            return (
                f"Which crop are you asking about? I have disease information for: {crops_str}.\n"
                "You can also share a photo for diagnosis."
            ), None
        condition = action.get("condition", "")
        if condition:
            text = format_disease_summary(crop, condition)
        else:
            kb = kb_context_for_llm(crop)
            text = kb if kb else f"No knowledge base found for {crop}."
        return text, None

    # ── Disease diagnosis from photo ───────────────────────────────────────────
    if a == "diagnose_photo":
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

    # ── Weather ────────────────────────────────────────────────────────────────
    if a == "weather_now":
        loc, lat, lon = _loc(action.get("location"), _lat, _lon, _name)
        data = wx.current_conditions(lat=lat, lon=lon, name=loc)
        return (
            f"Current weather at {data['location']}:\n"
            f"  {data['description']}, {data['temperature_c']}°C\n"
            f"  Humidity: {data['humidity_pct']}%  |  Wind: {data['wind_kmh']} km/h  |  Rain: {data['rain_mm']}mm"
        ), data

    if a == "weather_forecast":
        loc, lat, lon = _loc(action.get("location"), _lat, _lon, _name)
        days = action.get("days", 7)
        fc = wx.forecast(lat=lat, lon=lon, days=days, name=loc)
        lines = [f"Weather forecast — {fc['location']} ({days} days):"]
        lines.append(_fmt_forecast(fc["days"]))
        if fc.get("soil_moisture_now") is not None:
            lines.append(f"\nSoil moisture: {fc['soil_moisture_now']:.3f} m³/m³")
        return "\n".join(lines), fc

    if a == "spray_safe_tomorrow":
        loc, lat, lon = _loc(action.get("location"), _lat, _lon, _name)
        result = wx.spray_safe_tomorrow(lat=lat, lon=lon, name=loc)
        status = "SAFE to spray" if result["safe_to_spray"] else "NOT safe to spray"
        reasons = "\n  ".join(result["reasons"])
        lines = [f"Tomorrow ({result['date']}): {status}", f"  {reasons}"]
        lines.append(f"\n  Total rain expected: {result['rain_mm']}mm")

        rainy = result.get("rainy_hours", [])
        dry = result.get("dry_windows", [])
        hourly = result.get("hourly_rain", [])

        if rainy:
            lines.append(f"  Rain expected: {', '.join(rainy)}")
        if dry:
            morning = [h for h in dry if h < "12:00"]
            afternoon = [h for h in dry if "12:00" <= h < "17:00"]
            evening = [h for h in dry if h >= "17:00"]
            windows = []
            if morning:
                windows.append(f"morning ({morning[0]}–{morning[-1]})")
            if afternoon:
                windows.append(f"afternoon ({afternoon[0]}–{afternoon[-1]})")
            if evening:
                windows.append(f"evening ({evening[0]}–{evening[-1]})")
            lines.append(f"  Dry windows: {', '.join(windows) if windows else 'none'}")

        if hourly:
            peak = max(hourly, key=lambda h: h.get("rain_mm") or 0)
            if (peak.get("rain_mm") or 0) > 0:
                lines.append(f"  Peak rain: {peak['rain_mm']}mm at {peak['hour']}")

        return "\n".join(lines), result

    if a == "rainfall_history":
        loc, lat, lon = _loc(action.get("location"), _lat, _lon, _name)
        r = wx.historical_rainfall(lat=lat, lon=lon, name=loc,
                                   start=action.get("start"), end=action.get("end"))
        lines = [f"Rainfall — {r['location']} ({r['start']} to {r['end']}) — total: {r['total_mm']}mm"]
        for month, mm in r["monthly_mm"].items():
            lines.append(f"  {month}: {mm}mm")
        return "\n".join(lines), r

    if a == "crop_history":
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

    # ── Soil ───────────────────────────────────────────────────────────────────
    if a == "soil_data":
        plot_name = action.get("plot")
        if plot_name:
            # Named plot has explicit coords
            plot = tools.get_plot(plot_name)
            if plot and plot.get("lat"):
                data = get_soil(lat=plot["lat"], lon=plot["lon"],
                                location=plot.get("name", plot_name))
                return format_soil(data), data
        # Fall back to location resolution (handles personal refs + geocoding)
        loc, lat, lon = _loc(action.get("location"), _lat, _lon, _name)
        data = get_soil(lat=lat, lon=lon, location=loc)
        return format_soil(data), data

    # ── Summary ────────────────────────────────────────────────────────────────
    if a in ("season_summary", "summary"):
        s = tools.season_summary()
        return (
            f"Season summary:\n"
            f"  Plots: {s['plots']}\n"
            f"  Active crops: {s['active_crops']}\n"
            f"  Harvested: {s['harvested_crops']} ({s['total_yield_kg']}kg total yield)\n"
            f"  Open observations: {s['open_observations']}\n"
            f"  Sprays this month: {s['sprays_this_month']}"
        ), s

    if a == "chat":
        return action.get("reply", ""), None

    return f"Unknown action: {a}", None


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


class FarmingModule(BaseModule):
    name = "farming"
    description = (
        "Handles all farming: plots, crop tracking, spray schedules, disease and pest observations, "
        "disease diagnosis (with photo), soil data (pH, nitrogen, clay), weather forecasts, "
        "rainfall history, crop-season weather analysis, and farming advice for Maharashtra."
    )

    def __init__(self):
        db.init()

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        t0 = time.monotonic()
        try:
            action = _call_llm(query, context)
        except Exception as e:
            log.error("LLM call failed: %s", e, exc_info=True)
            return ModuleResponse(
                text="I couldn't process that — please try again.",
                module=self.name,
            )

        v = validate_action("farming", action)
        action_name = v.action.get("action", "unknown")

        if not v.valid:
            log.warning("action blocked action=%s errors=%s", action_name, v.errors)
            return ModuleResponse(
                text=f"Action blocked by validator: {'; '.join(v.errors)}",
                module=self.name,
            )
        for w in v.warnings:
            log.warning("validator: %s", w)

        log.info("action=%s latency=%dms", action_name, int((time.monotonic() - t0) * 1000))
        text, data = _execute(v.action, context)
        follow_up = _FOLLOW_UPS.get(action_name)
        return ModuleResponse(text=text, module=self.name, data=data, follow_up=follow_up)
