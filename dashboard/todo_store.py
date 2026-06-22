"""Persistent todo storage — SQLite, served via /api/todos endpoints."""

import sqlite3
import time
import uuid
from pathlib import Path

_DB = Path(__file__).parent.parent / "data" / "todos.db"


def _con():
    """Open the todos SQLite DB (thread-safe) with a Row factory."""
    _DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def ensure_table() -> None:
    """Create the todos table, migrate missing columns, and seed if empty."""
    with _con() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS todos (
                id          TEXT PRIMARY KEY,
                text        TEXT NOT NULL,
                quad        TEXT NOT NULL DEFAULT 'q2',
                done        INTEGER NOT NULL DEFAULT 0,
                priority    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                verifier    TEXT,           -- JSON verifier config or NULL
                verified    INTEGER DEFAULT 0,  -- 0=unchecked, 1=passed, -1=failed
                verify_note TEXT            -- last verifier result message
            )
        """)
        # Migrate older tables that lack verifier columns
        existing = {r[1] for r in con.execute("PRAGMA table_info(todos)").fetchall()}
        for col, defn in [
            ("verifier",    "TEXT"),
            ("verified",    "INTEGER DEFAULT 0"),
            ("verify_note", "TEXT"),
        ]:
            if col not in existing:
                con.execute(f"ALTER TABLE todos ADD COLUMN {col} {defn}")

        # Seed with initial tasks if table is empty
        if con.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0:
            _seed(con)


def _seed(con) -> None:
    """Insert the initial set of todos into an empty table."""
    import json as _json
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    # (text, quad, priority, verifier_dict or None)
    seeds = [
        ("Integrate GodsView AI satellite monitoring for farm plots",    "q1", 1, None),
        ("Add code directory analyzer module",                           "q1", 2, None),
        ("Set up ~/Uploads for daily photo-diary auto-generation",       "q1", 3, None),
        ("Add real-time NDVI crop health from satellite imagery",        "q2", 4, None),
        ("Voice from mobile (Tailscale URL)",                           "q2", 5, None),
        ("Add soil moisture trend chart to farming module",              "q2", 6, None),
        ("Set email credentials in .env (DIGEST_EMAIL_PASS)",           "q1", 7,
            {"type": "env_var", "key": "DIGEST_EMAIL_PASS"}),
        ("Set up SearXNG (docker run -d -p 8888:8080 searxng/searxng)", "q2", 8,
            {"type": "env_var", "key": "SEARXNG_URL"}),
        ("Optimize embedding router accuracy (add more training phrases)","q3", 9, None),
    ]
    for text, quad, pri, verifier in seeds:
        tid = str(uuid.uuid4())[:8]
        v_json = _json.dumps(verifier) if verifier else None
        con.execute(
            "INSERT INTO todos (id,text,quad,done,priority,created_at,updated_at,verifier)"
            " VALUES (?,?,?,0,?,?,?,?)",
            (tid, text, quad, pri, now, now, v_json)
        )


def list_todos(include_done: bool = False) -> list[dict]:
    """Return todos ordered by quadrant/priority; excludes done unless include_done."""
    with _con() as con:
        q = "SELECT * FROM todos ORDER BY quad, priority, created_at"
        if not include_done:
            q = "SELECT * FROM todos WHERE done=0 ORDER BY quad, priority, created_at"
        return [dict(r) for r in con.execute(q).fetchall()]


def add_todo(text: str, quad: str = "q2", verifier: dict | None = None) -> str:
    """Insert a new todo (auto-assigned priority) and return its id."""
    import json as _json
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    tid = str(uuid.uuid4())[:8]
    with _con() as con:
        pri = (con.execute("SELECT MAX(priority) FROM todos").fetchone()[0] or 0) + 1
        con.execute(
            "INSERT INTO todos (id,text,quad,done,priority,created_at,updated_at,verifier)"
            " VALUES (?,?,?,0,?,?,?,?)",
            (tid, text, quad, pri, now, now, _json.dumps(verifier) if verifier else None)
        )
    return tid


def get_todo(tid: str) -> dict | None:
    """Return the todo with the given id, or None."""
    with _con() as con:
        row = con.execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()
    return dict(row) if row else None


def update_todo(tid: str, **kwargs) -> None:
    """Update allow-listed fields of a todo and bump updated_at."""
    allowed = {"text", "quad", "done", "priority", "verifier", "verified", "verify_note"}
    sets, vals = [], []
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(int(v) if k in ("done", "priority", "verified") else v)
    if not sets:
        return
    vals += [time.strftime("%Y-%m-%dT%H:%M:%S"), tid]
    with _con() as con:
        con.execute(f"UPDATE todos SET {', '.join(sets)}, updated_at=? WHERE id=?", vals)  # nosec B608 — column names from allow-list, not user input


def delete_todo(tid: str) -> None:
    """Delete the todo with the given id."""
    with _con() as con:
        con.execute("DELETE FROM todos WHERE id=?", (tid,))
