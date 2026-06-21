"""
Diary writer — generates personal diary entries from photo metadata and captions.

Uses qwen3:1.7b (text LLM) to write warm, personal diary entries grounded in
EXIF data (date, time, device) and vision captions.
"""

import logging
import os
from datetime import datetime

import httpx

from core.config import OLLAMA_URL, TEXT_MODEL
from core.sanitizer import sanitize_external_text

log = logging.getLogger(__name__)


def _day_context(date_str: str) -> str:
    """
    Pull cross-module context for a given diary date.
    Returns a plain-text block injected into the diary LLM prompt so the entry
    reflects health readings, farm activity, spending, and completed tasks — not
    just photos.
    """
    parts = []

    # ── Health readings ────────────────────────────────────────────────────────
    try:
        from modules.health.db import conn as _hconn
        with _hconn() as c:
            rows = c.execute(
                "SELECT type, value1, value2, unit FROM health_readings "
                "WHERE date=? ORDER BY time", (date_str,)
            ).fetchall()
        health_lines = []
        for r in rows:
            t = r["type"]
            if t == "bp":
                health_lines.append(f"BP {int(r['value1'])}/{int(r['value2'])} mmHg")
            elif t == "steps":
                health_lines.append(f"{int(r['value1'])} steps")
            elif t == "sleep":
                health_lines.append(f"slept {r['value1']}h")
            elif t == "weight":
                health_lines.append(f"weight {r['value1']} kg")
            elif t == "sugar":
                health_lines.append(f"sugar {r['value1']} mg/dL")
            elif t == "mood":
                health_lines.append(f"mood: {r['unit'] or r['value1']}")
        if health_lines:
            parts.append("Health: " + ", ".join(health_lines))
    except Exception:
        pass

    # ── Farm activities ────────────────────────────────────────────────────────
    try:
        from modules.farming.db import conn as _fconn
        with _fconn() as c:
            sprays = c.execute(
                "SELECT p.name as plot, s.chemical, s.quantity "
                "FROM spray_logs s JOIN plots p ON s.plot_id=p.id WHERE s.date=?",
                (date_str,)
            ).fetchall()
            obs = c.execute(
                "SELECT p.name as plot, o.type, o.description, o.severity "
                "FROM observations o JOIN plots p ON o.plot_id=p.id WHERE o.date=?",
                (date_str,)
            ).fetchall()
        farm_lines = []
        for s in sprays:
            qty = f" ({s['quantity']})" if s["quantity"] else ""
            farm_lines.append(f"sprayed {s['chemical']}{qty} on {s['plot']}")
        for o in obs:
            sev = f" [{o['severity']}]" if o["severity"] else ""
            desc = sanitize_external_text(o["description"][:60], label="ctx:obs")
            farm_lines.append(f"{o['type']} on {o['plot']}: {desc}{sev}")
        if farm_lines:
            parts.append("Farm: " + "; ".join(farm_lines))
    except Exception:
        pass

    # ── Finance transactions ───────────────────────────────────────────────────
    try:
        from modules.finance.db import conn as _finconn
        from core.crypto import decrypt as _dec
        with _finconn() as c:
            rows = c.execute(
                "SELECT amount, type, category, description FROM transactions "
                "WHERE date(ts)=? ORDER BY ts", (date_str,)
            ).fetchall()
        fin_lines = []
        for r in rows:
            desc = _dec(r["description"]) if r["description"] else ""
            desc = sanitize_external_text(desc[:40], label="ctx:finance") if desc else ""
            sign = "+" if r["type"] == "income" else "-"
            label = f"{sign}₹{r['amount']:.0f} {r['category']}"
            if desc:
                label += f" ({desc})"
            fin_lines.append(label)
        if fin_lines:
            parts.append("Spending: " + "; ".join(fin_lines))
    except Exception:
        pass

    # ── Completed todos ────────────────────────────────────────────────────────
    try:
        from dashboard.todo_store import list_todos as _todos
        done = [t["text"] for t in _todos(include_done=True)
                if t.get("done") == 1 and (t.get("updated_at") or "")[:10] == date_str]
        if done:
            safe_done = [sanitize_external_text(d[:60], label="ctx:todo") for d in done]
            parts.append("Completed tasks: " + "; ".join(safe_done))
    except Exception:
        pass

    return "\n".join(parts)


