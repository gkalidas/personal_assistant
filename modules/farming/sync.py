"""
One-way sync: farming project's crop_plots → PA's plots + crops tables.

Both apps share the same farming.db file (set via FARMING_DB env var).
The farming server writes to crop_plots / analyses.
PA writes to plots / crops / spray_logs / observations.

This sync runs at startup and every 30 min so PA can see plots and crops
that were registered directly through the standalone farming app.
"""

from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger(__name__)


def _upsert_plot(c, plot_name: str, lat, lon, notes, now: str) -> tuple[int, bool]:
    """Find or insert a plot by name (case-insensitive). Returns (plot_id, was_added)."""
    existing = c.execute(
        "SELECT id FROM plots WHERE lower(name)=lower(?)", (plot_name,)
    ).fetchone()
    if existing:
        return existing[0], False
    cur = c.execute(
        "INSERT INTO plots (name, lat, lon, notes, created_at) VALUES (?,?,?,?,?)",
        (plot_name, lat, lon, notes or "", now),
    )
    log.info("sync: added plot '%s'", plot_name)
    return cur.lastrowid, True


def _upsert_crop(c, plot_id: int, crop: str, planted_at, now: str) -> bool:
    """Insert a crop if (plot, crop, planted_date) isn't present. Returns True if added."""
    existing = c.execute(
        "SELECT id FROM crops WHERE plot_id=? AND lower(crop_name)=lower(?) AND planted_date=?",
        (plot_id, crop, planted_at),
    ).fetchone()
    if existing:
        return False
    c.execute(
        "INSERT INTO crops (plot_id, crop_name, planted_date, status, created_at) "
        "VALUES (?,?,?,'active',?)",
        (plot_id, crop, planted_at, now),
    )
    log.info("sync: added crop '%s' on plot id=%d", crop, plot_id)
    return True


def sync_crop_plots_to_pa() -> dict:
    """Sync the farming server's crop_plots into PA's plots + crops tables.

    Idempotent — safe to call repeatedly. Returns {"synced_plots", "synced_crops"}.
    """
    from modules.farming.db import conn

    synced_plots = synced_crops = 0
    now = datetime.now().isoformat()
    try:
        with conn() as c:
            try:
                rows = c.execute(
                    "SELECT id, crop, location, planted_at, lat, lon, notes "
                    "FROM crop_plots ORDER BY id"
                ).fetchall()
            except Exception:
                return {"synced_plots": 0, "synced_crops": 0}  # no crop_plots table

            for cp_id, crop, location, planted_at, lat, lon, notes in rows:
                plot_name = (location or "").strip() or f"Farm-{cp_id}"
                plot_id, added_plot = _upsert_plot(c, plot_name, lat, lon, notes, now)
                synced_plots += added_plot
                synced_crops += _upsert_crop(c, plot_id, crop, planted_at, now)
    except Exception as e:
        log.error("sync_crop_plots_to_pa failed: %s", e)
        return {"synced_plots": 0, "synced_crops": 0, "error": str(e)}

    if synced_plots or synced_crops:
        log.info("farming sync complete: %d plot(s), %d crop(s) added", synced_plots, synced_crops)
    return {"synced_plots": synced_plots, "synced_crops": synced_crops}
