#!/usr/bin/env python3
"""
Generate a static "layered system model" poster — the still fallback for the
animated /showcase3d page. Mirrors that scene's concept:

  GUARDIAN security shell (outer) → ROUTER core → modules, connected to the
  INTERNET through the guardian, with attacks blocked at the perimeter.

Output: docs/system_model.png  (pure matplotlib, no browser required)
"""

import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch

OUT = Path(__file__).resolve().parent.parent / "docs" / "system_model.png"
OUT.parent.mkdir(exist_ok=True)

BG     = "#030b10"
CYAN   = "#00e5ff"
GREEN  = "#00ff88"
RED    = "#ff2255"
NET    = "#9fd6ff"
DIM    = "#1a4a5e"
TEXT   = "#cfe6ee"

R_SHELL = 4.7
R_MOD   = 2.5
R_CORE  = 0.78

# name · colour · angle(deg)
MODULES = [
    ("FARMING",   "#3fb950",  90),
    ("FINANCE",   "#ffaa00",  18),
    ("HEALTH",    "#ff2255", -54),
    ("DIARY",     "#bc8cff", -126),
    ("DASHBOARD", "#56d3ff",  162),
]


def _pol(angle_deg, r):
    """Polar (deg, radius) → cartesian (x, y)."""
    a = math.radians(angle_deg)
    return r * math.cos(a), r * math.sin(a)


def _glow_ring(ax, xy, r, color, layers=7, base_lw=2.4, alpha=0.5):
    """Draw a soft glowing ring by stacking translucent circles."""
    for i in range(layers, 0, -1):
        ax.add_patch(Circle(xy, r + i * 0.06, fill=False, edgecolor=color,
                            lw=base_lw + i * 0.9, alpha=alpha * (1 - i / (layers + 2)) * 0.5))
    ax.add_patch(Circle(xy, r, fill=False, edgecolor=color, lw=base_lw, alpha=0.95))


def _node(ax, xy, r, color, label, sub=None):
    """Draw a filled glowing node with a label (and optional sub-label)."""
    for i in range(5, 0, -1):
        ax.add_patch(Circle(xy, r + i * 0.05, color=color, alpha=0.06))
    ax.add_patch(Circle(xy, r, color=color, alpha=0.95, zorder=5))
    ax.add_patch(Circle(xy, r, fill=False, edgecolor="white", lw=0.6, alpha=0.5, zorder=6))
    ax.text(xy[0], xy[1] - r - 0.34, label, ha="center", va="top", color=color,
            fontsize=10.5, fontweight="bold", zorder=7)
    if sub:
        ax.text(xy[0], xy[1], sub, ha="center", va="center", color=BG,
                fontsize=7.5, fontweight="bold", zorder=7)


def _draw_guardian(ax):
    """Outer GUARDIAN shell + heading."""
    _glow_ring(ax, (0, 0), R_SHELL, GREEN)
    ax.text(0, R_SHELL + 0.55, "GUARDIAN  ·  security shell", ha="center", va="bottom",
            color=GREEN, fontsize=11, fontweight="bold")


def _draw_core_and_modules(ax):
    """ROUTER core, the module nodes, and their links + router↔guardian struts."""
    # struts: core → shell (router secured by the guardian)
    for ang in range(0, 360, 30):
        sx, sy = _pol(ang, R_SHELL)
        ax.plot([0, sx], [0, sy], color="#13b5a6", lw=0.8, alpha=0.22, zorder=1)
    # core → module links
    for name, color, ang in MODULES:
        mx, my = _pol(ang, R_MOD)
        ax.plot([0, mx], [0, my], color=color, lw=1.3, alpha=0.4, zorder=2)
    # nodes
    _node(ax, (0, 0), R_CORE, CYAN, "ROUTER", "intent")
    ax.text(0, -R_CORE - 0.34 - 0.36, "qwen2.5:0.5b", ha="center", va="top",
            color=DIM, fontsize=7.5, zorder=7)
    for name, color, ang in MODULES:
        _node(ax, _pol(ang, R_MOD), 0.56, color, name)


def _draw_internet(ax):
    """INTERNET node outside the shell + link through the guardian."""
    ix, iy = _pol(36, R_SHELL + 1.7)
    sx, sy = _pol(36, R_SHELL)
    ax.plot([sx, ix], [sy, iy], color=NET, lw=1.4, alpha=0.6, zorder=2)
    for dx, dy, r in [(-0.42, 0.05, 0.5), (0.0, 0.16, 0.6), (0.42, 0.05, 0.46), (0.1, -0.18, 0.4)]:
        ax.add_patch(Circle((ix + dx, iy + dy), r, color=NET, alpha=0.85, zorder=5))
    ax.text(ix, iy - 0.95, "INTERNET / APIs", ha="center", va="top", color=NET,
            fontsize=9, fontweight="bold", zorder=7)


def _draw_attacks(ax):
    """Red attack arrows from outside, blocked at the shell (✕ marks)."""
    for ang in (205, 240, 285, 320, 150):
        ox, oy = _pol(ang, R_SHELL + 2.1)
        hx, hy = _pol(ang, R_SHELL + 0.12)
        ax.add_patch(FancyArrowPatch((ox, oy), (hx, hy), arrowstyle="-|>",
                     mutation_scale=13, color=RED, lw=1.6, alpha=0.85, zorder=4))
        bx, by = _pol(ang, R_SHELL)
        ax.scatter([bx], [by], s=120, marker="x", color=RED, linewidths=2, zorder=6, alpha=0.9)
    ax.text(0, -R_SHELL - 0.95, "attacks blocked at the perimeter",
            ha="center", va="top", color=RED, fontsize=8, alpha=0.85)


def build(fig, ax):
    """Render the full poster onto the figure/axes."""
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(-7.2, 7.2)
    ax.set_ylim(-7.2, 7.2)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.text(0, 6.7, "GK PERSONAL ASSISTANT — SYSTEM MODEL", ha="center", va="center",
            color=CYAN, fontsize=13, fontweight="bold")
    ax.text(0, 6.18, "guardian protects · router routes · modules answer · 100% local",
            ha="center", va="center", color=DIM, fontsize=8.5)
    _draw_guardian(ax)
    _draw_attacks(ax)
    _draw_internet(ax)
    _draw_core_and_modules(ax)


def main():
    """Generate the system-model PNG and save it to OUT."""
    fig, ax = plt.subplots(figsize=(12, 12), dpi=150)
    build(fig, ax)
    plt.tight_layout(pad=0.4)
    fig.savefig(str(OUT), dpi=150, facecolor=BG, edgecolor="none", bbox_inches="tight")
    print(f"system model saved → {OUT}")


if __name__ == "__main__":
    main()
