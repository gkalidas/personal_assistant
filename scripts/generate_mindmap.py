"""
Generate the GK Personal Assistant mind map PNG.
Output: docs/mindmap.png
"""

import math
import os
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import numpy as np

OUT = Path(__file__).parent.parent / "docs" / "mindmap.png"
OUT.parent.mkdir(exist_ok=True)

# ── Colour palette ─────────────────────────────────────────────────────────────
C = {
    "bg":        "#0d1117",
    "center":    "#58a6ff",
    "farming":   "#3fb950",
    "finance":   "#f78166",
    "health":    "#ff7b72",
    "security":  "#ffa657",
    "core":      "#bc8cff",
    "llm":       "#79c0ff",
    "apis":      "#56d364",
    "eval":      "#d2a8ff",
    "edge":      "#30363d",
    "text_dark": "#0d1117",
    "text_light":"#e6edf3",
    "sub_text":  "#8b949e",
}

# ── Mind map data ──────────────────────────────────────────────────────────────
# (angle_deg, label, color_key, [(sublabel, ...)])
BRANCHES = [
    (  0, "Farming Module",   "farming", [
        "Current weather & 7-day forecast",
        "Spray safety check (wind/rain/humidity)",
        "Rainfall history (ERA5 archive)",
        "Plot management (add, list, crop history)",
        "Crop tracking (plant → harvest → yield)",
        "Spray log & observation diary",
        "Soil data (pH, N, clay via SoilGrids)",
        "Disease KB: Pomegranate · Sugarcane · Banana",
        "Fuzzy symptom → disease matching",
        "Mandi prices (APMC · data.gov.in · daily)",
        "Photo diagnosis (farming vision server)",
    ]),
    ( 45, "Finance Module",   "finance", [
        "Log income & expenses (category-tagged)",
        "Monthly summary (income vs spend vs net)",
        "Budget tracker (cap per category)",
        "Spending breakdown by category",
        "Financial goals (target + deadline)",
    ]),
    ( 90, "Health Module",    "health", [
        "Blood pressure (6 risk levels incl. crisis → call 108)",
        "Step count vs daily goal",
        "Weight tracking",
        "Sleep hours & quality",
        "Blood sugar (fasting / post-meal / random)",
        "7–14 day trends for any metric",
        "Goal setting (steps, weight, sleep)",
    ]),
    (135, "Diary Module",     "eval", [
        "Photo EXIF extraction (timestamp · GPS · device)",
        "Vision captions via moondream (local, no cloud)",
        "LLM diary entry writer (qwen3:1.7b)",
        "Weekly auto-draft from query history",
        "Draft → review → approve workflow",
        "Stored in SQLite (queryable + markdown)",
    ]),
    (180, "Dashboard",        "llm", [
        "Live system: CPU · RAM · Disk I/O",
        "Network interfaces: LAN · WiFi · Tethering",
        "Internet speed (download / upload Mbps)",
        "Multi-connection failover alerting",
        "Live weather widget (Barloni, Solapur)",
        "WebSocket push every 2s",
        "Phone access via Tailscale (fixed IP)",
    ]),
    (225, "Security Guardian","security", [
        "CVE scan — OSV.dev (daily)",
        "Code audit — bandit + custom patterns",
        "Threat intel — NVD · GitHub Advisory · CISA KEV · Arxiv",
        "Log anomaly detection (hourly)",
        "Auto-patch safe upgrades (same major, HIGH+ CVE)",
        "Idle-aware scheduler (one task at a time)",
        "Auto-starts on boot (systemd + linger)",
    ]),
    (270, "Core Layer",       "core", [
        "Router — qwen2.5:0.5b (intent → module)",
        "Config — single source (OLLAMA_URL · models)",
        "Sanitizer — 45 injection patterns + PII redact",
        "Memory — SQLite events log + user_profile.json",
        "Guardrails — web content injection defense",
        "Action schema validation (type-safe JSON)",
        "Follow-up suggestion engine",
        "System module — load · pattern · guardian",
    ]),
    (315, "External APIs",    "apis", [
        "Open-Meteo (weather · forecast · ERA5 archive)",
        "Open-Meteo Geocoding (location → lat/lon)",
        "SoilGrids / ISRIC (soil pH · N · clay · sand)",
        "data.gov.in APMC (live mandi prices)",
        "OSV.dev (Google — 20+ CVE databases)",
        "NVD / NIST (CVE search + LLM attacks)",
        "GitHub Advisory API (pip ecosystem)",
        "CISA KEV (actively exploited CVEs)",
        "Arxiv cs.CR RSS (AI security papers)",
    ]),
]

# ── Drawing helpers ────────────────────────────────────────────────────────────

def polar(angle_deg, r):
    """Convert a polar (angle°, radius) to cartesian (x, y)."""
    a = math.radians(angle_deg)
    return r * math.cos(a), r * math.sin(a)


def draw_rounded_box(ax, cx, cy, text, color, fontsize=9, alpha=0.92,
                     width=None, height=None, text_color=None):
    """Draw a rounded, filled text box centered at (cx, cy)."""
    tc = text_color or C["text_dark"]
    bbox = dict(boxstyle="round,pad=0.35", facecolor=color, edgecolor="white",
                linewidth=0.6, alpha=alpha)
    ax.text(cx, cy, text, ha="center", va="center", fontsize=fontsize,
            fontweight="bold", color=tc, bbox=bbox, zorder=5,
            wrap=False)


