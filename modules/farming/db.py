import os
import sqlite3

FARMING_DB = os.getenv("FARMING_DB", "farming.db")


def conn() -> sqlite3.Connection:
    """Open the farming SQLite DB (WAL mode), re-reading FARMING_DB each call."""
    # Re-read env each call so tests can override FARMING_DB after import
    path = os.getenv("FARMING_DB", FARMING_DB)
    c = sqlite3.connect(path, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")  # allow concurrent readers while writing
    return c


def init() -> None:
    """Create the plots, crops, spray_logs, and observations tables if absent."""
    with conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS plots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                area_acres  REAL,
                soil_type   TEXT,
                lat         REAL,
                lon         REAL,
                notes       TEXT,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crops (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plot_id         INTEGER REFERENCES plots(id),
                crop_name       TEXT NOT NULL,
                variety         TEXT,
                planted_date    TEXT,
                expected_harvest TEXT,
                status          TEXT DEFAULT 'active' CHECK(status IN ('active','harvested','failed')),
                yield_kg        REAL,
                notes           TEXT,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS spray_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                plot_id     INTEGER REFERENCES plots(id),
                crop_id     INTEGER REFERENCES crops(id),
                date        TEXT NOT NULL,
                chemical    TEXT NOT NULL,
                quantity    TEXT,
                reason      TEXT,
                notes       TEXT
            );

            CREATE TABLE IF NOT EXISTS observations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                plot_id     INTEGER REFERENCES plots(id),
                crop_id     INTEGER REFERENCES crops(id),
                date        TEXT NOT NULL,
                type        TEXT NOT NULL CHECK(type IN ('disease','pest','weather_damage','growth','yield','soil','other')),
                description TEXT NOT NULL,
                image_path  TEXT,
                severity    TEXT CHECK(severity IN ('low','medium','high')),
                resolved    INTEGER DEFAULT 0
            );
        """)
