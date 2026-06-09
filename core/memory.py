import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


GK_DB = os.getenv("GK_DB", "personal_assistant.db")
PROFILE_PATH = Path("user_profile.json")


# ── User Profile (fast cache) ─────────────────────────────────────────────────

def load_profile() -> dict[str, Any]:
    if PROFILE_PATH.exists():
        return json.loads(PROFILE_PATH.read_text())
    return {}


def save_profile(profile: dict[str, Any]) -> None:
    PROFILE_PATH.write_text(json.dumps(profile, indent=2, ensure_ascii=False))


def update_profile(key: str, value: Any) -> None:
    profile = load_profile()
    profile[key] = value
    save_profile(profile)


# ── Events DB (ground truth) ──────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(GK_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
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
                size_bytes   INTEGER,
                date_taken   TEXT,
                week         TEXT,
                processed_at TEXT NOT NULL
            );
        """)
        # Schema migrations: add columns that may be missing in older DBs
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
              latency_ms: int | None = None, metadata: dict | None = None) -> None:
    """Single-call log for cases where start/done split isn't needed."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO events (ts, module, query, response, latency_ms, metadata) VALUES (?,?,?,?,?,?)",
            (datetime.now().isoformat(), module, query, response, latency_ms,
             json.dumps(metadata) if metadata else None),
        )


def recent_events(module: str | None = None, limit: int = 10) -> list[dict]:
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


def events_for_week(week: str) -> list[dict]:
    """week format: YYYY-WNN (ISO week, e.g. 2026-W23)"""
    year, w = week.split("-W")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE strftime('%Y-W%W', ts) = ? ORDER BY ts",
            (f"{year}-W{w.zfill(2)}",),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Diary drafts ──────────────────────────────────────────────────────────────

def save_diary_draft(week: str, draft: str) -> None:
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO diary_drafts (week, draft, created_at) VALUES (?,?,?)
               ON CONFLICT(week) DO UPDATE SET draft=excluded.draft, created_at=excluded.created_at""",
            (week, draft, now),
        )


def get_diary_draft(week: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM diary_drafts WHERE week=?", (week,)).fetchone()
    return dict(row) if row else None


def approve_diary_draft(week: str) -> None:
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE diary_drafts SET approved=1, approved_at=? WHERE week=?", (now, week)
        )


def list_diary_drafts(approved: bool | None = None) -> list[dict]:
    with _conn() as conn:
        if approved is None:
            rows = conn.execute("SELECT * FROM diary_drafts ORDER BY week DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM diary_drafts WHERE approved=? ORDER BY week DESC", (1 if approved else 0,)
            ).fetchall()
    return [dict(r) for r in rows]


# ── Photo processing tracker ───────────────────────────────────────────────────

def mark_photos_processed(photos: list, week: str) -> None:
    """Record which photo files have been processed into diary entries."""
    now = datetime.now().isoformat()
    with _conn() as conn:
        for p in photos:
            path = str(getattr(p, "path", p))
            size = None
            date_taken = getattr(p, "date", None)
            try:
                import os
                size = os.path.getsize(path)
            except Exception:
                pass
            conn.execute(
                """INSERT OR IGNORE INTO processed_photos
                   (path, size_bytes, date_taken, week, processed_at) VALUES (?,?,?,?,?)""",
                (path, size, str(date_taken) if date_taken else None, week, now),
            )


def get_processed_photo_paths() -> set[str]:
    """Return the set of all photo file paths already processed."""
    with _conn() as conn:
        rows = conn.execute("SELECT path FROM processed_photos").fetchall()
    return {row["path"] for row in rows}


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
