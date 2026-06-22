#!/usr/bin/env python3
"""
Weather dashboard generator.
Fetches ECMWF + GFS forecasts, cross-checks them, saves a chart PNG.
Run by update_dashboard.sh every 6 hours via cron.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.gridspec as gridspec
import numpy as np

REPO_ROOT   = Path(__file__).parent.parent
PROFILE     = REPO_ROOT / "user_profile.json"
OUTPUT      = REPO_ROOT / "dashboard" / "weather.png"

DEFAULT_LAT  = 18.52
DEFAULT_LON  = 73.85
DEFAULT_NAME = "Pune, Maharashtra"

SPRAY_UNSAFE_RAIN_MM   = 2.0
SPRAY_UNSAFE_PROB_PCT  = 30
SPRAY_UNSAFE_WIND_KMPH = 20


# ── Location ──────────────────────────────────────────────────────────────────

def _location() -> tuple[float, float, str]:
    """Resolve the chart location (CLI arg or profile default) to (lat, lon, name)."""
    if PROFILE.exists():
        p = json.loads(PROFILE.read_text())
        default_label = p.get("preferences", {}).get("default_farm", "farm_1")
        farms = p.get("farms", [])
        # pick default_farm label, fall back to first farm
        farm = next((f for f in farms if f.get("label") == default_label), None)
        if not farm and farms:
            farm = farms[0]
        if farm:
            lat, lon = farm.get("lat"), farm.get("lon")
            name = farm.get("city") or farm.get("primary_location") or DEFAULT_NAME
            if lat and lon:
                return lat, lon, name
    return DEFAULT_LAT, DEFAULT_LON, DEFAULT_NAME


# ── API ───────────────────────────────────────────────────────────────────────

def _fetch(lat: float, lon: float, model: str) -> dict:
    """Fetch a 7-day Open-Meteo forecast for a model (ecmwf/gfs). Returns the JSON."""
    if model == "ecmwf":
        url = "https://api.open-meteo.com/v1/forecast"
        extra = {}
    else:
        url = "https://api.open-meteo.com/v1/forecast"
        extra = {"models": "gfs_seamless"}

    resp = httpx.get(url, params={
        "latitude": lat, "longitude": lon,
        "daily": ",".join([
            "precipitation_sum",
            "precipitation_probability_max",
            "temperature_2m_max",
            "temperature_2m_min",
            "windspeed_10m_max",
        ]),
        "current": "temperature_2m,relative_humidity_2m,precipitation,windspeed_10m",
        "timezone": "Asia/Kolkata",
        "forecast_days": 7,
        **extra,
    }, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _parse(data: dict) -> dict:
    """Extract dates/rain/probability/temp/wind arrays from a forecast response."""
    d = data.get("daily", {})
    n = len(d.get("time", []))
    return {
        "dates":    d.get("time", []),
        "rain_mm":  d.get("precipitation_sum",           [0.0] * n),
        "rain_prob":d.get("precipitation_probability_max",[0]   * n),
        "temp_max": d.get("temperature_2m_max",          [None] * n),
        "temp_min": d.get("temperature_2m_min",          [None] * n),
        "wind_max": d.get("windspeed_10m_max",           [None] * n),
    }


def _current(data: dict) -> dict:
    """Extract the current conditions (temp/humidity/wind) from a forecast response."""
    c = data.get("current", {})
    return {
        "temp_c":   c.get("temperature_2m"),
        "humidity": c.get("relative_humidity_2m"),
        "rain_mm":  c.get("precipitation"),
        "wind_kmh": c.get("windspeed_10m"),
    }


# ── Chart ─────────────────────────────────────────────────────────────────────

def _spray_safe(rain_mm: float, prob: int, wind: float) -> bool:
    """Return True if rain/probability/wind are within spray-safe thresholds."""
    return (
        rain_mm < SPRAY_UNSAFE_RAIN_MM
        and prob < SPRAY_UNSAFE_PROB_PCT
        and (wind or 0) < SPRAY_UNSAFE_WIND_KMPH
    )


def _short_date(iso: str) -> str:
    """Format a YYYY-MM-DD date as a short weekday+day label."""
    dt = datetime.strptime(iso, "%Y-%m-%d")
    return dt.strftime("%a\n%d %b")


# Chart palette (GitHub-dark theme).
_COL = {
    "dark": "#0d1117", "card": "#161b22", "text": "#e6edf3", "muted": "#8b949e",
    "ecmwf": "#4fc3f7", "gfs": "#ffb74d", "prob": "#ce93d8",
    "grid": "#30363d", "safe": "#2ecc71", "avoid": "#e74c3c", "warn": "#f39c12",
}


def _model_confidence(ec: dict, gfs: dict) -> int:
    """% of days where ECMWF and GFS agree within 2mm rain and 20% probability."""
    n = len(ec["dates"])
    agree = sum(1 for i in range(n)
                if abs((ec["rain_mm"][i] or 0) - (gfs["rain_mm"][i] or 0)) <= 2.0
                and abs((ec["rain_prob"][i] or 0) - (gfs["rain_prob"][i] or 0)) <= 20)
    return int(agree / n * 100) if n else 0


def _spray_colors(ec: dict) -> list[str]:
    """Per-day spray-safe (green) / avoid (red) colors, driven by ECMWF."""
    return [_COL["safe"] if _spray_safe(ec["rain_mm"][i] or 0, ec["rain_prob"][i] or 0,
                                        ec["wind_max"][i] or 0) else _COL["avoid"]
            for i in range(len(ec["dates"]))]


def _draw_header(ax, name: str, now: dict, confidence: int) -> None:
    """Draw the chart header: title, model confidence, current conditions, legend hint."""
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    updated    = datetime.now().strftime("%d %b %Y, %H:%M IST")
    conf_color = _COL["safe"] if confidence >= 70 else _COL["warn"] if confidence >= 50 else _COL["avoid"]
    conf_label = "High" if confidence >= 70 else "Medium" if confidence >= 50 else "Low"
    ax.text(0.0, 0.80, f"Farm Weather — {name}", color=_COL["text"],
            fontsize=13, fontweight="bold", va="center")
    ax.text(1.0, 0.80, f"Model confidence: {conf_label} ({confidence}%)",
            color=conf_color, fontsize=10, fontweight="bold", va="center", ha="right")
    if now.get("temp_c") is not None:
        ax.text(0.0, 0.15, f"Now:  {now['temp_c']}°C    Humidity {now.get('humidity','--')}%"
                f"    Wind {now.get('wind_kmh','--')} km/h    Updated {updated}",
                color=_COL["muted"], fontsize=9, va="center")
    else:
        ax.text(0.0, 0.15, f"Updated {updated}", color=_COL["muted"], fontsize=9, va="center")
    ax.text(1.0, 0.15, "Green strip = safe to spray    Red strip = avoid spray",
            color=_COL["muted"], fontsize=8, va="center", ha="right")


def _draw_rain_panel(ax, x, labels, ec, gfs, spray_colors) -> None:
    """Draw the rainfall bars, probability twin-axis, spray strips, and disagreement marks."""
    bar_w = 0.38
    ax.bar(x - bar_w/2, ec["rain_mm"],  bar_w, color=_COL["ecmwf"], alpha=0.85)
    ax.bar(x + bar_w/2, gfs["rain_mm"], bar_w, color=_COL["gfs"],   alpha=0.85)
    for i, col in enumerate(spray_colors):
        ax.axvspan(i - 0.5, i + 0.5, ymin=0, ymax=0.07, color=col, alpha=0.75, zorder=0)

    prob = ax.twinx()
    prob.set_facecolor(_COL["card"])
    prob.plot(x, ec["rain_prob"],  color=_COL["prob"], linewidth=2, marker="o", markersize=5, zorder=5)
    prob.plot(x, gfs["rain_prob"], color=_COL["prob"], linewidth=1.5, linestyle="--",
              marker="s", markersize=4, zorder=5, alpha=0.7)
    prob.axhline(30, color=_COL["avoid"], linewidth=0.8, linestyle=":", alpha=0.6)
    prob.set_ylim(0, 110)
    prob.set_ylabel("Rain probability %", color=_COL["prob"], fontsize=9)
    prob.tick_params(colors=_COL["prob"], labelsize=8)
    for spine in prob.spines.values():
        spine.set_edgecolor(_COL["grid"])

    for i in range(len(ec["dates"])):
        if (abs((ec["rain_mm"][i] or 0) - (gfs["rain_mm"][i] or 0)) > 2.0
                or abs((ec["rain_prob"][i] or 0) - (gfs["rain_prob"][i] or 0)) > 20):
            y = max((ec["rain_mm"][i] or 0), (gfs["rain_mm"][i] or 0)) + 0.5
            ax.text(i, y, "!", ha="center", fontsize=14, fontweight="bold", color=_COL["warn"])

    ax.set_xticks(x); ax.set_xticklabels(labels, color=_COL["text"], fontsize=9)
    ax.set_ylabel("Rain (mm)", color=_COL["ecmwf"], fontsize=9)
    ax.tick_params(axis="y", colors=_COL["ecmwf"])
    ax.set_title("7-Day Rainfall Forecast", color=_COL["text"], fontsize=11, pad=8)
    ax.set_xlim(-0.6, len(ec["dates"]) - 0.4)
    ax.yaxis.grid(True, color=_COL["grid"], linewidth=0.5); ax.set_axisbelow(True)
    legend = [mpatches.Patch(color=_COL["ecmwf"], label="ECMWF rain mm"),
              mpatches.Patch(color=_COL["gfs"], label="GFS rain mm"),
              mpatches.Patch(color=_COL["prob"], label="Rain probability %"),
              mpatches.Patch(color=_COL["safe"], label="Safe to spray"),
              mpatches.Patch(color=_COL["avoid"], label="Avoid spray")]
    ax.legend(handles=legend, loc="upper left", fontsize=7.5, framealpha=0.3,
              facecolor=_COL["card"], edgecolor=_COL["grid"], labelcolor=_COL["text"])


def _draw_temp_panel(ax, x, labels, ec, gfs) -> None:
    """Draw the ECMWF/GFS temperature ranges with the phytotoxicity threshold."""
    ec_tmax  = [v or 0 for v in ec["temp_max"]]
    ec_tmin  = [v or 0 for v in ec["temp_min"]]
    gfs_tmax = [v or 0 for v in gfs["temp_max"]]
    gfs_tmin = [v or 0 for v in gfs["temp_min"]]
    ax.fill_between(x, ec_tmin, ec_tmax, color=_COL["ecmwf"], alpha=0.2, label="ECMWF range")
    ax.plot(x, ec_tmax, color=_COL["ecmwf"], linewidth=2, marker="o", markersize=5, label="ECMWF max")
    ax.plot(x, ec_tmin, color=_COL["ecmwf"], linewidth=1.5, linestyle="--", marker="o", markersize=4, alpha=0.7)
    ax.fill_between(x, gfs_tmin, gfs_tmax, color=_COL["gfs"], alpha=0.15, label="GFS range")
    ax.plot(x, gfs_tmax, color=_COL["gfs"], linewidth=1.5, linestyle="--", marker="s",
            markersize=4, label="GFS max", alpha=0.8)
    ax.axhline(35, color=_COL["avoid"], linewidth=0.8, linestyle=":", alpha=0.5, label="35°C phytotox risk")
    for i, v in enumerate(ec_tmax):
        ax.text(i, v + 0.4, f"{v:.0f}°", ha="center", fontsize=8, color=_COL["ecmwf"])
    ax.set_xticks(x); ax.set_xticklabels(labels, color=_COL["text"], fontsize=9)
    ax.set_ylabel("Temperature (°C)", color=_COL["text"], fontsize=9)
    ax.set_title("Temperature Range", color=_COL["text"], fontsize=11, pad=8)
    ax.set_xlim(-0.6, len(ec["dates"]) - 0.4)
    ax.yaxis.grid(True, color=_COL["grid"], linewidth=0.5); ax.set_axisbelow(True)
    ax.legend(loc="upper right", fontsize=7.5, framealpha=0.3,
              facecolor=_COL["card"], edgecolor=_COL["grid"], labelcolor=_COL["text"])


def build_chart(lat: float, lon: float, name: str) -> None:
    """Fetch ECMWF+GFS forecasts and render the 3-panel farm weather chart to OUTPUT."""
    print(f"Fetching ECMWF forecast for {name}...")
    ecmwf_raw = _fetch(lat, lon, "ecmwf")
    print("Fetching GFS forecast...")
    ec  = _parse(ecmwf_raw)
    gfs = _parse(_fetch(lat, lon, "gfs"))
    now = _current(ecmwf_raw)

    labels = [_short_date(d) for d in ec["dates"]]
    x      = np.arange(len(ec["dates"]))

    fig = plt.figure(figsize=(14, 9), facecolor=_COL["dark"])
    gs  = gridspec.GridSpec(3, 1, figure=fig, height_ratios=[0.14, 0.50, 0.36], hspace=0.50)
    header_ax, rain_ax, temp_ax = (fig.add_subplot(gs[i]) for i in range(3))
    for ax in (header_ax, rain_ax, temp_ax):
        ax.set_facecolor(_COL["card"])
        ax.tick_params(colors=_COL["text"], labelsize=9)
        for spine in ax.spines.values():
            spine.set_edgecolor(_COL["grid"])

    _draw_header(header_ax, name, now, _model_confidence(ec, gfs))
    _draw_rain_panel(rain_ax, x, labels, ec, gfs, _spray_colors(ec))
    _draw_temp_panel(temp_ax, x, labels, ec, gfs)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=130, bbox_inches="tight", facecolor=_COL["dark"], edgecolor="none")
    plt.close(fig)
    print(f"Saved: {OUTPUT}")


if __name__ == "__main__":
    lat, lon, name = _location()
    print(f"Location: {name} ({lat}, {lon})")
    try:
        build_chart(lat, lon, name)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
