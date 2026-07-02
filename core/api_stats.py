"""
API call tracking — inbound dashboard endpoints + outbound external-API calls.

Two layers:

1. In-memory live counters (calls, latency, errors, recent hits) that back the
   /api/stats endpoint. Reset on restart.
2. A persisted per-call event log: every inbound request and every outbound
   external-API call is recorded as its own row in the `api_call_log` table of
   personal_assistant.db, with an exact timestamp — for "which API, when"
   analysis. Events are buffered in memory and batch-flushed every ~15s (and on
   shutdown) so request latency isn't hit by a DB write per call.

   Outbound query strings are NOT stored (external URLs carry API keys) — only
   host + path.

Outbound calls are captured by wrapping the httpx module-level get/post/stream
functions once at startup; every call site in this codebase uses those (no
Client instances, no async), so no call site needs editing.
"""
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import urlparse

import httpx

_GK_DB = os.getenv("GK_DB", "personal_assistant.db")

# Which process is logging (distinguishes dashboard vs guardian outbound calls).
_SOURCE = "dashboard"

_lock = threading.Lock()


def set_source(name: str) -> None:
    """Tag calls logged by this process (e.g. 'guardian'). Call before startup."""
    global _SOURCE
    _SOURCE = name


def _new_entry() -> dict:
    return {
        "calls": 0, "total_ms": 0.0, "max_ms": 0.0, "errors": 0,
        "recent": deque(maxlen=20),
    }


# direction -> { name -> entry }  (in-memory live view)
_stores: dict[str, dict[str, dict]] = {
    "inbound": defaultdict(_new_entry),
    "outbound": defaultdict(_new_entry),
}

# Pending per-call events awaiting DB flush. Capped so a persistent DB failure
# can't grow memory without bound (oldest events dropped past the cap).
# Tuple layout: (ts, source, direction, name, method, path, status, latency_ms)
_events: deque[tuple] = deque(maxlen=200_000)

_installed = False
_stop = threading.Event()
_flusher: threading.Thread | None = None


def _host_of(url) -> str:
    """Best-effort host[:port] for grouping; falls back to the raw URL."""
    try:
        netloc = urlparse(str(url)).netloc
        return netloc or str(url)
    except Exception:
        return str(url)


def _path_of(url) -> str:
    """URL path only — query string dropped so API keys aren't persisted."""
    try:
        return urlparse(str(url)).path or "/"
    except Exception:
        return ""


def _record(direction: str, name: str, method: str, path: str,
            elapsed_ms: float, status, error: bool) -> None:
    now = datetime.now()
    with _lock:
        s = _stores[direction][name]
        s["calls"] += 1
        s["total_ms"] += elapsed_ms
        if elapsed_ms > s["max_ms"]:
            s["max_ms"] = elapsed_ms
        if error or (isinstance(status, int) and status >= 400):
            s["errors"] += 1
        s["recent"].append({
            "ts": now.strftime("%H:%M:%S"),
            "ms": round(elapsed_ms, 1),
            "status": status,
        })
        _events.append((
            now.isoformat(), _SOURCE, direction, name, method, path,
            status if isinstance(status, int) else None,
            round(elapsed_ms, 1),
        ))


def record_inbound(method: str, path: str, elapsed_ms: float, status) -> None:
    """Record one inbound request to a dashboard endpoint."""
    _record("inbound", f"{method} {path}", method, path, elapsed_ms, status, error=False)


# ── Outbound httpx wrapping ────────────────────────────────────────────────────

def _timed(orig, method: str, url, args, kwargs):
    """Call orig(url, *args, **kwargs), recording host/path/latency/status."""
    t0 = time.monotonic()
    status = None
    error = False
    try:
        resp = orig(url, *args, **kwargs)
        status = getattr(resp, "status_code", None)
        return resp
    except Exception:
        error = True
        raise
    finally:
        _record("outbound", _host_of(url), method, _path_of(url),
                (time.monotonic() - t0) * 1000, status, error)


