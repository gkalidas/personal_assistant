import hashlib
import json
import os
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any


GK_DB = os.getenv("GK_DB", "personal_assistant.db")
PROFILE_PATH = Path("user_profile.json")


# ── User Profile (fast cache) ─────────────────────────────────────────────────

def load_profile() -> dict[str, Any]:
    """Load the user profile dict from user_profile.json (empty dict if absent)."""
    if PROFILE_PATH.exists():
        return json.loads(PROFILE_PATH.read_text())
    return {}


def save_profile(profile: dict[str, Any]) -> None:
    """Write the user profile dict to user_profile.json."""
    PROFILE_PATH.write_text(json.dumps(profile, indent=2, ensure_ascii=False))


def update_profile(key: str, value: Any) -> None:
    """Set one key in the user profile and persist it."""
    profile = load_profile()
    profile[key] = value
    save_profile(profile)


# ── Personal preferences (learned likes: music, food, colour, …) ──────────────
# Stored under a "personal" block in the profile so they sit alongside the user's
# other personal information and stay human-readable/editable. Each entry is
# {value, source, updated} where source ∈ {"stated","asked","chat"} records how we
# learned it (volunteered in chat / answered when asked / recovered from history).

def get_personal_prefs() -> dict[str, Any]:
    """Return all learned personal preferences (empty dict if none)."""
    return load_profile().get("personal", {}) or {}


def get_personal_pref(category: str) -> dict | None:
    """Return one preference entry {value, source, updated}, or None if unknown."""
    return get_personal_prefs().get(category.strip().lower())


def set_personal_pref(category: str, value: str, source: str = "stated") -> None:
    """Store/overwrite a personal preference and persist it to the profile."""
    profile = load_profile()
    personal = profile.get("personal") or {}
    personal[category.strip().lower()] = {
        "value": value.strip(),
        "source": source,
        "updated": datetime.now().isoformat(),
    }
    profile["personal"] = personal
    save_profile(profile)


# ── Learned content lists (pre-fetched "top X" for a known taste) ─────────────
# When the idle scan discovers a content preference (e.g. music=ghazals), the
# personal module pre-fetches a "top ghazals" list and caches it here so a later
# "list of top ghazals" is served instantly instead of hitting the web. Stored per
# category under a "personal_lists" block: {items, query, source, updated}.

def get_personal_list(category: str) -> dict | None:
    """Return the cached list for a category {items, query, source, updated}, or None."""
    return (load_profile().get("personal_lists") or {}).get(category.strip().lower())


def set_personal_list(category: str, items: list[str],
                      query: str = "", source: str = "prefetch") -> None:
    """Store/overwrite a pre-fetched content list for a category and persist it."""
    profile = load_profile()
    lists = profile.get("personal_lists") or {}
    lists[category.strip().lower()] = {
        "items": items,
        "query": query,
        "source": source,
        "updated": datetime.now().isoformat(),
    }
    profile["personal_lists"] = lists
    save_profile(profile)


# ── Chat scan cursor (idle preference mining) ─────────────────────────────────
# The idle scan only reads events newer than this id, so each run does bounded
# work (usually none). Persisted in the profile so it survives daemon restarts.

def get_personal_scan_cursor() -> int:
    """Return the highest event id the idle preference scan has already processed."""
    try:
        return int(load_profile().get("_personal_scan_cursor") or 0)
    except (TypeError, ValueError):
        return 0


def set_personal_scan_cursor(event_id: int) -> None:
    """Advance the idle-scan cursor to the newest event id it just processed."""
    update_profile("_personal_scan_cursor", int(event_id))


# ── Pending personal question (multi-turn "ask then capture the reply") ────────
# When GK asks the user about an unknown preference, it records the category here
# so the next message (often a bare answer like "jazz") is captured as the answer
# instead of being routed as a fresh query. Kept in the profile so it survives a
# process restart between the question and the reply.

def set_pending_personal(category: str) -> None:
    """Record that GK is waiting for the user to answer about `category`."""
    update_profile("_pending_personal", category.strip().lower())


def get_pending_personal() -> str | None:
    """Return the category GK is awaiting an answer for, or None."""
    return load_profile().get("_pending_personal") or None


