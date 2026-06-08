"""
Diary Module — turns photos into personal diary entries.

Intent detection is keyword-based (no extra LLM call needed).
Commands:
  "write diary from my photos"              → scan default photo_dir, write entries
  "write diary from /path/to/photos"        → scan specific directory
  "diary for 2025-08-03"                    → write entry for that date only
  "show diary draft" / "show this week"     → display saved draft
  "list diary drafts"                       → list all saved weeks
  "approve diary"                           → mark current draft approved
"""

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.base_module import BaseModule, ModuleResponse
from core.memory import save_diary_draft, get_diary_draft, approve_diary_draft, list_diary_drafts
from modules.diary.photo_reader import scan_photos, default_photo_dir
from modules.diary.vision import caption_batch
from modules.diary.writer import write_diary_entry, format_draft

log = logging.getLogger(__name__)


# ── Intent detection ──────────────────────────────────────────────────────────

_WRITE_WORDS  = {"write", "create", "make", "generate", "draft", "from photos", "from my photos"}
_SHOW_WORDS   = {"show", "display", "read", "view", "get", "see"}
_LIST_WORDS   = {"list", "all drafts", "history", "entries"}
_APPROVE_WORDS= {"approve", "save", "confirm", "done", "finalize", "finalise"}

# Regex to pull a path from the query (e.g. "from /home/ganesh/Photos")
_PATH_RE   = re.compile(r"(?:from\s+)?(/[\w/._~-]+|~/[\w/._-]*)", re.IGNORECASE)
# Regex to pull a specific date "diary for 2025-08-03"
_DATE_RE   = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _intent(query: str) -> str:
    q = query.lower()
    if any(w in q for w in _APPROVE_WORDS) and "diary" in q:
        return "approve"
    if any(w in q for w in _LIST_WORDS) and "diary" in q:
        return "list"
    if any(w in q for w in _SHOW_WORDS) and "diary" in q:
        return "show"
    if any(w in q for w in _WRITE_WORDS):
        return "write"
    if "diary" in q:
        return "show"   # bare "diary" → show current draft
    return "write"


def _current_week() -> str:
    """ISO week string for today: YYYY-WNN"""
    today = datetime.now()
    return f"{today.year}-W{today.isocalendar()[1]:02d}"