def draw_line(ax, x0, y0, x1, y1, color, lw=1.0, alpha=0.5, style="-"):
    """Draw a straight line between two points."""
    ax.plot([x0, x1], [y0, y1], color=color, lw=lw, alpha=alpha,
            linestyle=style, zorder=2)


def draw_curve(ax, x0, y0, x1, y1, color, lw=1.2, alpha=0.55):
    """Smooth bezier-ish curve via a midpoint."""
    mx = (x0 + x1) / 2
    my = (y0 + y1) / 2
    # Pull midpoint toward center for a curve effect
    mx = mx * 0.65
    my = my * 0.65
    from matplotlib.patches import FancyArrowPatch
    import matplotlib.patheffects as pe
    t = np.linspace(0, 1, 80)
    bx = (1-t)**2*x0 + 2*(1-t)*t*mx + t**2*x1
    by = (1-t)**2*y0 + 2*(1-t)*t*my + t**2*y1
    ax.plot(bx, by, color=color, lw=lw, alpha=alpha, zorder=2)


# ── Main draw ──────────────────────────────────────────────────────────────────

def _draw_center(ax) -> None:
    """Draw the central GK node and the subtitle line."""
    ax.add_patch(plt.Circle((0, 0), 1.35, color=C["center"], zorder=4))
    ax.text(0, 0.18, "GK", ha="center", va="center", fontsize=22,
            fontweight="bold", color=C["text_dark"], zorder=6)
    ax.text(0, -0.28, "Personal", ha="center", va="center", fontsize=9,
            color=C["text_dark"], zorder=6)
    ax.text(0, -0.62, "Assistant", ha="center", va="center", fontsize=9,
            color=C["text_dark"], zorder=6)
    ax.text(0, -1.75, "100% local · private · no cloud · router-based",
            ha="center", va="center", fontsize=8, color=C["sub_text"], zorder=4)


def _draw_branch(ax, angle, label, ckey, subs) -> None:
    """Draw one branch node, its curve from center, and its fanned-out sub-items."""
    color = C[ckey]
    bx, by = polar(angle, 4.2)
    draw_curve(ax, 0, 0, bx, by, color, lw=2.0, alpha=0.7)
    draw_rounded_box(ax, bx, by, label, color, fontsize=10, text_color=C["text_dark"])

    n = len(subs)
    spread  = min(50, n * 9)             # wedge width around the branch angle
    start_a = angle - spread / 2
    step_a  = spread / max(n - 1, 1) if n > 1 else 0
    for i, sub in enumerate(subs):
        sx, sy = polar(start_a + i * step_a, 7.8)
        draw_line(ax, bx, by, sx, sy, color, lw=0.9, alpha=0.45)
        ax.text(sx, sy, sub, ha="center", va="center", fontsize=6.6,
                color=C["text_light"], zorder=5,
                bbox=dict(boxstyle="round,pad=0.25", facecolor="#161b22",
                          edgecolor=color, linewidth=0.8, alpha=0.88))


def _draw_badges(ax) -> None:
    """Draw the LLM-stack line, the stat badges, and the title/subtitle."""
    ax.text(0, -10.5,
            "LLM Stack (fully local):  qwen3:1.7b  ·  qwen2.5:0.5b  |  "
            "Ollama  ·  SQLite  ·  Python 3.10",
            ha="center", va="center", fontsize=8, color=C["sub_text"])
    stats = [
        ("5 Modules", -6.5, 10.2, C["farming"]), ("10 Core files", -3.2, 10.2, C["llm"]),
        ("9 Free APIs", 0.0, 10.2, C["apis"]), ("45 Sec patterns", 3.2, 10.2, C["security"]),
        ("384 Tests", 6.5, 10.2, C["eval"]),
    ]
    for label, sx, sy, sc in stats:
        ax.text(sx, sy, label, ha="center", va="center", fontsize=8,
                fontweight="bold", color=C["text_dark"],
                bbox=dict(boxstyle="round,pad=0.3", facecolor=sc, edgecolor="none", alpha=0.9))
    ax.text(0, 10.8, "GK Personal Assistant — Architecture Mind Map",
            ha="center", va="center", fontsize=13, fontweight="bold", color=C["text_light"])
    ax.text(0, 10.45, "June 2026  ·  v0.1  ·  github.com/gkalidas",
            ha="center", va="center", fontsize=8, color=C["sub_text"])


def build(fig, ax):
    """Render the full architecture mind map onto the figure/axes."""
    ax.set_facecolor(C["bg"])
    fig.patch.set_facecolor(C["bg"])
    ax.set_xlim(-11, 11)
    ax.set_ylim(-11, 11)
    ax.set_aspect("equal")
    ax.axis("off")
    _draw_center(ax)
    for angle, label, ckey, subs in BRANCHES:
        _draw_branch(ax, angle, label, ckey, subs)
    _draw_badges(ax)


def main():
    """Generate the mind-map PNG and save it to OUT."""
    fig, ax = plt.subplots(figsize=(26, 26), dpi=160)
    build(fig, ax)
    plt.tight_layout(pad=0.2)
    fig.savefig(str(OUT), dpi=160, bbox_inches="tight",
                facecolor=C["bg"], edgecolor="none")
    print(f"Mind map saved → {OUT}")


if __name__ == "__main__":
    main()
