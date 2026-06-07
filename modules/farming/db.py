import os
import sqlite3

FARMING_DB = os.getenv("FARMING_DB", "farming.db")


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(FARMING_DB)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
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
