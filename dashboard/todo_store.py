"""Persistent todo storage — SQLite, served via /api/todos endpoints."""

import sqlite3
import time
import uuid
from pathlib import Path

_DB = Path(__file__).parent.parent / "data" / "todos.db"


def _con():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB), check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def ensure_table() -> None:
    with _con() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS todos (
                id          TEXT PRIMARY KEY,
                text        TEXT NOT NULL,
                quad        TEXT NOT NULL DEFAULT 'q2',
                done        INTEGER NOT NULL DEFAULT 0,
                priority    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        # Seed with initial tasks if table is empty
        if con.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0:
            _seed(con)


def _seed(con) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    seeds = [
        ("Integrate GodsView AI satellite monitoring for farm plots",   "q1", 1),
        ("Add code directory analyzer module",                          "q1", 2),
        ("Set up ~/Uploads for daily photo-diary auto-generation",      "q1", 3),
        ("Add real-time NDVI crop health from satellite imagery",       "q2", 4),
        ("Voice from mobile (Tailscale URL)",                          "q2", 5),
        ("Add soil moisture trend chart to farming module",             "q2", 6),
        ("Weekly email digest of diary + finance summary",              "q3", 7),
        ("Optimize embedding router accuracy (add more training phrases)","q3", 8),
    ]
    for text, quad, pri in seeds:
        tid = str(uuid.uuid4())[:8]
        con.execute(
            "INSERT INTO todos VALUES (?,?,?,0,?,?,?)",
            (tid, text, quad, pri, now, now)
        )


def list_todos(include_done: bool = False) -> list[dict]:
    with _con() as con:
        q = "SELECT * FROM todos ORDER BY quad, priority, created_at"
        if not include_done:
            q = "SELECT * FROM todos WHERE done=0 ORDER BY quad, priority, created_at"
        return [dict(r) for r in con.execute(q).fetchall()]


def add_todo(text: str, quad: str = "q2") -> str:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    tid = str(uuid.uuid4())[:8]
    with _con() as con:
        pri = (con.execute("SELECT MAX(priority) FROM todos").fetchone()[0] or 0) + 1
        con.execute(
            "INSERT INTO todos (id,text,quad,done,priority,created_at,updated_at) VALUES (?,?,?,0,?,?,?)",
            (tid, text, quad, pri, now, now)
        )
    return tid


def update_todo(tid: str, **kwargs) -> None:
    allowed = {"text", "quad", "done", "priority"}
    sets, vals = [], []
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(int(v) if k in ("done", "priority") else v)
    if not sets:
        return
    vals += [time.strftime("%Y-%m-%dT%H:%M:%S"), tid]
    with _con() as con:
        con.execute(f"UPDATE todos SET {', '.join(sets)}, updated_at=? WHERE id=?", vals)


def delete_todo(tid: str) -> None:
    with _con() as con:
        con.execute("DELETE FROM todos WHERE id=?", (tid,))