def clear_pending_personal() -> None:
    """Clear the pending personal question flag."""
    profile = load_profile()
    if profile.pop("_pending_personal", None) is not None:
        save_profile(profile)


# ── Events DB (ground truth) ──────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    """Open the GK events SQLite DB with a Row factory."""
    conn = sqlite3.connect(GK_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create all tables and run lightweight column migrations (safe to call repeatedly)."""
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL,
                module       TEXT NOT NULL,
                query        TEXT NOT NULL,
                response     TEXT NOT NULL DEFAULT '',
                latency_ms   INTEGER,
                status       TEXT NOT NULL DEFAULT 'ok' CHECK(status IN ('ok','error','dropped')),
                metadata     TEXT
            );

            CREATE TABLE IF NOT EXISTS diary_drafts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                week         TEXT NOT NULL UNIQUE,
                draft        TEXT NOT NULL,
                approved     INTEGER DEFAULT 0,
                created_at   TEXT NOT NULL,
                approved_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS processed_photos (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                path         TEXT NOT NULL UNIQUE,
                sha256       TEXT,
                size_bytes   INTEGER,
                date_taken   TEXT,
                week         TEXT,
                processed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS diary_questions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                week         TEXT NOT NULL,
                photo_path   TEXT,
                question     TEXT NOT NULL,
                answer       TEXT,
                asked_at     TEXT NOT NULL,
                answered_at  TEXT
            );
        """)
        _migrate_events(conn)
        _migrate_processed_photos(conn)


def _migrate_processed_photos(conn: sqlite3.Connection) -> None:
    """Add the content-hash column to older processed_photos tables."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(processed_photos)")}
    if "sha256" not in existing:
        conn.execute("ALTER TABLE processed_photos ADD COLUMN sha256 TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_sha ON processed_photos(sha256)")


def _migrate_events(conn: sqlite3.Connection) -> None:
    """Add columns to the events table that may be missing in older DBs."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "latency_ms" not in existing:
        conn.execute("ALTER TABLE events ADD COLUMN latency_ms INTEGER")
    if "status" not in existing:
        conn.execute(
            "ALTER TABLE events ADD COLUMN status TEXT NOT NULL DEFAULT 'ok' "
            "CHECK(status IN ('ok','error','dropped'))"
        )


def log_query_start(query: str, module: str = "router") -> int:
    """Log a query before processing. Returns row id for updating after."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (ts, module, query, response, status) VALUES (?,?,?,'','ok')",
            (datetime.now().isoformat(), module, query),
        )
        return cur.lastrowid


def log_query_done(event_id: int, module: str, response: str,
                   latency_ms: int, status: str = "ok", metadata: dict | None = None) -> None:
    """Update an event row after processing completes."""
    with _conn() as conn:
        conn.execute(
            "UPDATE events SET module=?, response=?, latency_ms=?, status=?, metadata=? WHERE id=?",
            (module, response, latency_ms, status,
             json.dumps(metadata) if metadata else None, event_id),
        )


def log_event(module: str, query: str, response: str,
              latency_ms: int | None = None, metadata: dict | None = None,
              status: str = "ok") -> None:
    """Single-call log for cases where start/done split isn't needed.

    Pass status='error' for background failures (e.g. a failed captioning run)
    so the anomaly analyser, which counts status='error' rows, can see them.
    """
    with _conn() as conn:
        conn.execute(
            "INSERT INTO events (ts, module, query, response, latency_ms, metadata, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(), module, query, response, latency_ms,
             json.dumps(metadata) if metadata else None, status),
        )


def recent_events(module: str | None = None, limit: int = 10) -> list[dict]:
    """Return the most recent events (optionally filtered by module), newest first."""
    with _conn() as conn:
        if module:
            rows = conn.execute(
                "SELECT * FROM events WHERE module=? ORDER BY ts DESC LIMIT ?",
                (module, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def events_since_id(last_id: int, limit: int = 500) -> list[dict]:
    """Return chat events with id greater than last_id (oldest first), capped at limit.

    Used by the idle personal scan to process only messages it hasn't seen yet, so a
    run with no new chats is a cheap no-op.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def events_for_week(week: str) -> list[dict]:
    """Return all events in an ISO week. week format: YYYY-WNN (e.g. 2026-W23).

    Filters by the Mon..Mon date range of the ISO week so it matches the same
    weeks that core.analysis.current_iso_week() produces. SQLite's
    strftime('%W') numbers weeks differently (Monday-of-year based, off by one
    from ISO and wrong across year boundaries), so it cannot be used here.
    """
    year, w = week.split("-W")
    start = date.fromisocalendar(int(year), int(w), 1)   # Monday of the ISO week
    end   = start + timedelta(days=7)                     # exclusive (next Monday)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Diary drafts ──────────────────────────────────────────────────────────────

