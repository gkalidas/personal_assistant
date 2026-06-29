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
                         get_processed_photo_paths, get_processed_photo_hashes,
                         get_photo_stats, file_content_hash, record_photo_alias,
                         add_diary_question, get_pending_questions, load_profile)
from core.analysis import run_weekly_pipeline, analyse_week, current_iso_week
from modules.diary.photo_reader import scan_photos, default_photo_dir, cap_per_day
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
    """Classify a diary query into an intent (write/show/list/weekly/approve)."""
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


def _resolve_photo_dir(query: str, profile: dict) -> Path:
    """Return the photo directory named in the query, else the profile default."""
    m = _PATH_RE.search(query)
    return Path(m.group(1)).expanduser() if m else default_photo_dir(profile)


def _filter_unprocessed(by_date: dict[str, list]) -> tuple[dict, int]:
    """Drop already-processed photos, by path first then by content hash.

    A photo whose content hash was already processed under a different path
    (moved/renamed/re-downloaded copy) is recorded as an alias and skipped, so
    the expensive captioning never re-runs on identical content. The freshly
    computed hash is cached on each kept photo (``sha256``) so marking it later
    doesn't re-read the file. Returns (filtered_by_date, skipped_count).
    """
    already = get_processed_photo_paths()
    hashes  = get_processed_photo_hashes()
    filtered: dict[str, list] = {}
    skipped = 0
    for day, photos in by_date.items():
        new_photos = []
        for p in photos:
            path = p.get("path")
            if path in already:
                skipped += 1
                continue
            sha = file_content_hash(path)
            p["sha256"] = sha          # cache so mark_photos_processed won't re-hash
            if sha and sha in hashes:   # same content, different path → alias, don't recaption
                record_photo_alias(path, sha)
                skipped += 1
                continue
            new_photos.append(p)
        if new_photos:
            filtered[day] = new_photos
    return filtered, skipped


def _process_diary_day(date_str: str, photos: list, profile: dict) -> tuple[str, bool]:
    """Caption + write + save a diary draft for one day. Returns (output_line, written)."""
    log.info("processing %d photos for %s", len(photos), date_str)
    captions = caption_batch(photos)
    entry = write_diary_entry(date_str, photos, captions, profile)
    if entry.startswith("[Could not generate"):
        log.warning("diary LLM failed for %s — photos NOT marked processed, will retry", date_str)
        return f"⚠ {date_str}: generation failed — Ollama may be busy. Try again later.", False

    week_key = _date_to_week(date_str)
    draft = format_draft(date_str, entry, len(photos))
    existing = get_diary_draft(week_key)
    save_diary_draft(week_key, (existing["draft"] + "\n\n" + draft)
                     if existing and existing.get("draft") else draft)
    mark_photos_processed(photos, week_key)
    _ask_photo_questions(photos, captions, week_key)
    log.info("diary draft saved for week %s (%d captioned)", week_key, len(captions))
    return draft, True


def _do_write(query: str, profile: dict) -> tuple[str, dict | None]:
    """Scan photos, caption them, write diary entries, and save weekly drafts."""
    photo_dir = _resolve_photo_dir(query, profile)
    if not photo_dir.exists():
        return (f"Photo directory not found: {photo_dir}\n"
                "Set a default with: preferences.photo_dir in user_profile.json\n"
                "Or specify a path: \"write diary from /home/ganesh/Photos\"", None)

    date_match  = _DATE_RE.search(query)
    date_filter = date_match.group(1) if date_match else None
    log.info("diary write: dir=%s date_filter=%s", photo_dir, date_filter)

    # Scan WITHOUT the per-day cap so we can cap *after* dropping already-processed
    # photos — otherwise days with >cap photos orphan the rest forever.
    by_date = scan_photos(photo_dir, max_per_day=None)
    if not by_date:
        return f"No photos found in {photo_dir}.", None
    if date_filter:
        if date_filter not in by_date:
            return (f"No photos found for {date_filter}.\n"
                    f"Available dates in {photo_dir.name}/: {', '.join(sorted(by_date))}", None)
        by_date = {date_filter: by_date[date_filter]}

    total_in_dir = sum(len(v) for v in by_date.values())
    skipped_count = 0
    if not date_filter:
        by_date, skipped_count = _filter_unprocessed(by_date)
    if not by_date:
        stats = get_photo_stats()
        return (f"All {total_in_dir} photos in {photo_dir.name}/ have already been processed "
                f"({stats['total_processed']} total across {len(stats['by_week'])} weeks).\n"
                f"To reprocess a specific date, say: \"diary for YYYY-MM-DD\"", None)

    # Cap AFTER filtering: this cycle captions the first N unprocessed per day;
    # the remainder are picked up on the next idle cycle until the day is done.
    by_date = cap_per_day(by_date, 12)

    written_days, output_lines = [], []
    for date_str in sorted(by_date.keys()):
        line, written = _process_diary_day(date_str, by_date[date_str], profile)
        output_lines.append(line)
        if written:
            written_days.append(date_str)

    n = len(written_days)
    header = f"Diary written for {n} day{'s' if n > 1 else ''}: {', '.join(written_days)}\n"
    if skipped_count:
        header += (f"({skipped_count} already-processed photo{'s' if skipped_count > 1 else ''} "
                   "skipped — say \"diary for YYYY-MM-DD\" to reprocess a date)\n")
    header += "Draft saved. Say \"approve diary\" when you're happy with it.\n\n"
    return header + "\n\n".join(output_lines), {"days_written": written_days}


_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".heif"}


def caption_pending_photos() -> dict:
    """Scan ~/Uploads and ~/Pictures for unprocessed photos and write diary drafts.

    Dashboard-independent entry point used by the guardian's idle scheduler so the
    heavy vision work (moondream + qwen3, ~1 min/photo) runs only when the system
    is idle. A cheap no-op (one directory scan) when there are no new photos.

    Returns: {"new_photos": int, "folders": [..], "days_written": [..]}
    """
    profile = load_profile()
    already = get_processed_photo_paths()
    total_new, folders, days_written = 0, [], []
    for folder in ("Uploads", "Pictures"):
        d = Path.home() / folder
        if not d.exists():
            continue
        new = [p for p in d.rglob("*")
               if p.suffix.lower() in _PHOTO_EXTS and str(p) not in already]
        if not new:
            continue
        total_new += len(new)
        folders.append(folder)
        log.info("diary: %d new photo(s) in ~/%s — captioning", len(new), folder)
        _, data = _do_write(f"write diary from my photos {d}", profile)
        if data and data.get("days_written"):
            days_written += data["days_written"]
    return {"new_photos": total_new, "folders": folders, "days_written": days_written}


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
        """Route a diary query to its handler by intent (no LLM for routing)."""
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
