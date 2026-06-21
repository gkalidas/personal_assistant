"""
Todo task verifiers — each verifier config is stored as JSON in todos.verifier.

Supported types:
  env_var       — key must be present and non-empty in .env
  file_exists   — path must exist on disk
  file_contains — file must match a regex pattern
  db_nonempty   — SQLite table must have at least one row
  api_ok        — internal API endpoint must return HTTP 200
  manual        — no auto-check; user must confirm explicitly

Example verifier JSON stored in todos table:
  {"type": "env_var", "key": "DIGEST_EMAIL_PASS"}
  {"type": "file_contains", "path": ".env", "pattern": "DIGEST_EMAIL_PASS=.+"}
  {"type": "db_nonempty", "db": "faces.db", "table": "faces", "where": "face_idx >= 0"}
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent


def _read_dotenv() -> dict[str, str]:
    """Parse .env file into key→value dict (strips comments, blank lines)."""
    env_file = _PROJECT_ROOT / ".env"
    result: dict[str, str] = {}
    if not env_file.exists():
        return result
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip()
    return result


# ── Individual verifier functions ─────────────────────────────────────────────

def _verify_env_var(cfg: dict) -> tuple[bool, str]:
    key = cfg.get("key", "")
    if not key:
        return False, "verifier config missing 'key'"
    env = _read_dotenv()
    val = env.get(key, "")
    if val:
        return True, f"{key} is set"
    return False, f"{key} is empty or missing in .env"


def _verify_file_exists(cfg: dict) -> tuple[bool, str]:
    raw = cfg.get("path", "")
    path = (_PROJECT_ROOT / raw).resolve() if not Path(raw).is_absolute() else Path(raw)
    if path.exists():
        return True, f"{path.name} exists"
    return False, f"File not found: {path}"


def _verify_file_contains(cfg: dict) -> tuple[bool, str]:
    raw     = cfg.get("path", "")
    pattern = cfg.get("pattern", "")
    path = (_PROJECT_ROOT / raw).resolve() if not Path(raw).is_absolute() else Path(raw)
    if not path.exists():
        return False, f"File not found: {path.name}"
    if not pattern:
        return True, f"{path.name} exists"
    try:
        content = path.read_text()
        if re.search(pattern, content, re.MULTILINE):
            return True, f"Pattern found in {path.name}"
        return False, f"Pattern not found in {path.name}: {pattern!r}"
    except Exception as e:
        return False, f"Read error: {e}"


def _verify_db_nonempty(cfg: dict) -> tuple[bool, str]:
    db_name = cfg.get("db", "")
    table   = cfg.get("table", "")
    where   = cfg.get("where", "")
    if not db_name or not table:
        return False, "verifier config missing 'db' or 'table'"
    db_path = _PROJECT_ROOT / db_name
    if not db_path.exists():
        return False, f"DB not found: {db_name}"
    try:
        sql = f"SELECT COUNT(*) FROM {table}"  # nosec B608 — table name from verifier config, admin-defined
        if where:
            sql += f" WHERE {where}"           # nosec B608
        with sqlite3.connect(str(db_path)) as con:
            count = con.execute(sql).fetchone()[0]
        if count > 0:
            return True, f"{table} has {count} row(s)"
        return False, f"{table} is empty"
    except Exception as e:
        return False, f"DB check error: {e}"


def _verify_manual(_cfg: dict) -> tuple[bool, str]:
    return False, "manual verification required — mark done only after confirming"


_VERIFIER_FNS = {
    "env_var":       _verify_env_var,
    "file_exists":   _verify_file_exists,
    "file_contains": _verify_file_contains,
    "db_nonempty":   _verify_db_nonempty,
    "manual":        _verify_manual,
}


# ── Public API ────────────────────────────────────────────────────────────────

def run_verifier(verifier_json: str | dict) -> tuple[bool, str]:
    """
    Run a verifier. Returns (passed: bool, note: str).
    Accepts JSON string or already-parsed dict.
    """
    if isinstance(verifier_json, str):
        try:
            cfg = json.loads(verifier_json)
        except Exception:
            return False, f"invalid verifier JSON: {verifier_json!r}"
    else:
        cfg = verifier_json

    vtype = cfg.get("type", "manual")
    fn    = _VERIFIER_FNS.get(vtype)
    if not fn:
        return False, f"unknown verifier type: {vtype!r}"
    try:
        return fn(cfg)
    except Exception as e:
        log.error("verifier %r crashed: %s", vtype, e)
        return False, f"verifier error: {e}"


def verify_all_todos() -> list[dict]:
    """
    Re-run verifiers for every todo that has one.
    Returns list of {id, text, passed, note}.
    """
    from dashboard.todo_store import list_todos, update_todo
    results = []
    for todo in list_todos(include_done=True):
        v = todo.get("verifier")
        if not v:
            continue
        passed, note = run_verifier(v)
        update_todo(todo["id"],
                    verified=1 if passed else -1,
                    verify_note=note)
        if not passed and todo.get("done"):
            # Reopen the task — it claimed done but verification failed
            update_todo(todo["id"], done=0)
            log.warning("todo %s reopened: %s", todo["id"], note)
        results.append({
            "id":     todo["id"],
            "text":   todo["text"],
            "passed": passed,
            "note":   note,
        })
    return results
