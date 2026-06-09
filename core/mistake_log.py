"""
Jarvis Mistake Journal — writes every error, uncertain response, routing
mismatch, or model failure to two sinks:

  1. SQLite table `mistake_log`  — queryable from the dashboard
  2. logs/mistakes.jsonl         — human-readable, for offline analysis

Error types
-----------
  routing_mismatch   embed-router and LLM-router chose different modules
  uncertain_response model admitted it doesn't know; search fallback triggered
  module_error       module.handle() raised an exception
  llm_timeout        Ollama call exceeded the timeout threshold
  llm_fallback       primary model failed; fallback model was used
  unknown_action     module received an action it doesn't handle
  search_fallback    search was used as fallback for another module

Analyser hints
--------------
  Run `python -m core.mistake_log` to print a pattern summary and suggested fixes.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_GK_DB        = os.getenv("GK_DB", "personal_assistant.db")
_MISTAKES_DIR = Path("logs")
_JSONL        = _MISTAKES_DIR / "mistakes.jsonl"

# ── Schema bootstrap ──────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_GK_DB)
    c.row_factory = sqlite3.Row
    return c


def ensure_table() -> None:
    """Create mistake_log table if absent (safe to call repeatedly)."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS mistake_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                error_type  TEXT NOT NULL,
                query       TEXT,
                module      TEXT,
                details     TEXT,
                severity    TEXT DEFAULT 'low'
            )
        """)
    _MISTAKES_DIR.mkdir(exist_ok=True)


# ── Logging API ───────────────────────────────────────────────────────────────

def log_mistake(
    error_type: str,
    *,
    query: str = "",
    module: str = "",
    details: dict | str | None = None,
    severity: str = "low",          # "low" | "medium" | "high"
) -> None:
    """
    Record one mistake/anomaly.

    Parameters
    ----------
    error_type : str
        One of the error types listed in the module docstring.
    query : str
        The user's original query (sanitized/redacted).
    module : str
        Module name involved, if any.
    details : dict | str | None
        Any extra context (model name, exception message, etc.).
    severity : str
        "low" | "medium" | "high" — used for dashboard colour-coding.
    """
    ts   = datetime.now().isoformat()
    det  = json.dumps(details) if isinstance(details, dict) else (details or "")
    row  = {
        "ts": ts, "error_type": error_type,
        "query": query[:500], "module": module,
        "details": det, "severity": severity,
    }

    # ── SQLite ────────────────────────────────────────────────────────────────
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO mistake_log (ts, error_type, query, module, details, severity) "
                "VALUES (?,?,?,?,?,?)",
                (ts, error_type, row["query"], module, det, severity),
            )
    except Exception as e:
        log.warning("mistake_log sqlite write failed: %s", e)

    # ── JSONL ─────────────────────────────────────────────────────────────────
    try:
        _MISTAKES_DIR.mkdir(exist_ok=True)
        with _JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("mistake_log jsonl write failed: %s", e)

    log.info("mistake logged: type=%s severity=%s module=%s q=%r",
             error_type, severity, module, query[:60])


def get_mistakes(limit: int = 100, error_type: str | None = None) -> list[dict]:
    """Fetch recent mistakes from SQLite (newest first)."""
    try:
        with _conn() as c:
            if error_type:
                rows = c.execute(
                    "SELECT * FROM mistake_log WHERE error_type=? ORDER BY ts DESC LIMIT ?",
                    (error_type, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM mistake_log ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def count_by_type() -> dict[str, int]:
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT error_type, COUNT(*) as n FROM mistake_log GROUP BY error_type ORDER BY n DESC"
            ).fetchall()
        return {r["error_type"]: r["n"] for r in rows}
    except Exception:
        return {}


# ── Pattern analyser (CLI runnable) ───────────────────────────────────────────

def suggest_fixes() -> str:
    """
    Analyse the mistake log and print actionable suggestions.
    Run from project root:  python -m core.mistake_log
    """
    counts = count_by_type()
    if not counts:
        return "No mistakes logged yet."

    lines = [
        "── JARVIS MISTAKE ANALYSIS ──────────────────────────────────",
        "",
    ]
    total = sum(counts.values())
    lines.append(f"Total mistakes logged: {total}")
    lines.append("")

    for etype, n in counts.items():
        pct = round(100 * n / total)
        bar = "█" * min(pct // 5, 20)
        lines.append(f"  {etype:<25} {n:>4}  {pct:>3}%  {bar}")

    lines.append("")
    lines.append("── SUGGESTED FIXES ──────────────────────────────────────────")

    if counts.get("routing_mismatch", 0) > 5:
        lines.append(
            "  • routing_mismatch is high → add more example phrases to "
            "core/embedding_router.py _MODULE_EXAMPLES for the misrouted modules"
        )
    if counts.get("uncertain_response", 0) > 3:
        lines.append(
            "  • uncertain_response is frequent → check if the queries can be\n"
            "    covered by adding them to the relevant module's knowledge base\n"
            "    or improving the system prompt"
        )
    if counts.get("llm_timeout", 0) > 2:
        lines.append(
            "  • llm_timeout happening → consider switching TEXT_MODEL to a\n"
            "    smaller model (qwen2.5:0.5b) or increasing server resources"
        )
    if counts.get("module_error", 0) > 2:
        lines.append(
            "  • module_error is high → review logs/mistakes.jsonl for the\n"
            "    exception details and fix the module's error handling"
        )
    if counts.get("unknown_action", 0) > 2:
        lines.append(
            "  • unknown_action suggests the LLM is generating action types\n"
            "    not in the module's handler → expand the system prompt examples"
        )

    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    ensure_table()
    print(suggest_fixes())