def install_httpx_tracking() -> None:
    """Monkeypatch httpx.get/post/stream to log outbound calls. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True

    _orig_get, _orig_post, _orig_stream = httpx.get, httpx.post, httpx.stream

    def get(url, *a, **kw):
        return _timed(_orig_get, "GET", url, a, kw)

    def post(url, *a, **kw):
        return _timed(_orig_post, "POST", url, a, kw)

    def stream(method, url, *a, **kw):
        # The I/O happens on context-manager __enter__, so we can't time the
        # full body here — record the call itself (latency unknown).
        _record("outbound", _host_of(url), method, _path_of(url), 0.0, None, False)
        return _orig_stream(method, url, *a, **kw)

    httpx.get, httpx.post, httpx.stream = get, post, stream


# ── Live snapshot (in-memory, for /api/stats) ──────────────────────────────────

def _public(entry: dict) -> dict:
    calls = entry["calls"]
    return {
        "calls": calls,
        "total_ms": entry["total_ms"],
        "avg_ms": round(entry["total_ms"] / calls, 1) if calls else 0.0,
        "max_ms": round(entry["max_ms"], 1),
        "errors": entry["errors"],
        "recent": list(entry["recent"])[-5:],
    }


def inbound_snapshot() -> dict:
    with _lock:
        return {k: _public(v) for k, v in _stores["inbound"].items()}


def outbound_snapshot() -> dict:
    """Per-host outbound stats, most-called first."""
    with _lock:
        items = sorted(_stores["outbound"].items(), key=lambda x: -x[1]["calls"])
        return {k: _public(v) for k, v in items}


# ── DB persistence (per-call event log) ────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_GK_DB, timeout=5.0)
    # Multiple processes (dashboard + guardian daemon) write api_call_log; wait
    # for the write lock instead of erroring with "database is locked".
    c.execute("PRAGMA busy_timeout=5000")
    c.row_factory = sqlite3.Row
    return c


def ensure_table() -> None:
    """Create the api_call_log table if absent (safe to call repeatedly)."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS api_call_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,         -- ISO timestamp of the call
                source     TEXT,                  -- process: 'dashboard' | 'guardian'
                direction  TEXT NOT NULL,         -- 'inbound' | 'outbound'
                name       TEXT NOT NULL,         -- endpoint ("GET /path") or host
                method     TEXT,                  -- HTTP method
                path       TEXT,                  -- URL path (no query string)
                status     INTEGER,               -- HTTP status, if known
                latency_ms REAL                   -- 0/NULL for streaming calls
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_api_log_ts ON api_call_log(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_api_log_name ON api_call_log(direction, name)")


def flush_to_db() -> int:
    """Batch-insert buffered per-call events. Returns number of rows written."""
    with _lock:
        if not _events:
            return 0
        batch = list(_events)
        _events.clear()
    try:
        with _conn() as c:
            c.executemany(
                "INSERT INTO api_call_log (ts, source, direction, name, method, path, status, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
    except Exception:
        # Put events back (front, original order) so they're retried next flush.
        with _lock:
            _events.extendleft(reversed(batch))
        raise
    return len(batch)


def delete_calls(
    *,
    before: str | None = None,
    after: str | None = None,
    name: str | None = None,
    direction: str | None = None,
    source: str | None = None,
) -> int:
    """
    Delete logged calls, filtered by date range and/or API name (filters ANDed).

    Passing no filter deletes every row. Returns rows deleted.

    before : ts < this ISO date/datetime (e.g. "2026-06-01")
    after  : ts >= this ISO date/datetime
    name   : exact endpoint/host ("GET /api/data" or "api.open-meteo.com")
    direction : "inbound" | "outbound"
    source : process that logged it ("dashboard" | "guardian")
    """
    clauses, params = [], []
    if before is not None:
        clauses.append("ts < ?"); params.append(before)
    if after is not None:
        clauses.append("ts >= ?"); params.append(after)
    if name is not None:
        clauses.append("name = ?"); params.append(name)
    if direction is not None:
        clauses.append("direction = ?"); params.append(direction)
    if source is not None:
        clauses.append("source = ?"); params.append(source)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as c:
        return c.execute(f"DELETE FROM api_call_log{where}", params).rowcount


def query_calls(
    *,
    before: str | None = None,
    after: str | None = None,
    name: str | None = None,
    direction: str | None = None,
    source: str | None = None,
    group: bool = False,
    limit: int = 1000,
) -> list[dict]:
    """
    Read logged calls with optional filters.

    group=True → totals per (source, direction, name): calls, errors, avg_ms.
    group=False → raw per-call rows, newest first, capped at `limit`.
    """
    clauses, params = [], []
    if before is not None:
        clauses.append("ts < ?"); params.append(before)
    if after is not None:
        clauses.append("ts >= ?"); params.append(after)
    if name is not None:
        clauses.append("name = ?"); params.append(name)
    if direction is not None:
        clauses.append("direction = ?"); params.append(direction)
    if source is not None:
        clauses.append("source = ?"); params.append(source)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    if group:
        sql = (f"SELECT source, direction, name, COUNT(*) AS calls, "
               f"SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors, "
               f"ROUND(AVG(latency_ms), 1) AS avg_ms "
               f"FROM api_call_log{where} GROUP BY source, direction, name ORDER BY calls DESC")
        with _conn() as c:
            return [dict(r) for r in c.execute(sql, params)]
    sql = (f"SELECT ts, source, direction, name, method, path, status, latency_ms "
           f"FROM api_call_log{where} ORDER BY ts DESC LIMIT ?")
    params.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params)]


def start_persistence(interval: float = 15.0) -> None:
    """Start the background event flusher (every `interval` s). Idempotent."""
    global _flusher
    if _flusher is not None:
        return
    ensure_table()

    def _loop():
        while not _stop.wait(interval):
            try:
                flush_to_db()
            except Exception:
                pass  # best-effort; retried next tick

    _flusher = threading.Thread(target=_loop, daemon=True, name="api-stats-flush")
    _flusher.start()


def stop_persistence() -> None:
    """Signal the flusher to stop and write any pending events (call on shutdown)."""
    _stop.set()
    try:
        flush_to_db()
    except Exception:
        pass