def save_diary_draft(week: str, draft: str) -> None:
    """Insert or replace the diary draft for a week.

    If the draft content actually changes, the week's approval is reset
    (approved=0, approved_at cleared) — so content added after approval (e.g. a
    new photo day appended to an already-approved week) re-opens it for review
    instead of silently shipping unreviewed text in the digest.
    """
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO diary_drafts (week, draft, created_at) VALUES (?,?,?)
               ON CONFLICT(week) DO UPDATE SET
                   draft=excluded.draft,
                   created_at=excluded.created_at,
                   approved=CASE WHEN diary_drafts.draft <> excluded.draft
                                 THEN 0 ELSE diary_drafts.approved END,
                   approved_at=CASE WHEN diary_drafts.draft <> excluded.draft
                                    THEN NULL ELSE diary_drafts.approved_at END""",
            (week, draft, now),
        )


def get_diary_draft(week: str) -> dict | None:
    """Return the diary draft row for a week, or None."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM diary_drafts WHERE week=?", (week,)).fetchone()
    return dict(row) if row else None


def approve_diary_draft(week: str) -> None:
    """Mark the diary draft for a week as approved."""
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE diary_drafts SET approved=1, approved_at=? WHERE week=?", (now, week)
        )


def list_diary_drafts(approved: bool | None = None) -> list[dict]:
    """List diary drafts, optionally filtered by approval status, newest week first."""
    with _conn() as conn:
        if approved is None:
            rows = conn.execute("SELECT * FROM diary_drafts ORDER BY week DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM diary_drafts WHERE approved=? ORDER BY week DESC", (1 if approved else 0,)
            ).fetchall()
    return [dict(r) for r in rows]


# ── Photo processing tracker ───────────────────────────────────────────────────

def file_content_hash(path: str) -> str | None:
    """Return a hex content hash (blake2b) of a file, or None if unreadable.

    Used to dedup photos by content so a moved/renamed/re-downloaded copy of an
    already-captioned image is not captioned again.
    """
    try:
        h = hashlib.blake2b(digest_size=16)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):  # 1 MiB chunks
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def mark_photos_processed(photos: list, week: str) -> None:
    """Record which photo files have been processed into diary entries.

    Stores a content hash (sha256 column, blake2b value) alongside the path so
    future scans skip the same content even under a different filename. Reuses a
    precomputed ``sha256`` from the photo meta dict when present (avoids re-hashing).
    """
    now = datetime.now().isoformat()
    with _conn() as conn:
        for p in photos:
            # photos are dicts from read_exif() — use dict key, not str(dict)
            if isinstance(p, dict):
                path = p.get("path") or p.get("filename") or str(p)
                date_taken = p.get("date")
                sha = p.get("sha256")
            else:
                path = str(getattr(p, "path", p))
                date_taken = getattr(p, "date", None)
                sha = None
            if not sha:
                sha = file_content_hash(path)
            size = None
            try:
                size = os.path.getsize(path)
            except Exception:
                pass
            conn.execute(
                """INSERT OR IGNORE INTO processed_photos
                   (path, sha256, size_bytes, date_taken, week, processed_at)
                   VALUES (?,?,?,?,?,?)""",
                (path, sha, size, str(date_taken) if date_taken else None, week, now),
            )


def record_photo_alias(path: str, sha256: str, week: str = "alias") -> None:
    """Record a new path for content already processed (a moved/renamed duplicate).

    Lets future scans fast-path the file by path instead of re-hashing it each run.
    """
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO processed_photos
               (path, sha256, week, processed_at) VALUES (?,?,?,?)""",
            (path, sha256, week, now),
        )


def get_processed_photo_paths() -> set[str]:
    """Return the set of all photo file paths already processed."""
    with _conn() as conn:
        rows = conn.execute("SELECT path FROM processed_photos").fetchall()
    return {row["path"] for row in rows}


def get_processed_photo_hashes() -> set[str]:
    """Return the set of all photo content hashes already processed."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT sha256 FROM processed_photos WHERE sha256 IS NOT NULL"
        ).fetchall()
    return {row["sha256"] for row in rows}


