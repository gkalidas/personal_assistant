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

log = logging.getLogger(__name__)


_SYSTEM = """You are writing a personal diary for Ganesh Kalidas, a farmer and entrepreneur
from Barloni village, Solapur district, Maharashtra, India.

Write in first person, warm and honest. Ganesh is down-to-earth — not flowery.
He has pomegranate and sugarcane farms, a family, and splits time between the farm (Barloni)
and city (Pune/Bavdhan). He uses his iPhone to take photos.

Rules:
- Ground the entry in what's in the photos and their timestamps — don't invent events
- If you see farming activity, mention it naturally
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

    prompt = f"""Write a diary entry for: {date_human} {device_note}

Photos taken {time_range}:
{photo_summary}

Profile context:
- Farms: {farm_names or 'Barloni farm'}
- Homes: {home_names or 'Barloni, Pune'}

Write the diary entry now (150-250 words, first person, grounded only in what's described above):"""

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
