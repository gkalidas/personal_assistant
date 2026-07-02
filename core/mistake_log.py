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
    """Open the GK SQLite DB with a Row factory."""
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
    det = json.dumps(details) if isinstance(details, dict) else (details or "")
    row = {
        "ts": datetime.now().isoformat(), "error_type": error_type,
        "query": query[:500], "module": module,
        "details": det, "severity": severity,
    }
    _write_sqlite(row)
    _write_jsonl(row)
    log.info("mistake logged: type=%s severity=%s module=%s q=%r",
             error_type, severity, module, query[:60])


def log_service_failure(service: str, detail: str = "", severity: str = "medium") -> None:
    """Record an external-service / dependency failure (Ollama, farming server,
    search, external APIs, email, …).

    Convenience wrapper around log_mistake with a uniform ``service_failure``
    type, so the analyser can group failures by ``module`` (the service name).
    Never raises — safe to call from swallowed except branches.
    """
    try:
        log_mistake("service_failure", module=service, details=detail, severity=severity)
    except Exception:
        log.debug("could not record service failure for %s", service)


def _write_sqlite(row: dict) -> None:
    """Insert one mistake row into the mistake_log table (best-effort)."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO mistake_log (ts, error_type, query, module, details, severity) "
                "VALUES (?,?,?,?,?,?)",
                (row["ts"], row["error_type"], row["query"],
                 row["module"], row["details"], row["severity"]),
            )
    except Exception as e:
        log.warning("mistake_log sqlite write failed: %s", e)


def _write_jsonl(row: dict) -> None:
    """Append one mistake row to logs/mistakes.jsonl (best-effort)."""
    try:
        _MISTAKES_DIR.mkdir(exist_ok=True)
        with _JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("mistake_log jsonl write failed: %s", e)


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
    """Return {error_type: count} across all logged mistakes, busiest first."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT error_type, COUNT(*) as n FROM mistake_log GROUP BY error_type ORDER BY n DESC"
            ).fetchall()
        return {r["error_type"]: r["n"] for r in rows}
    except Exception:
        return {}


# ── Pattern analyser (CLI runnable) ───────────────────────────────────────────

# (error_type, threshold, suggestion) — emitted when count exceeds threshold.
_FIX_RULES = [
    ("routing_mismatch", 5,
     "routing_mismatch is high → add more example phrases to "
     "core/embedding_router.py _MODULE_EXAMPLES for the misrouted modules"),
    ("uncertain_response", 3,
     "uncertain_response is frequent → check if the queries can be\n"
     "    covered by adding them to the relevant module's knowledge base\n"
     "    or improving the system prompt"),
    ("llm_timeout", 2,
     "llm_timeout happening → consider switching TEXT_MODEL to a\n"
     "    smaller model (qwen2.5:0.5b) or increasing server resources"),
    ("module_error", 2,
     "module_error is high → review logs/mistakes.jsonl for the\n"
     "    exception details and fix the module's error handling"),
    ("unknown_action", 2,
     "unknown_action suggests the LLM is generating action types\n"
     "    not in the module's handler → expand the system prompt examples"),
]


def _mistake_histogram(counts: dict[str, int], total: int) -> list[str]:
    """Render the per-type count/percentage bar chart lines."""
    lines = [f"Total mistakes logged: {total}", ""]
    for etype, n in counts.items():
        pct = round(100 * n / total)
        lines.append(f"  {etype:<25} {n:>4}  {pct:>3}%  {'█' * min(pct // 5, 20)}")
    return lines


def suggest_fixes() -> str:
    """Analyse the mistake log and return a summary with actionable suggestions.

    Run from project root:  python -m core.mistake_log
    """
    counts = count_by_type()
    if not counts:
        return "No mistakes logged yet."

    total = sum(counts.values())
    lines = ["── JARVIS MISTAKE ANALYSIS ──────────────────────────────────", ""]
    lines += _mistake_histogram(counts, total)
    lines += ["", "── SUGGESTED FIXES ──────────────────────────────────────────"]
    lines += [f"  • {msg}" for etype, threshold, msg in _FIX_RULES
              if counts.get(etype, 0) > threshold]
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    ensure_table()
    print(suggest_fixes())