def get_photo_stats(photo_dir: str | None = None) -> dict:
    """Return summary: total processed, by week, latest date."""
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM processed_photos").fetchone()[0]
        weeks = conn.execute(
            "SELECT week, COUNT(*) as cnt FROM processed_photos GROUP BY week ORDER BY week DESC"
        ).fetchall()
        latest = conn.execute(
            "SELECT processed_at FROM processed_photos ORDER BY processed_at DESC LIMIT 1"
        ).fetchone()
    return {
        "total_processed": total,
        "by_week": [{"week": r["week"], "count": r["cnt"]} for r in weeks],
        "last_processed_at": latest["processed_at"] if latest else None,
    }


# ── Diary questions (photo review) ────────────────────────────────────────────

def add_diary_question(week: str, question: str, photo_path: str | None = None) -> int:
    """Queue a question about a photo/week for the user to answer.

    Skips insertion if an identical unanswered question already exists for the
    same week+photo, so repeated diary writes don't pile up duplicate prompts.
    """
    now = datetime.now().isoformat()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM diary_questions "
            "WHERE answer IS NULL AND week=? AND question=? "
            "AND ((photo_path IS NULL AND ? IS NULL) OR photo_path=?)",
            (week, question, photo_path, photo_path),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO diary_questions (week, photo_path, question, asked_at) VALUES (?,?,?,?)",
            (week, photo_path, question, now),
        )
        return cur.lastrowid


def _extract_names(text: str) -> list[str]:
    """Pull proper nouns from a free-text answer. Returns [] if none found."""
    import re
    # Match capitalized words of 3+ chars; skip common filler words
    _SKIP = {"That", "This", "The", "Who", "What", "Yes", "No", "She", "He",
             "His", "Her", "They", "Their", "My", "Our", "Its", "And", "But"}
    candidates = re.findall(r'\b[A-Z][a-z]{2,}\b', text)
    return [c for c in candidates if c not in _SKIP]


def _auto_label_faces_from_answer(photo_path: str, answer: str) -> None:
    """When user answers 'Who is in this photo?', label matching face clusters."""
    try:
        from modules.faces.db import faces_for_photo, rename_cluster
        faces = faces_for_photo(photo_path)
        cluster_ids = list({f["cluster_id"] for f in faces
                            if f.get("cluster_id") is not None})
        if not cluster_ids:
            return
        names = _extract_names(answer)
        if not names:
            return
        # Assign the first extracted name to the first (largest) cluster in the photo
        for i, cid in enumerate(cluster_ids):
            label = names[i] if i < len(names) else names[0]
            rename_cluster(cid, label)
    except Exception:
        pass


def answer_diary_question(qid: int, answer: str) -> None:
    """Record the user's answer to a diary question and auto-label face clusters."""
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE diary_questions SET answer=?, answered_at=? WHERE id=?",
            (answer, now, qid),
        )
        row = conn.execute(
            "SELECT photo_path, question FROM diary_questions WHERE id=?", (qid,)
        ).fetchone()
    if row and row["photo_path"] and "who" in (row["question"] or "").lower():
        _auto_label_faces_from_answer(row["photo_path"], answer)


def get_pending_questions(week: str | None = None, limit: int = 10) -> list[dict]:
    """Return unanswered diary questions, optionally filtered by week."""
    with _conn() as conn:
        if week:
            rows = conn.execute(
                "SELECT * FROM diary_questions WHERE answer IS NULL AND week=? ORDER BY asked_at DESC LIMIT ?",
                (week, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM diary_questions WHERE answer IS NULL ORDER BY asked_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_answered_questions(week: str) -> list[dict]:
    """Return answered questions for a given week (used when regenerating diary entry)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM diary_questions WHERE answer IS NOT NULL AND week=? ORDER BY asked_at",
            (week,),
        ).fetchall()
    return [dict(r) for r in rows]
