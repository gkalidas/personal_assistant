"""
Knowledge Graph — SQLite-backed nodes and edges for People, Places, Events, Media.

Node types: person, place, event, media, topic
Edge types: appeared_in, attended, located_at, tagged_in, related_to, co_appears_with

Designed for personal scale (thousands of nodes, not millions).
vis-network ready: get_graph_json() returns {nodes, edges} for the browser.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DB = Path(__file__).parent.parent / "personal_assistant.db"

# Node type → display color for vis-network
_NODE_COLORS = {
    "person": "#d2a8ff",   # purple
    "place":  "#56d364",   # green
    "event":  "#79c0ff",   # blue
    "media":  "#ffa657",   # orange
    "topic":  "#f78166",   # red
}


def _conn():
    conn = sqlite3.connect(str(_DB))
    conn.row_factory = sqlite3.Row
    return conn


def init_graph_tables():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS kg_nodes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                type       TEXT NOT NULL CHECK(type IN ('person','place','event','media','topic')),
                label      TEXT NOT NULL,
                aliases    TEXT,          -- JSON list of alternate names
                properties TEXT,          -- JSON dict of extra fields
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS kg_nodes_label ON kg_nodes(type, label);

            CREATE TABLE IF NOT EXISTS kg_edges (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                src_id      INTEGER NOT NULL REFERENCES kg_nodes(id),
                dst_id      INTEGER NOT NULL REFERENCES kg_nodes(id),
                rel         TEXT NOT NULL,   -- appeared_in, attended, located_at, etc.
                weight      REAL DEFAULT 1.0,
                properties  TEXT,            -- JSON
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS kg_edges_src ON kg_edges(src_id);
            CREATE INDEX IF NOT EXISTS kg_edges_dst ON kg_edges(dst_id);
        """)


# ── Node CRUD ─────────────────────────────────────────────────────────────────

def upsert_node(type_: str, label: str, properties: dict | None = None,
                aliases: list[str] | None = None) -> int:
    """Create or update a node. Returns node id."""
    now = datetime.now().isoformat()
    props_json = json.dumps(properties or {})
    alias_json = json.dumps(aliases or [])
    with _conn() as c:
        existing = c.execute(
            "SELECT id FROM kg_nodes WHERE type=? AND label=?", (type_, label)
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE kg_nodes SET properties=?, aliases=?, updated_at=? WHERE id=?",
                (props_json, alias_json, now, existing["id"]),
            )
            return existing["id"]
        cur = c.execute(
            "INSERT INTO kg_nodes (type, label, aliases, properties, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (type_, label, alias_json, props_json, now, now),
        )
        return cur.lastrowid


def get_node(node_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM kg_nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["properties"] = json.loads(d.get("properties") or "{}")
    d["aliases"]    = json.loads(d.get("aliases")    or "[]")
    return d


def find_node(label: str, type_: str | None = None) -> dict | None:
    """Find node by label (or alias match)."""
    with _conn() as c:
        if type_:
            row = c.execute(
                "SELECT * FROM kg_nodes WHERE label=? AND type=?", (label, type_)
            ).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM kg_nodes WHERE label=?", (label,)
            ).fetchone()
        if not row:
            # Try alias search
            rows = c.execute("SELECT * FROM kg_nodes").fetchall()
            for r in rows:
                aliases = json.loads(r["aliases"] or "[]")
                if label.lower() in [a.lower() for a in aliases]:
                    row = r
                    break
    if not row:
        return None
    d = dict(row)
    d["properties"] = json.loads(d.get("properties") or "{}")
    d["aliases"]    = json.loads(d.get("aliases")    or "[]")
    return d


def list_nodes(type_: str | None = None, limit: int = 100) -> list[dict]:
    with _conn() as c:
        if type_:
            rows = c.execute(
                "SELECT * FROM kg_nodes WHERE type=? ORDER BY label LIMIT ?", (type_, limit)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM kg_nodes ORDER BY type, label LIMIT ?", (limit,)
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["properties"] = json.loads(d.get("properties") or "{}")
        d["aliases"]    = json.loads(d.get("aliases")    or "[]")
        result.append(d)
    return result


# ── Edge CRUD ─────────────────────────────────────────────────────────────────

def add_edge(src_id: int, dst_id: int, rel: str,
             weight: float = 1.0, properties: dict | None = None) -> int:
    now = datetime.now().isoformat()
    with _conn() as c:
        # Avoid duplicate edges (same src, dst, rel)
        existing = c.execute(
            "SELECT id FROM kg_edges WHERE src_id=? AND dst_id=? AND rel=?",
            (src_id, dst_id, rel),
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE kg_edges SET weight=weight+?, properties=? WHERE id=?",
                (weight, json.dumps(properties or {}), existing["id"]),
            )
            return existing["id"]
        cur = c.execute(
            "INSERT INTO kg_edges (src_id, dst_id, rel, weight, properties, created_at) VALUES (?,?,?,?,?,?)",
            (src_id, dst_id, rel, weight, json.dumps(properties or {}), now),
        )
        return cur.lastrowid


def get_edges(node_id: int, direction: str = "both") -> list[dict]:
    """Return edges for a node. direction: 'out', 'in', or 'both'."""
    with _conn() as c:
        if direction == "out":
            rows = c.execute("SELECT * FROM kg_edges WHERE src_id=?", (node_id,)).fetchall()
        elif direction == "in":
            rows = c.execute("SELECT * FROM kg_edges WHERE dst_id=?", (node_id,)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM kg_edges WHERE src_id=? OR dst_id=?", (node_id, node_id)
            ).fetchall()
    return [dict(r) for r in rows]


# ── Graph stats ───────────────────────────────────────────────────────────────

def graph_stats() -> dict:
    with _conn() as c:
        total_nodes = c.execute("SELECT COUNT(*) FROM kg_nodes").fetchone()[0]
        total_edges = c.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0]
        by_type = c.execute(
            "SELECT type, COUNT(*) as cnt FROM kg_nodes GROUP BY type"
        ).fetchall()
        top_connected = c.execute("""
            SELECT n.label, n.type, COUNT(e.id) as degree
            FROM kg_nodes n
            LEFT JOIN kg_edges e ON e.src_id=n.id OR e.dst_id=n.id
            GROUP BY n.id ORDER BY degree DESC LIMIT 5
        """).fetchall()
    return {
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "by_type": {r["type"]: r["cnt"] for r in by_type},
        "top_connected": [{"label": r["label"], "type": r["type"], "degree": r["degree"]} for r in top_connected],
    }


# ── vis-network export ────────────────────────────────────────────────────────

def get_graph_json(max_nodes: int = 200) -> dict[str, list]:
    """Return {nodes, edges} for vis-network rendering."""
    nodes = list_nodes(limit=max_nodes)
    all_edges = []
    with _conn() as c:
        rows = c.execute("SELECT * FROM kg_edges LIMIT 500").fetchall()
        all_edges = [dict(r) for r in rows]

    node_ids = {n["id"] for n in nodes}
    vis_nodes = [
        {
            "id":    n["id"],
            "label": n["label"],
            "group": n["type"],
            "color": _NODE_COLORS.get(n["type"], "#888"),
            "title": f"{n['type'].upper()}: {n['label']}",
        }
        for n in nodes
    ]
    vis_edges = [
        {
            "from":  e["src_id"],
            "to":    e["dst_id"],
            "label": e["rel"],
            "width": min(5, max(1, int(e["weight"]))),
        }
        for e in all_edges
        if e["src_id"] in node_ids and e["dst_id"] in node_ids
    ]
    return {"nodes": vis_nodes, "edges": vis_edges}