_SYSTEM = """You are writing a personal diary for Ganesh Kalidas, a farmer and entrepreneur
from Barloni village, Solapur district, Maharashtra, India.

Write in first person, warm and honest. Ganesh is down-to-earth — not flowery.
He has pomegranate and sugarcane farms, a family, and splits time between the farm (Barloni)
and city (Pune/Bavdhan). He uses his iPhone to take photos.

Rules:
- Ground the entry in the photos, their timestamps, and the day context below
- Weave health stats naturally ("walked a lot today", "BP was on the higher side") — don't list them robotically
- Mention farm activities if they happened that day
- Note significant spending only if it tells a story (e.g., "bought seeds for the new plot")
- Keep it 150-250 words
- Simple English, natural — occasional Hindi/Marathi word is fine (pani, bhai, masala, etc.)
- End with one line about what you're thinking or feeling
- No bullet points — flowing diary prose only
- Never say "I took X photos" — just describe the day naturally"""


def _build_photo_summary(photos: list[dict], captions: dict[str, str]) -> str:
    """Build a concise photo summary for the LLM prompt."""
    lines = []
    for i, meta in enumerate(photos, 1):
        cap = captions.get(meta["filename"], "")
        time_s = meta.get("time_str", "")
        if cap:
            lines.append(f"{i}. {time_s} — {cap}")
        else:
            lines.append(f"{i}. {time_s} — (photo, no description available)")
    return "\n".join(lines)


def write_diary_entry(
    date_str: str,
    photos: list[dict],
    captions: dict[str, str],
    profile: dict,
) -> str:
    """
    Generate a diary entry for one day.

    Args:
        date_str:  "2025-08-03"
        photos:    list of photo metadata dicts from photo_reader
        captions:  {filename: caption} from vision.caption_batch
        profile:   user profile dict (for location context)

    Returns:
        Diary entry text (150-250 words).
    """
    # Format date nicely
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        date_human = date_obj.strftime("%A, %d %B %Y")   # "Sunday, 03 August 2025"
    except ValueError:
        date_human = date_str

    # Location context from profile
    farms = profile.get("farms", [])
    farm_names = ", ".join(f.get("label", "") for f in farms if f.get("label"))
    homes = profile.get("homes", [])
    home_names = ", ".join(h.get("label", "") for h in homes if h.get("label"))

    # Device info (deduplicated)
    devices = list({m["device"] for m in photos if m.get("device") != "unknown device"})
    device_note = f"(shot on {devices[0]})" if devices else ""

    # Photo summary
    photo_summary = _build_photo_summary(photos, captions)

    # Time range
    times = [m["time_str"] for m in photos if m.get("time_str")]
    time_range = f"{times[0]} to {times[-1]}" if len(times) > 1 else (times[0] if times else "")

    # Cross-module context: health, farming, finance, todos
    day_ctx = _day_context(date_str)

    prompt = f"""Write a diary entry for: {date_human} {device_note}

Photos taken {time_range}:
{photo_summary}

Profile context:
- Farms: {farm_names or 'Barloni farm'}
- Homes: {home_names or 'Barloni, Pune'}
{f'''
Day context (weave naturally into the entry):
{day_ctx}''' if day_ctx else ''}

Write the diary entry now (150-250 words, first person):"""

    payload = {
        "model": TEXT_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user",   "content": prompt},
        ],
        "stream": False,
        "think":  False,
        "options": {"num_predict": 400},
    }

    try:
        resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=120.0)
        resp.raise_for_status()
        entry = resp.json()["message"]["content"].strip()
        log.info("diary entry written for %s (%d chars)", date_str, len(entry))
        return entry
    except Exception as e:
        log.error("diary LLM call failed for %s: %s", date_str, e)
        return f"[Could not generate diary entry for {date_human} — please try again.]"


def format_draft(date_str: str, entry: str, photo_count: int) -> str:
    """Wrap the raw entry with a simple header for display."""
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        header = date_obj.strftime("%A, %d %B %Y")
    except ValueError:
        header = date_str
    return f"── {header} ({photo_count} photo{'s' if photo_count != 1 else ''}) ──\n\n{entry}"