def _date_to_week(date_str: str) -> str:
    """Convert YYYY-MM-DD to YYYY-WNN"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.year}-W{d.isocalendar()[1]:02d}"


# ── Action handlers ───────────────────────────────────────────────────────────

def _do_write(query: str, profile: dict) -> tuple[str, dict | None]:
    """Scan photos, caption them, write diary entries, save drafts."""
    # Detect directory from query or use profile default
    path_match = _PATH_RE.search(query)
    if path_match:
        photo_dir = Path(path_match.group(1)).expanduser()
    else:
        photo_dir = default_photo_dir(profile)

    if not photo_dir.exists():
        return (
            f"Photo directory not found: {photo_dir}\n"
            "Set a default with: preferences.photo_dir in user_profile.json\n"
            "Or specify a path: \"write diary from /home/ganesh/Photos\"",
            None,
        )

    # Check if a specific date was requested
    date_filter = None
    date_match = _DATE_RE.search(query)
    if date_match:
        date_filter = date_match.group(1)

    log.info("diary write: dir=%s date_filter=%s", photo_dir, date_filter)

    # Scan photos grouped by date
    by_date = scan_photos(photo_dir)
    if not by_date:
        return f"No photos found in {photo_dir}.", None

    # Filter to specific date if requested
    if date_filter:
        if date_filter not in by_date:
            available = ", ".join(sorted(by_date.keys()))
            return (
                f"No photos found for {date_filter}.\n"
                f"Available dates in {photo_dir.name}/: {available}",
                None,
            )
        by_date = {date_filter: by_date[date_filter]}

    # Process each day
    written_days = []
    output_lines = []

    for date_str in sorted(by_date.keys()):
        photos = by_date[date_str]
        log.info("processing %d photos for %s", len(photos), date_str)

        # Caption photos with vision model
        captions = caption_batch(photos)

        # Write diary entry
        entry = write_diary_entry(date_str, photos, captions, profile)

        # Save draft (keyed by ISO week so multiple days merge into one week draft)
        week_key = _date_to_week(date_str)
        existing = get_diary_draft(week_key)
        if existing and existing.get("draft"):
            # Append to existing week draft
            combined = existing["draft"] + "\n\n" + format_draft(date_str, entry, len(photos))
            save_diary_draft(week_key, combined)
        else:
            save_diary_draft(week_key, format_draft(date_str, entry, len(photos)))

        written_days.append(date_str)
        output_lines.append(format_draft(date_str, entry, len(photos)))

        caption_note = f" ({len(captions)} photos described by vision model)" if captions else " (no vision model — using date/time context)"
        log.info("diary draft saved for week %s%s", week_key, caption_note)

    n = len(written_days)
    header = f"Diary written for {n} day{'s' if n > 1 else ''}: {', '.join(written_days)}\n"
    header += "Draft saved. Say \"approve diary\" when you're happy with it.\n\n"

    return header + "\n\n".join(output_lines), {"days_written": written_days}


def _do_show(query: str) -> tuple[str, dict | None]:
    """Show a saved diary draft."""
    # Check for specific week
    date_match = _DATE_RE.search(query)
    if date_match:
        week = _date_to_week(date_match.group(1))
    else:
        week = _current_week()

    draft = get_diary_draft(week)
    if not draft:
        # Try the most recent draft
        all_drafts = list_diary_drafts()
        if all_drafts:
            draft = all_drafts[0]
            week  = draft["week"]
        else:
            return "No diary drafts saved yet. Say \"write diary from my photos\" to create one.", None

    status = "✓ Approved" if draft.get("approved") else "Draft (not yet approved)"
    text = f"Week {week} [{status}]\n\n{draft['draft']}"
    return text, {"week": week, "approved": draft.get("approved", False)}


def _do_list() -> tuple[str, dict | None]:
    """List all saved diary drafts."""
    drafts = list_diary_drafts()
    if not drafts:
        return "No diary drafts saved yet.", None
    lines = ["Saved diary drafts:"]
    for d in drafts:
        status = "✓" if d.get("approved") else "○"
        created = (d.get("created_at") or "")[:10]
        lines.append(f"  {status} {d['week']}  (saved {created})")
    lines.append("\nSay \"show diary [week]\" to read one.")
    return "\n".join(lines), {"count": len(drafts)}


def _do_approve(query: str) -> tuple[str, dict | None]:
    """Approve a diary draft."""
    date_match = _DATE_RE.search(query)
    if date_match:
        week = _date_to_week(date_match.group(1))
    else:
        week = _current_week()
        # Fall back to most recent unapproved
        all_drafts = list_diary_drafts(approved=False)
        if all_drafts:
            week = all_drafts[0]["week"]

    draft = get_diary_draft(week)
    if not draft:
        return f"No draft found for week {week}.", None

    approve_diary_draft(week)
    log.info("diary draft approved: week=%s", week)
    return f"Diary for week {week} approved and saved.", {"week": week}


# ── Module class ──────────────────────────────────────────────────────────────

class DiaryModule(BaseModule):
    name = "diary"
    description = (
        "Writes personal diary entries from photos. "
        "Reads EXIF date/time, captions photos with vision model, "
        "writes warm first-person diary entries and saves drafts."
    )

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        profile = context.get("profile", {})
        intent  = _intent(query)
        log.info("intent=%s q=%r", intent, query[:80])

        if intent == "write":
            text, data = _do_write(query, profile)
            follow_up = "Say \"approve diary\" when you're happy with the entry."
        elif intent == "show":
            text, data = _do_show(query)
            follow_up = "Say \"approve diary\" to mark it as final."
        elif intent == "list":
            text, data = _do_list()
            follow_up = None
        elif intent == "approve":
            text, data = _do_approve(query)
            follow_up = None
        else:
            text = "Say \"write diary from my photos\" to start, or \"show diary\" to read a saved draft."
            data = None
            follow_up = None

        return ModuleResponse(text=text, module=self.name, data=data, follow_up=follow_up)
