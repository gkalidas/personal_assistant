"""
System Load Monitor — observes usage patterns and detects idle windows.

Continuously samples:
  - CPU load (1-second average via psutil)
  - RAM usage
  - Disk I/O busyness (delta between samples)
  - Recent assistant queries (events table — last 8 minutes)
  - Ollama model state (loaded vs actively inferring)

Persists observations to SQLite → learns hourly load patterns over time.
After ~2 days of data, pattern predictions become reliable.

API:
  observe()                  → sample + store current load (call every 60s)
  is_idle()                  → bool: safe to run a background task right now
  pattern_by_hour(days=7)    → dict[int, float]: hour→avg load score 0-100
  pattern_by_dow_hour(days)  → dict[(dow,hour), float]: day-of-week-aware load
  predicted_idle_hours()     → list[int]: upcoming hours likely to be idle (dow-aware)
  report()                   → dict: current state + pattern summary
  print_pattern()            → human-readable usage heatmap to stdout
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import psutil

log = logging.getLogger("security.load_monitor")

PROJECT  = Path(__file__).parent.parent
DB_PATH  = PROJECT / "logs" / "security" / "load_monitor.db"
GK_DB    = PROJECT / "personal_assistant.db"
OLLAMA   = "http://localhost:11434"

# ── Idle thresholds (tune these to your system) ────────────────────────────────
CPU_IDLE_PCT     = 30    # CPU must be below this
RAM_IDLE_PCT     = 85    # RAM must be below this
QUERY_IDLE_MINS  = 8     # No assistant queries in last N minutes
IO_IDLE_PCT      = 40    # Disk I/O busy time below this %
# Composite load score below this = "idle"
IDLE_SCORE_MAX   = 25


# ── SQLite schema ──────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    dow             INTEGER,   -- 0=Monday … 6=Sunday
    hour            INTEGER,
    minute          INTEGER,
    cpu_pct         REAL,
    ram_pct         REAL,
    io_busy_pct     REAL,
    recent_queries  INTEGER,
    ollama_busy     INTEGER,   -- 1 if model actively inferring
    load_score      REAL       -- composite 0–100 (0=idle, 100=max)
);

CREATE TABLE IF NOT EXISTS pattern (
    dow             INTEGER,
    hour            INTEGER,
    avg_load        REAL,
    sample_count    INTEGER,
    PRIMARY KEY (dow, hour)
);
"""


