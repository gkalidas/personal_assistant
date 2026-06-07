import os
import sqlite3
from pathlib import Path


FINANCE_DB = os.getenv("FINANCE_DB", "finance.db")


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(FINANCE_DB)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                amount      REAL NOT NULL,
                type        TEXT NOT NULL CHECK(type IN ('income', 'expense')),
                category    TEXT NOT NULL,
                description TEXT,
                source      TEXT DEFAULT 'manual'
            );

            CREATE TABLE IF NOT EXISTS budgets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT NOT NULL UNIQUE,
                monthly_cap REAL NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS goals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                target      REAL NOT NULL,
                saved       REAL NOT NULL DEFAULT 0,
                deadline    TEXT,
                created_at  TEXT NOT NULL
            );
        """)
