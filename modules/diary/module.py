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
from core.memory import (save_diary_draft, get_diary_draft, approve_diary_draft,
                         list_diary_drafts, mark_photos_processed,
                         get_processed_photo_paths, get_photo_stats,
                         add_diary_question, get_pending_questions)
from core.analysis import run_weekly_pipeline, analyse_week, current_iso_week
from modules.diary.photo_reader import scan_photos, default_photo_dir
from modules.diary.vision import caption_batch
from modules.diary.writer import write_diary_entry, format_draft

log = logging.getLogger(__name__)


# ── Intent detection ──────────────────────────────────────────────────────────

_WRITE_WORDS   = {"write", "create", "make", "generate", "draft", "from photos", "from my photos"}
_SHOW_WORDS    = {"show", "display", "read", "view", "get", "see"}
_LIST_WORDS    = {"list", "all drafts", "history", "entries"}
_APPROVE_WORDS = {"approve", "save", "confirm", "done", "finalize", "finalise"}
_WEEKLY_WORDS  = {"this week", "weekly summary", "what did i do", "week summary",
                  "week in review", "auto draft", "from queries", "from history"}

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
    if any(w in q for w in _WEEKLY_WORDS):
        return "weekly"
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

# Questions the diary module asks about photos to enrich entries
_QUESTION_TEMPLATES = [
    ("unknown_faces",  "Who is in this photo? (names, relationship to you)"),
    ("unknown_place",  "Where was this photo taken? (location, occasion)"),
    ("no_gps",         "What was happening at this moment? Tell me more about this photo."),
    ("many_people",    "Who are all the people in this photo?"),
    ("event",          "What was this event/occasion?"),
]


def _ask_photo_questions(photos: list, captions: dict, week: str) -> None:
    """Queue review questions for photos that lack context."""
    from modules.diary.vision import _known_faces_in_photo
    asked = set()
    for photo in photos[:5]:  # limit to 5 questions per diary write to avoid spam
        path = photo.get("path", str(photo)) if isinstance(photo, dict) else str(getattr(photo, "path", photo))
        caption = captions.get(photo.get("filename", "") if isinstance(photo, dict) else path, "") if captions else ""
        has_gps = (photo.get("has_gps") if isinstance(photo, dict) else bool(getattr(photo, "gps", None)))

        # Ask about location if no GPS and caption doesn't mention a place
        if not has_gps and "location" not in asked:
            q_type = "no_gps" if not caption else "unknown_place"
            q = next((t for k, t in _QUESTION_TEMPLATES if k == q_type), None)
            if q:
                add_diary_question(week, q, path)
                asked.add("location")

        # Ask about people only if faces aren't already identified
        if caption and any(w in caption.lower() for w in ("person", "people", "man", "woman", "child", "group")) and "people" not in asked:
            known = _known_faces_in_photo(path)
            if not known:  # skip if face clusters already named
                q = next((t for k, t in _QUESTION_TEMPLATES if k == "many_people"), None)
                if q:
                    add_diary_question(week, q, path)
                    asked.add("people")


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

    # Load set of already-processed photo paths
    already_processed = get_processed_photo_paths()

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

    # Count totals for reporting
    total_in_dir = sum(len(v) for v in by_date.values())
    skipped_count = 0

    # Filter out already-processed photos unless a specific date was forced
    if not date_filter:
        filtered = {}
        for d, photos in by_date.items():
            new_photos = [p for p in photos if str(getattr(p, "path", p)) not in already_processed]
            if new_photos:
                filtered[d] = new_photos
            skipped_count += len(photos) - len(new_photos)
        by_date = filtered

    if not by_date:
        stats = get_photo_stats()
        return (
            f"All {total_in_dir} photos in {photo_dir.name}/ have already been processed "
            f"({stats['total_processed']} total across {len(stats['by_week'])} weeks).\n"
            f"To reprocess a specific date, say: \"diary for YYYY-MM-DD\"",
            None,
        )

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

        # Skip saving if LLM failed — leave photos unprocessed so retry works
        if entry.startswith("[Could not generate"):
            log.warning("diary LLM failed for %s — photos NOT marked processed, will retry", date_str)
            output_lines.append(f"⚠ {date_str}: generation failed — Ollama may be busy. Try again later.")
            continue

        # Save draft (keyed by ISO week so multiple days merge into one week draft)
        week_key = _date_to_week(date_str)
        existing = get_diary_draft(week_key)
        if existing and existing.get("draft"):
            combined = existing["draft"] + "\n\n" + format_draft(date_str, entry, len(photos))
            save_diary_draft(week_key, combined)
        else:
            save_diary_draft(week_key, format_draft(date_str, entry, len(photos)))

        # Mark photos as processed so they won't be re-processed next time
        mark_photos_processed(photos, week_key)

        # Generate review questions for photos that need context
        _ask_photo_questions(photos, captions, week_key)

        written_days.append(date_str)
        output_lines.append(format_draft(date_str, entry, len(photos)))

        caption_note = f" ({len(captions)} photos described by vision model)" if captions else " (no vision model — using date/time context)"
        log.info("diary draft saved for week %s%s", week_key, caption_note)

    n = len(written_days)
    header = f"Diary written for {n} day{'s' if n > 1 else ''}: {', '.join(written_days)}\n"
    if skipped_count:
        header += f"({skipped_count} already-processed photo{'s' if skipped_count > 1 else ''} skipped — say \"diary for YYYY-MM-DD\" to reprocess a date)\n"
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


def _do_weekly(query: str) -> tuple[str, dict | None]:
    """Auto-draft a diary entry from this week's query history."""
    date_match = _DATE_RE.search(query)
    if date_match:
        week = _date_to_week(date_match.group(1))
    else:
        week = _current_week()

    force = "force" in query.lower() or "regenerate" in query.lower()
    log.info("weekly pipeline: week=%s force=%s", week, force)
    result = run_weekly_pipeline(week=week, force=force)

    if result.get("skipped"):
        return result["reason"], {"week": week, "skipped": True}

    ana = result.get("analysis", {})
    total = ana.get("total_queries", 0)
    by_mod = ana.get("by_module", {})
    mod_summary = ", ".join(f"{m}:{n}" for m, n in sorted(by_mod.items(), key=lambda x: -x[1])[:3])

    text = (
        f"Weekly diary for {week} auto-drafted from {total} queries"
        + (f" ({mod_summary})" if mod_summary else "")
        + ".\n\n"
        + result.get("draft", "")
    )
    return text, {"week": week, "total_queries": total, "by_module": by_mod}


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
        elif intent == "weekly":
            text, data = _do_weekly(query)
            follow_up = "Say \"approve diary\" to mark this week's draft as final."
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
