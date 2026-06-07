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
    c = data.get("current", {})
    return {
        "temp_c":   c.get("temperature_2m"),
        "humidity": c.get("relative_humidity_2m"),
        "rain_mm":  c.get("precipitation"),
        "wind_kmh": c.get("windspeed_10m"),
    }


# ── Chart ─────────────────────────────────────────────────────────────────────

def _spray_safe(rain_mm: float, prob: int, wind: float) -> bool:
    return (
        rain_mm < SPRAY_UNSAFE_RAIN_MM
        and prob < SPRAY_UNSAFE_PROB_PCT
        and (wind or 0) < SPRAY_UNSAFE_WIND_KMPH
    )


def _short_date(iso: str) -> str:
    dt = datetime.strptime(iso, "%Y-%m-%d")
    return dt.strftime("%a\n%d %b")


def build_chart(lat: float, lon: float, name: str) -> None:
    print(f"Fetching ECMWF forecast for {name}...")
    ecmwf_raw = _fetch(lat, lon, "ecmwf")
    print("Fetching GFS forecast...")
    gfs_raw   = _fetch(lat, lon, "gfs")

    ec  = _parse(ecmwf_raw)
    gfs = _parse(gfs_raw)
    now = _current(ecmwf_raw)

    dates     = ec["dates"]
    labels    = [_short_date(d) for d in dates]
    x         = np.arange(len(dates))
    bar_w     = 0.38

    # Confidence: % of days where models agree within 2mm and 20% prob
    agree_count = sum(
        1 for i in range(len(dates))
        if abs((ec["rain_mm"][i] or 0) - (gfs["rain_mm"][i] or 0)) <= 2.0
        and abs((ec["rain_prob"][i] or 0) - (gfs["rain_prob"][i] or 0)) <= 20
    )
    confidence = int(agree_count / len(dates) * 100)

    # Spray safety per day (ECMWF drives the decision)
    spray_colors = [
        "#2ecc71" if _spray_safe(
            ec["rain_mm"][i] or 0,
            ec["rain_prob"][i] or 0,
            ec["wind_max"][i] or 0,
        ) else "#e74c3c"
        for i in range(len(dates))
    ]

    # ── Figure layout ─────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(14, 8), facecolor="#0d1117")
    gs  = gridspec.GridSpec(
        3, 1, figure=fig,
        height_ratios=[0.12, 0.52, 0.36],
        hspace=0.45,
    )

    header_ax = fig.add_subplot(gs[0])
    rain_ax   = fig.add_subplot(gs[1])
    temp_ax   = fig.add_subplot(gs[2])

    dark_bg    = "#0d1117"
    card_bg    = "#161b22"
    text_color = "#e6edf3"
    muted      = "#8b949e"
    ecmwf_col  = "#4fc3f7"
    gfs_col    = "#ffb74d"
    prob_col   = "#ce93d8"

    for ax in [header_ax, rain_ax, temp_ax]:
        ax.set_facecolor(card_bg)
        ax.tick_params(colors=text_color, labelsize=9)
        for spine in ax.spines.values():
            spine.set_edgecolor("#30363d")

    # ── Header ────────────────────────────────────────────────────────────────
    header_ax.set_xlim(0, 1)
    header_ax.set_ylim(0, 1)
    header_ax.axis("off")

    updated = datetime.now().strftime("%d %b %Y, %H:%M IST")
    conf_color = "#2ecc71" if confidence >= 70 else "#f39c12" if confidence >= 50 else "#e74c3c"

    header_ax.text(0.0, 0.75, f"Farm Weather — {name}",
                   color=text_color, fontsize=13, fontweight="bold", va="center")
    header_ax.text(0.0, 0.2, f"Updated {updated}",
                   color=muted, fontsize=9, va="center")

    if now.get("temp_c") is not None:
        summary = (
            f"Now:  {now['temp_c']}°C   "
            f"Humidity {now.get('humidity', '--')}%   "
            f"Wind {now.get('wind_kmh', '--')} km/h   "
            f"Rain {now.get('rain_mm', 0)} mm"
        )
        header_ax.text(0.5, 0.75, summary,
                       color=text_color, fontsize=10, va="center")

    header_ax.text(0.72, 0.75,
                   f"Model agreement: {confidence}%",
                   color=conf_color, fontsize=10, fontweight="bold", va="center")
    header_ax.text(0.72, 0.2,
                   "Green bar = safe to spray   Red bar = avoid",
                   color=muted, fontsize=8, va="center")

    # ── Rain + Probability ─────────────────────────────────────────────────────
    ec_bars  = rain_ax.bar(x - bar_w/2, ec["rain_mm"],  bar_w,
                           color=[ecmwf_col]*len(x), alpha=0.85, label="ECMWF (rain mm)")
    gfs_bars = rain_ax.bar(x + bar_w/2, gfs["rain_mm"], bar_w,
                           color=[gfs_col]*len(x),   alpha=0.85, label="GFS (rain mm)")

    # Spray safety overlay (colored bottom strip)
    for i, col in enumerate(spray_colors):
        rain_ax.axvspan(i - 0.5, i + 0.5, ymin=0, ymax=0.04,
                        color=col, alpha=0.6, zorder=0)

    prob_ax2 = rain_ax.twinx()
    prob_ax2.set_facecolor(card_bg)
    prob_ax2.plot(x, ec["rain_prob"],  color=prob_col, linewidth=2,
                  marker="o", markersize=5, label="ECMWF prob %", zorder=5)
    prob_ax2.plot(x, gfs["rain_prob"], color=prob_col, linewidth=1.5,
                  linestyle="--", marker="s", markersize=4, label="GFS prob %", zorder=5, alpha=0.7)
    prob_ax2.axhline(30, color="#e74c3c", linewidth=0.8, linestyle=":", alpha=0.6)
    prob_ax2.set_ylim(0, 110)
    prob_ax2.set_ylabel("Rain probability %", color=prob_col, fontsize=9)
    prob_ax2.tick_params(colors=prob_col, labelsize=8)
    for spine in prob_ax2.spines.values():
        spine.set_edgecolor("#30363d")

    # Disagreement markers
    for i in range(len(dates)):
        diff_mm   = abs((ec["rain_mm"][i]   or 0) - (gfs["rain_mm"][i]   or 0))
        diff_prob = abs((ec["rain_prob"][i]  or 0) - (gfs["rain_prob"][i]  or 0))
        if diff_mm > 2.0 or diff_prob > 20:
            rain_ax.text(i, max((ec["rain_mm"][i] or 0), (gfs["rain_mm"][i] or 0)) + 0.2,
                         "⚠", ha="center", fontsize=10, color="#f39c12")

    rain_ax.set_xticks(x)
    rain_ax.set_xticklabels(labels, color=text_color, fontsize=9)
    rain_ax.set_ylabel("Rain (mm)", color=ecmwf_col, fontsize=9)
    rain_ax.tick_params(axis="y", colors=ecmwf_col)
    rain_ax.set_title("7-Day Rainfall Forecast", color=text_color, fontsize=11, pad=8)
    rain_ax.set_xlim(-0.6, len(dates) - 0.4)
    rain_ax.yaxis.grid(True, color="#30363d", linewidth=0.5)
    rain_ax.set_axisbelow(True)

    legend_patches = [
        mpatches.Patch(color=ecmwf_col, label="ECMWF rain mm"),
        mpatches.Patch(color=gfs_col,   label="GFS rain mm"),
        mpatches.Patch(color=prob_col,  label="Rain probability %"),
        mpatches.Patch(color="#2ecc71", label="Safe to spray"),
        mpatches.Patch(color="#e74c3c", label="Avoid spray"),
    ]
    rain_ax.legend(handles=legend_patches, loc="upper left",
                   fontsize=7.5, framealpha=0.3,
                   facecolor=card_bg, edgecolor="#30363d", labelcolor=text_color)

    # ── Temperature ───────────────────────────────────────────────────────────
    ec_tmax  = [v or 0 for v in ec["temp_max"]]
    ec_tmin  = [v or 0 for v in ec["temp_min"]]
    gfs_tmax = [v or 0 for v in gfs["temp_max"]]
    gfs_tmin = [v or 0 for v in gfs["temp_min"]]

    temp_ax.fill_between(x, ec_tmin, ec_tmax,
                         color=ecmwf_col, alpha=0.2, label="ECMWF range")
    temp_ax.plot(x, ec_tmax, color=ecmwf_col, linewidth=2,
                 marker="o", markersize=5, label="ECMWF max")
    temp_ax.plot(x, ec_tmin, color=ecmwf_col, linewidth=1.5,
                 linestyle="--", marker="o", markersize=4, alpha=0.7)

    temp_ax.fill_between(x, gfs_tmin, gfs_tmax,
                         color=gfs_col, alpha=0.15, label="GFS range")
    temp_ax.plot(x, gfs_tmax, color=gfs_col, linewidth=1.5,
                 linestyle="--", marker="s", markersize=4, label="GFS max", alpha=0.8)

    temp_ax.axhline(35, color="#e74c3c", linewidth=0.8, linestyle=":",
                    alpha=0.5, label="35°C phytotox risk")

    # Labels on ECMWF max
    for i, v in enumerate(ec_tmax):
        temp_ax.text(i, v + 0.4, f"{v:.0f}°", ha="center",
                     fontsize=8, color=ecmwf_col)

    temp_ax.set_xticks(x)
    temp_ax.set_xticklabels(labels, color=text_color, fontsize=9)
    temp_ax.set_ylabel("Temperature (°C)", color=text_color, fontsize=9)
    temp_ax.set_title("Temperature Range", color=text_color, fontsize=11, pad=8)
    temp_ax.set_xlim(-0.6, len(dates) - 0.4)
    temp_ax.yaxis.grid(True, color="#30363d", linewidth=0.5)
    temp_ax.set_axisbelow(True)
    temp_ax.legend(loc="upper right", fontsize=7.5, framealpha=0.3,
                   facecolor=card_bg, edgecolor="#30363d", labelcolor=text_color)

    # ── Save ──────────────────────────────────────────────────────────────────
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=130, bbox_inches="tight",
                facecolor=dark_bg, edgecolor="none")
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
