import os
import sqlite3

HEALTH_DB = os.getenv("HEALTH_DB", "health.db")


def conn() -> sqlite3.Connection:
    """Open the health SQLite DB with a Row factory."""
    c = sqlite3.connect(HEALTH_DB)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    """Create the health_readings and health_goals tables if absent."""
    with conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS health_readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                time        TEXT NOT NULL,
                type        TEXT NOT NULL CHECK(type IN (
                                'bp','steps','weight','sleep','sugar','water','mood','other'
                            )),
                value1      REAL NOT NULL,
                value2      REAL,
                unit        TEXT,
                meal_state  TEXT CHECK(meal_state IN ('fasting','post_meal','random',NULL)),
                notes       TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_health_type_date
                ON health_readings(type, date);

            CREATE TABLE IF NOT EXISTS health_goals (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                type       TEXT NOT NULL UNIQUE,
                target     REAL NOT NULL,
                unit       TEXT,
                notes      TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