def _conn() -> sqlite3.Connection:
    """Open the load-monitor SQLite DB (creating schema), with Row factory."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


# ── Metric samplers ────────────────────────────────────────────────────────────

def _cpu() -> float:
    """Current CPU utilisation percent (1-second average)."""
    return psutil.cpu_percent(interval=1)


def _ram() -> float:
    """Current RAM utilisation percent."""
    return psutil.virtual_memory().percent


_prev_io_time: float = 0.0
_prev_wall: float    = 0.0

def _io_busy() -> float:
    """Percentage of time the disk was busy since last call."""
    global _prev_io_time, _prev_wall
    try:
        counters = psutil.disk_io_counters()
        if counters is None:
            return 0.0
        now_io   = counters.busy_time / 1000.0  # ms → s
        now_wall = time.monotonic()
        elapsed  = now_wall - _prev_wall
        io_delta = now_io   - _prev_io_time
        _prev_io_time = now_io
        _prev_wall    = now_wall
        if elapsed <= 0:
            return 0.0
        return min(100.0, (io_delta / elapsed) * 100.0)
    except Exception:
        return 0.0


def _recent_queries(minutes: int = QUERY_IDLE_MINS) -> int:
    """Count assistant queries in the last N minutes."""
    if not GK_DB.exists():
        return 0
    try:
        since = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        con   = sqlite3.connect(str(GK_DB))
        row   = con.execute(
            "SELECT COUNT(*) FROM events WHERE ts >= ? AND status != 'error'",
            (since,)
        ).fetchone()
        con.close()
        return row[0] if row else 0
    except Exception:
        return 0


def _ollama_busy() -> bool:
    """
    True if Ollama is actively running inference right now.
    Heuristic: a model is loaded AND its expires_at is far in the future
    (i.e. it was loaded/used very recently — keep_alive resets on each call).
    """
    try:
        resp = httpx.get(f"{OLLAMA}/api/ps", timeout=2.0)
        if resp.status_code != 200:
            return False
        models = resp.json().get("models", [])
        if not models:
            return False
        # Check if any model was used in the last 2 minutes
        for m in models:
            exp_str = m.get("expires_at", "")
            if not exp_str:
                continue
            # expires_at = now + keep_alive. If keep_alive=10m and exp is 10m away → fresh use.
            # If exp is only 1-2 min away, the model has been idle for 8+ min → safe.
            try:
                from datetime import timezone
                exp = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                # keep_alive default is 5 minutes. If expires in > 4 min, used very recently.
                minutes_left = (exp - now).total_seconds() / 60
                if minutes_left > 4.0:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False


def _load_score(cpu: float, ram: float, io: float,
                queries: int, ollama: bool) -> float:
    """
    Composite load score 0–100.
    Weights: CPU 50%, RAM 20%, IO 15%, queries 10%, ollama 5%.
    If the user is actively querying the assistant, treat as heavy load (≥ 80).
    """
    if queries > 0:
        return max(80.0, cpu * 0.5 + ram * 0.2 + io * 0.15 + 80 * 0.1)
    if ollama:
        return max(70.0, cpu * 0.5 + ram * 0.2 + io * 0.15)
    return cpu * 0.5 + ram * 0.2 + io * 0.15


# ── Core API ───────────────────────────────────────────────────────────────────

def _sample_now() -> tuple[float, float, float, int, bool, float]:
    """Sample all load metrics once. Returns (cpu, ram, io, queries, ollama, score)."""
    cpu  = _cpu()
    ram  = _ram()
    io   = _io_busy()
    qrys = _recent_queries()
    busy = _ollama_busy()
    return cpu, ram, io, qrys, busy, _load_score(cpu, ram, io, qrys, busy)


def _store_observation(row: dict, now: datetime) -> None:
    """Persist one observation, update the hourly pattern average, and trim >14d data."""
    try:
        con = _conn()
        con.execute(
            """INSERT INTO observations
               (ts, dow, hour, minute, cpu_pct, ram_pct, io_busy_pct,
                recent_queries, ollama_busy, load_score)
               VALUES (:ts,:dow,:hour,:minute,:cpu_pct,:ram_pct,:io_busy_pct,
                       :recent_queries,:ollama_busy,:load_score)""",
            row,
        )
        con.execute(
            """INSERT INTO pattern (dow, hour, avg_load, sample_count)
               VALUES (:dow, :hour, :load_score, 1)
               ON CONFLICT(dow, hour) DO UPDATE SET
                 avg_load = (avg_load * sample_count + excluded.avg_load) / (sample_count + 1),
                 sample_count = sample_count + 1""",
            row,
        )
        cutoff = (now - timedelta(days=14)).isoformat()
        con.execute("DELETE FROM observations WHERE ts < ?", (cutoff,))
        con.commit()
        con.close()
    except Exception as e:
        log.debug(f"load_monitor: observe() DB write failed: {e}")


def observe() -> dict:
    """Sample current system load, store it, and update the pattern table.

    Call every ~60 seconds from the guardian daemon. Returns the stored row.
    """
    now = datetime.now()
    cpu, ram, io, qrys, busy, score = _sample_now()
    row = {
        "ts":             now.isoformat(),
        "dow":            now.weekday(),
        "hour":           now.hour,
        "minute":         now.minute,
        "cpu_pct":        round(cpu, 1),
        "ram_pct":        round(ram, 1),
        "io_busy_pct":    round(io, 1),
        "recent_queries": qrys,
        "ollama_busy":    int(busy),
        "load_score":     round(score, 1),
    }
    _store_observation(row, now)
    return row


def is_idle() -> bool:
    """
    Quick check: is the system idle enough to run a background task right now?
    Does NOT store an observation (call observe() separately for that).
    """
    try:
        cpu   = psutil.cpu_percent(interval=1)
        ram   = psutil.virtual_memory().percent
        qrys  = _recent_queries()
        busy  = _ollama_busy()

        if qrys > 0:
            return False   # User is actively talking to the assistant
        if busy:
            return False   # Ollama is inferring
        if cpu >= CPU_IDLE_PCT:
            return False
        if ram >= RAM_IDLE_PCT:
            return False

        io = _io_busy()
        score = _load_score(cpu, ram, io, 0, False)
        return score < IDLE_SCORE_MAX
    except Exception:
        return False


def pattern_by_hour(days: int = 7) -> dict[int, float]:
    """
    Returns {hour: avg_load_score} averaged across all days-of-week
    for the past `days` days. Gaps (not enough data) return None.
    """
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        con   = _conn()
        rows  = con.execute(
            """SELECT hour, AVG(load_score) as avg_load, COUNT(*) as cnt
               FROM observations
               WHERE ts >= ?
               GROUP BY hour
               ORDER BY hour""",
            (since,),
        ).fetchall()
        con.close()
        return {r["hour"]: round(r["avg_load"], 1) for r in rows if r["cnt"] >= 3}
    except Exception:
        return {}


def pattern_by_dow_hour(days: int = 14) -> dict[tuple[int, int], float]:
    """
    Returns {(dow, hour): avg_load_score} for the past `days` days, where
    dow is 0=Monday … 6=Sunday. Only cells with >= 3 samples are included.

    This is the day-of-week-aware view: it distinguishes a quiet weekday
    afternoon from a busy weekend evening, which a flat hourly average can't.
    """
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        con   = _conn()
        rows  = con.execute(
            """SELECT dow, hour, AVG(load_score) as avg_load, COUNT(*) as cnt
               FROM observations
               WHERE ts >= ?
               GROUP BY dow, hour""",
            (since,),
        ).fetchall()
        con.close()
        return {(r["dow"], r["hour"]): round(r["avg_load"], 1)
                for r in rows if r["cnt"] >= 3}
    except Exception:
        return {}


def predicted_idle_hours(n: int = 6, days: int = 14) -> list[int]:
    """
    Returns the next N calendar hours (0-23) that are typically idle.

    Day-of-week aware: ranks the upcoming hours using THIS weekday's learned
    pattern first, falling back to the flat cross-day average for hours with
    too little day-specific data, then to a late-night window if there is
    barely any data at all.
    """
    now   = datetime.now()
    now_h = now.hour
    dow   = now.weekday()

    dow_pat = pattern_by_dow_hour(days=days)   # (dow, hour) -> load
    flat    = pattern_by_hour(days=days)       # hour -> load (all days)

    # Not enough data anywhere — default to the late-night window.
    if not dow_pat and len(flat) < 6:
        return [(now_h + i + 1) % 24 for i in range(6) if (now_h + i + 1) % 24 in range(1, 6)]

    def _score(offset: int) -> float:
        """Load score for the hour `offset` hours ahead, on its real weekday."""
        future = now + timedelta(hours=offset)
        key = (future.weekday(), future.hour)
        if key in dow_pat:
            return dow_pat[key]
        return flat.get(future.hour, 50.0)   # unknown → assume moderate

    # Rank the next 24h window by predicted load (lowest first).
    candidates = sorted((_score(off), (now_h + off) % 24) for off in range(1, 25))
    return [h for _, h in candidates[:n]]


def _observation_stats() -> tuple[int, str]:
    """Return (total observations, date of earliest observation or 'none')."""
    try:
        con    = _conn()
        total  = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        oldest = con.execute("SELECT MIN(ts) FROM observations").fetchone()[0]
        con.close()
        return total, (oldest[:10] if oldest else "none")
    except Exception:
        return 0, "none"


def report() -> dict:
    """Current load snapshot + learned-pattern summary."""
    try:
        cpu, ram, io, qrys, busy, score = _sample_now()
    except Exception:
        cpu = ram = io = score = 0.0
        qrys = 0
        busy = False

    total, obs_since = _observation_stats()
    pat    = pattern_by_hour()
    idle_h = predicted_idle_hours()

    return {
        "current": {
            "cpu_pct":        round(cpu, 1),
            "ram_pct":        round(ram, 1),
            "io_busy_pct":    round(io, 1),
            "recent_queries": qrys,
            "ollama_busy":    busy,
            "load_score":     round(score, 1),
            "is_idle":        score < IDLE_SCORE_MAX and qrys == 0 and not busy,
        },
        "pattern": {
            "observations_total": total,
            "observing_since":    obs_since,
            "hourly_avg_load":    pat,
            "predicted_idle_hours": idle_h,
            "data_quality": (
                "good (7+ days)" if total > 1000
                else "building up" if total > 100
                else "too early — need more observations"
            ),
        },
    }


def print_pattern(days: int = 7) -> None:
    """Print a compact heatmap of your usage pattern to stdout."""
    pat = pattern_by_hour(days=days)
    if not pat:
        print("  Not enough data yet. Run the assistant for a day or two first.")
        return

    now_h = datetime.now().hour
    print(f"\n  System Load Pattern (last {days} days)  ← lower = better time for bg tasks\n")
    print("  Hour  Load  Bar")
    print("  ────  ────  " + "─" * 22)
    for h in range(24):
        score = pat.get(h)
        marker = "◄ now" if h == now_h else ""
        if score is None:
            bar = "··· (no data)"
            label = "   ?"
        else:
            filled = int(score / 5)    # 0-100 → 0-20 blocks
            bar    = "█" * filled + "░" * (20 - filled)
            label  = f"{score:5.1f}"
            if score < IDLE_SCORE_MAX:
                bar = bar + "  ✓ idle"
        print(f"  {h:02d}:00 {label}  {bar}  {marker}")
    print()
    idle = predicted_idle_hours(n=4, days=days)
    if idle:
        print(f"  Best upcoming hours for background tasks: {', '.join(f'{h:02d}:xx' for h in idle)}\n")
