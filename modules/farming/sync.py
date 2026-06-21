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


def sync_crop_plots_to_pa() -> dict:
    """
    Read every row in crop_plots, ensure a matching row exists in PA's
    plots + crops tables. Idempotent — safe to call repeatedly.
    Returns {"synced_plots": N, "synced_crops": M}.
    """
    from modules.farming.db import conn

    synced_plots = 0
    synced_crops = 0
    now = datetime.now().isoformat()

    try:
        with conn() as c:
            try:
                rows = c.execute(
                    "SELECT id, crop, location, planted_at, lat, lon, notes "
                    "FROM crop_plots ORDER BY id"
                ).fetchall()
            except Exception:
                # crop_plots doesn't exist — standalone PA DB, nothing to sync
                return {"synced_plots": 0, "synced_crops": 0}

            for cp_id, crop, location, planted_at, lat, lon, notes in rows:
                plot_name = (location or "").strip() or f"Farm-{cp_id}"

                # Upsert into plots
                existing_plot = c.execute(
                    "SELECT id FROM plots WHERE lower(name)=lower(?)", (plot_name,)
                ).fetchone()

                if existing_plot:
                    plot_id = existing_plot[0]
                else:
                    cur = c.execute(
                        "INSERT INTO plots (name, lat, lon, notes, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (plot_name, lat, lon, notes or "", now),
                    )
                    plot_id = cur.lastrowid
                    synced_plots += 1
                    log.info("sync: added plot '%s' from crop_plots id=%d", plot_name, cp_id)

                # Upsert into crops (match on plot_id + crop_name + planted_date)
                existing_crop = c.execute(
                    "SELECT id FROM crops WHERE plot_id=? AND lower(crop_name)=lower(?) "
                    "AND planted_date=?",
                    (plot_id, crop, planted_at),
                ).fetchone()

                if not existing_crop:
                    c.execute(
                        "INSERT INTO crops (plot_id, crop_name, planted_date, status, created_at) "
                        "VALUES (?,?,?,'active',?)",
                        (plot_id, crop, planted_at, now),
                    )
                    synced_crops += 1
                    log.info("sync: added crop '%s' on plot '%s'", crop, plot_name)

    except Exception as e:
        log.error("sync_crop_plots_to_pa failed: %s", e)
        return {"synced_plots": 0, "synced_crops": 0, "error": str(e)}

    if synced_plots or synced_crops:
        log.info("farming sync complete: %d plot(s), %d crop(s) added", synced_plots, synced_crops)
    return {"synced_plots": synced_plots, "synced_crops": synced_crops}
