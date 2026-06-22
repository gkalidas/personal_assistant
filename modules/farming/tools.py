from datetime import datetime, date
from typing import Any

from modules.farming.db import conn


# ── Plots ─────────────────────────────────────────────────────────────────────

def add_plot(name: str, area_acres: float | None = None, soil_type: str | None = None,
             lat: float | None = None, lon: float | None = None, notes: str = "") -> dict:
    """Create a plot and return its id and name."""
    now = datetime.now().isoformat()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO plots (name, area_acres, soil_type, lat, lon, notes, created_at) VALUES (?,?,?,?,?,?,?)",
            (name, area_acres, soil_type, lat, lon, notes, now),
        )
        return {"id": cur.lastrowid, "name": name}


def list_plots() -> list[dict]:
    """Return all plots ordered by name."""
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM plots ORDER BY name").fetchall()]


def get_plot(name: str) -> dict | None:
    """Return the plot matching a name (case-insensitive), or None."""
    with conn() as c:
        row = c.execute("SELECT * FROM plots WHERE lower(name) = lower(?)", (name,)).fetchone()
    return dict(row) if row else None


# ── Crops ─────────────────────────────────────────────────────────────────────

def plant_crop(plot_name: str, crop_name: str, variety: str | None = None,
               planted_date: str | None = None, expected_harvest: str | None = None,
               notes: str = "") -> dict:
    """Plant a crop on a plot (mirrored to crop_plots for the farming server). Returns its id."""
    plot = get_plot(plot_name)
    if not plot:
        return {"error": f"Plot '{plot_name}' not found"}
    now = datetime.now().isoformat()
    planted_date = planted_date or date.today().isoformat()
    with conn() as c:
        cur = c.execute(
            """INSERT INTO crops (plot_id, crop_name, variety, planted_date, expected_harvest, notes, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (plot["id"], crop_name, variety, planted_date, expected_harvest, notes, now),
        )
        crop_id = cur.lastrowid

        # Mirror into crop_plots so the farming server sees new crops from GK
        try:
            c.execute(
                "INSERT OR IGNORE INTO crop_plots (crop, location, planted_at, lat, lon, notes, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (crop_name, plot_name, planted_date, plot.get("lat"), plot.get("lon"), notes, now),
            )
        except Exception:
            pass  # crop_plots may not exist in standalone PA DB

        return {"id": crop_id, "plot": plot_name, "crop": crop_name, "planted": planted_date}


def list_crops(plot_name: str | None = None, status: str = "active") -> list[dict]:
    """List crops of a given status, optionally filtered to one plot, with plot names."""
    with conn() as c:
        if plot_name:
            plot = get_plot(plot_name)
            if not plot:
                return []
            rows = c.execute(
                "SELECT c.*, p.name as plot_name FROM crops c JOIN plots p ON c.plot_id=p.id WHERE c.plot_id=? AND c.status=?",
                (plot["id"], status),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT c.*, p.name as plot_name FROM crops c JOIN plots p ON c.plot_id=p.id WHERE c.status=?",
                (status,),
            ).fetchall()
    return [dict(r) for r in rows]


def update_crop_status(crop_id: int, status: str, yield_kg: float | None = None) -> dict:
    """Update a crop's status (and yield) and return the updated row."""
    with conn() as c:
        c.execute("UPDATE crops SET status=?, yield_kg=? WHERE id=?", (status, yield_kg, crop_id))
        row = c.execute("SELECT * FROM crops WHERE id=?", (crop_id,)).fetchone()
    return dict(row)


# ── Spray Logs ────────────────────────────────────────────────────────────────

def log_spray(plot_name: str, chemical: str, quantity: str | None = None,
              reason: str | None = None, date_str: str | None = None, notes: str = "") -> dict:
    """Record a spray application on a plot and return its id."""
    plot = get_plot(plot_name)
    if not plot:
        return {"error": f"Plot '{plot_name}' not found"}
    date_str = date_str or date.today().isoformat()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO spray_logs (plot_id, date, chemical, quantity, reason, notes) VALUES (?,?,?,?,?,?)",
            (plot["id"], date_str, chemical, quantity, reason, notes),
        )
        return {"id": cur.lastrowid, "plot": plot_name, "chemical": chemical, "date": date_str}


def spray_history(plot_name: str, limit: int = 20) -> list[dict]:
    """Return recent spray logs for a plot, newest first."""
    plot = get_plot(plot_name)
    if not plot:
        return []
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM spray_logs WHERE plot_id=? ORDER BY date DESC LIMIT ?",
            (plot["id"], limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Observations ──────────────────────────────────────────────────────────────

def log_observation(plot_name: str, obs_type: str, description: str,
                    severity: str | None = None, image_path: str | None = None) -> dict:
    """Record a field observation (disease/pest/etc.) on a plot and return its id."""
    plot = get_plot(plot_name)
    if not plot:
        return {"error": f"Plot '{plot_name}' not found"}
    today = date.today().isoformat()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO observations (plot_id, date, type, description, severity, image_path) VALUES (?,?,?,?,?,?)",
            (plot["id"], today, obs_type, description, severity, image_path),
        )
        return {"id": cur.lastrowid, "plot": plot_name, "type": obs_type}


def open_observations(plot_name: str | None = None) -> list[dict]:
    """Return unresolved observations, optionally filtered to one plot, with plot names."""
    with conn() as c:
        if plot_name:
            plot = get_plot(plot_name)
            if not plot:
                return []
            rows = c.execute(
                "SELECT o.*, p.name as plot_name FROM observations o JOIN plots p ON o.plot_id=p.id WHERE o.plot_id=? AND o.resolved=0 ORDER BY o.date DESC",
                (plot["id"],),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT o.*, p.name as plot_name FROM observations o JOIN plots p ON o.plot_id=p.id WHERE o.resolved=0 ORDER BY o.date DESC"
            ).fetchall()
    return [dict(r) for r in rows]


# ── Season Summary ────────────────────────────────────────────────────────────

def season_summary() -> dict[str, Any]:
    """Return season-wide counts: plots, active/harvested crops, yield, open obs, sprays this month."""
    with conn() as c:
        plots = c.execute("SELECT COUNT(*) as n FROM plots").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) as n FROM crops WHERE status='active'").fetchone()["n"]
        harvested = c.execute("SELECT COUNT(*) as n FROM crops WHERE status='harvested'").fetchone()["n"]
        total_yield = c.execute("SELECT COALESCE(SUM(yield_kg),0) as t FROM crops WHERE status='harvested'").fetchone()["t"]
        open_obs = c.execute("SELECT COUNT(*) as n FROM observations WHERE resolved=0").fetchone()["n"]
        sprays_this_month = c.execute(
            "SELECT COUNT(*) as n FROM spray_logs WHERE strftime('%Y-%m', date) = strftime('%Y-%m', 'now')"
        ).fetchone()["n"]
    return {
        "plots": plots,
        "active_crops": active,
        "harvested_crops": harvested,
        "total_yield_kg": total_yield,
        "open_observations": open_obs,
        "sprays_this_month": sprays_this_month,
    }
