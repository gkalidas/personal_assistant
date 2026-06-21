"""
SQLite storage for face embeddings and cluster labels.

Schema:
  faces     — one row per detected face, with embedding + source photo path
  clusters  — named person clusters (id, name, exemplar_face_id)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent.parent / "faces.db"


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(_DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS faces (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_path  TEXT NOT NULL,
            face_idx    INTEGER NOT NULL DEFAULT 0,
            embedding   TEXT NOT NULL,          -- JSON float array
            bbox        TEXT,                   -- JSON [x1,y1,x2,y2]
            cluster_id  INTEGER,                -- NULL = unassigned
            det_score   REAL,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_path);
        CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);

        CREATE TABLE IF NOT EXISTS clusters (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL DEFAULT 'Unknown',
            exemplar_face_id INTEGER,
            face_count      INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );
        """)
    log.debug("faces db initialised at %s", _DB_PATH)


def insert_face(photo_path: str, face_idx: int, embedding: list[float],
                bbox: list[int] | None = None, det_score: float | None = None) -> int:
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO faces (photo_path, face_idx, embedding, bbox, det_score) "
            "VALUES (?,?,?,?,?)",
            (photo_path, face_idx, json.dumps(embedding),
             json.dumps(bbox) if bbox else None, det_score),
        )
        return cur.lastrowid


def get_all_embeddings() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT id, photo_path, face_idx, embedding, cluster_id FROM faces"
        ).fetchall()
    return [dict(r) for r in rows]


def get_unassigned() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT id, photo_path, face_idx, embedding FROM faces WHERE cluster_id IS NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def already_processed(photo_path: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM faces WHERE photo_path=? LIMIT 1", (photo_path,)
        ).fetchone()
    return row is not None


def assign_cluster(face_id: int, cluster_id: int) -> None:
    with _conn() as con:
        con.execute("UPDATE faces SET cluster_id=? WHERE id=?", (cluster_id, face_id))


def clear_clusters() -> None:
    """Remove all cluster rows and reset all face cluster assignments. Called before re-clustering."""
    with _conn() as con:
        con.execute("DELETE FROM clusters")
        con.execute("UPDATE faces SET cluster_id=NULL")


def upsert_cluster(name: str, exemplar_face_id: int | None, face_count: int) -> int:
    """Insert a new cluster row and return its DB id."""
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO clusters (name, exemplar_face_id, face_count) VALUES (?,?,?)",
            (name, exemplar_face_id, face_count),
        )
        return cur.lastrowid


def list_clusters() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT c.id, c.name, c.face_count, c.updated_at, "
            "       f.photo_path as exemplar_photo "
            "FROM clusters c "
            "LEFT JOIN faces f ON f.id = c.exemplar_face_id "
            "ORDER BY c.face_count DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def rename_cluster(cluster_id: int, name: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE clusters SET name=?, updated_at=datetime('now') WHERE id=?",
            (name, cluster_id),
        )


def faces_for_photo(photo_path: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT f.id, f.face_idx, f.bbox, f.cluster_id, c.name as person_name "
            "FROM faces f LEFT JOIN clusters c ON c.id=f.cluster_id "
            "WHERE f.photo_path=?",
            (photo_path,),
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    with _conn() as con:
        total_faces    = con.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
        total_clusters = con.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
        unassigned     = con.execute(
            "SELECT COUNT(*) FROM faces WHERE cluster_id IS NULL"
        ).fetchone()[0]
    return {
        "total_faces":    total_faces,
        "total_clusters": total_clusters,
        "unassigned":     unassigned,
    }
