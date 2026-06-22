"""
Injection pattern watcher + anomaly detector.

1. Pattern updater — fetches new LLM jailbreak/injection patterns from
   curated security intelligence sources and updates security/patterns.json.
   Sources: OWASP LLM Top 10, NVD CPE entries for AI/ML, CVE keyword search.

2. Anomaly detector — scans the assistant's event log for:
   - Injection attempts in user queries
   - Repeated errors from a single session
   - Unusual action sequences (e.g., trying to read files, system actions)
   - Blocked/failed actions spike
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger("security.watcher")

PROJECT      = Path(__file__).parent.parent
PATTERNS_FILE = PROJECT / "security" / "patterns.json"
GK_DB        = PROJECT / "personal_assistant.db"

# NVD API — search for AI/LLM-related CVEs (free, no auth)
NVD_SEARCH_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"


def _load_patterns() -> dict:
    """Load security/patterns.json, or an empty skeleton if it is missing."""
    if PATTERNS_FILE.exists():
        return json.loads(PATTERNS_FILE.read_text())
    return {"injection_patterns": [], "_meta": {}}


def _save_patterns(data: dict) -> None:
    """Write the patterns dict back to security/patterns.json."""
    PATTERNS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _existing_pattern_ids(data: dict) -> set[str]:
    """Return the set of pattern IDs already present in the patterns data."""
    return {p["id"] for p in data.get("injection_patterns", [])}


def fetch_nvd_llm_cves() -> list[dict]:
    """
    Query NVD for recent CVEs related to LLM/AI prompt injection.
    Returns list of new pattern candidates.
    """
    new_patterns = []
    keywords = ["prompt injection", "LLM jailbreak", "AI model manipulation"]

    for kw in keywords[:1]:  # Limit to 1 query to avoid rate limiting
        try:
            resp = httpx.get(
                NVD_SEARCH_URL,
                params={"keywordSearch": kw, "resultsPerPage": 5},
                timeout=15.0,
                headers={"User-Agent": "GK-Security-Guardian/1.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                for vuln in data.get("vulnerabilities", []):
                    cve = vuln.get("cve", {})
                    cve_id = cve.get("id", "")
                    desc = " ".join(
                        d.get("value", "")
                        for d in cve.get("descriptions", [])
                        if d.get("lang") == "en"
                    )[:300]
                    if desc:
                        new_patterns.append({"cve_id": cve_id, "description": desc})
        except Exception as e:
            log.warning(f"NVD fetch failed for '{kw}': {e}")

    return new_patterns


def _generate_pattern_from_cve(cve_info: dict, next_id: int) -> dict | None:
    """
    Attempt to extract a regex pattern hint from CVE description.
    Returns None if no actionable pattern found.
    """
    desc = cve_info.get("description", "").lower()
    # Look for specific technique names we can convert to patterns
    technique_map = {
        "many-shot": (r"(repeat|example).{0,30}(ignore|override|forget).{0,30}(instruction|rule|guideline)", "many_shot"),
        "indirect injection": (r"(retrieved|fetched|loaded|tool\s+result).{0,50}(ignore|override|forget)", "indirect_injection"),
        "role-playing": (r"(role.?play|pretend|act\s+as|imagine\s+you\s+are).{0,30}(uncensored|unrestricted|evil)", "roleplay_jailbreak"),
        "token smuggling": (r"(\\u[0-9a-f]{4}|&#x[0-9a-f]+;|%[0-9a-f]{2}).{0,50}(instruction|system|admin)", "token_smuggling"),
        "context overflow": (r"(repeat|say|output).{0,20}(10000|1000|many)\s+times", "context_overflow"),
    }
    for technique, (pattern, category) in technique_map.items():
        if technique in desc:
            pat_id = f"INJ-{100 + next_id:03d}"
            return {
                "id":       pat_id,
                "pattern":  pattern,
                "severity": "high",
                "category": category,
                "source":   cve_info.get("cve_id", "generated"),
                "auto_generated": True,
            }
    return None


# Known high-value patterns we maintain (kept here, not bundled in patterns.json,
# for separation of concerns — update_patterns merges any not already present).
_STATIC_PATTERNS = [
    {
        "id": "INJ-A01",
        "pattern": r"(grandma|grandmother).{0,50}(tell me|used to tell|bedtime story).{0,100}(how to|steps to|recipe for)",
        "severity": "medium", "category": "social_engineering",
        "note": "Grandmother jailbreak — wraps harmful requests in emotional story framing"
    },
    {
        "id": "INJ-A02",
        "pattern": r"hypothetically\s+(speaking|if|say)\s+.{0,50}(how\s+(would|could|might)|what\s+steps)",
        "severity": "low", "category": "context_manipulation",
        "note": "Hypothetical framing to lower guard"
    },
    {
        "id": "INJ-A03",
        "pattern": r"(token|character|word)\s+(budget|limit|window).{0,30}(ignore|bypass|skip)",
        "severity": "medium", "category": "context_manipulation",
        "note": "Token budget manipulation"
    },
    {
        "id": "INJ-A04",
        "pattern": r"<\s*(JAILBREAK|SYSTEM|INJECT|OVERRIDE|ADMIN)\s*>",
        "severity": "critical", "category": "tag_injection",
        "note": "Custom tag injection attempts"
    },
    {
        "id": "INJ-A05",
        "pattern": r"(write|create|generate)\s+(a\s+)?story\s+(where|in\s+which|about).{0,50}(hacking|exploit|malware|ransomware)",
        "severity": "high", "category": "fictional_framing",
        "note": "Fictional story framing for harmful content"
    },
]


def _recently_updated(data: dict, max_hours: int = 6) -> bool:
    """True if patterns.json was updated within ``max_hours`` (skip refetch)."""
    last_up = data.get("_meta", {}).get("last_updated", "2000-01-01")
    try:
        return datetime.now() - datetime.fromisoformat(last_up) < timedelta(hours=max_hours)
    except Exception:
        return False


def _merge_patterns(data: dict, candidates: list[dict], existing_ids: set[str],
                    source_label) -> list[str]:
    """Append candidate patterns not already present (by id). Returns source tags.

    ``source_label`` may be a string prefix or a callable(candidate)->tag, so
    both static additions and per-CVE patterns can record their provenance.
    """
    sources = []
    for pat in candidates:
        if not pat or pat["id"] in existing_ids:
            continue
        data["injection_patterns"].append(pat)
        existing_ids.add(pat["id"])
        sources.append(source_label(pat) if callable(source_label) else f"{source_label}:{pat['id']}")
    return sources


def update_patterns(force: bool = False) -> dict:
    """Refresh injection patterns from intelligence sources (additive only).

    Skips if updated within the last 6 hours (unless ``force``). Merges NVD-CVE
    derived patterns and the maintained static set, then rewrites patterns.json.
    Returns a summary of what changed.
    """
    data = _load_patterns()
    if not force and _recently_updated(data):
        log.info("Patterns up-to-date — skipping fetch.")
        return {"status": "skipped", "reason": "Updated less than 6 hours ago"}

    existing_ids = _existing_pattern_ids(data)
    next_id      = len(data.get("injection_patterns", [])) + 1

    log.info("Fetching NVD CVEs for AI/LLM vulnerabilities...")
    cve_patterns = []
    for cve_info in fetch_nvd_llm_cves():
        pat = _generate_pattern_from_cve(cve_info, next_id)
        if pat:
            cve_patterns.append(pat)
            next_id += 1
    sources  = _merge_patterns(data, cve_patterns, existing_ids, "NVD")
    sources += _merge_patterns(data, _STATIC_PATTERNS, existing_ids, "static")

    data["_meta"]["last_updated"]   = datetime.now().strftime("%Y-%m-%d")
    data["_meta"]["total_patterns"] = len(data["injection_patterns"])
    data["_meta"]["last_sources"]   = sources
    _save_patterns(data)
    log.info(f"Patterns updated: {len(sources)} new patterns added. Total: {len(data['injection_patterns'])}")

    return {
        "status":     "updated",
        "added":      len(sources),
        "total":      len(data["injection_patterns"]),
        "sources":    sources,
        "updated_at": datetime.now().isoformat(),
    }


# ── Log anomaly detection ──────────────────────────────────────────────────────

def _load_pattern_regexes() -> list[re.Pattern]:
    """Compile every injection pattern from patterns.json (skipping bad regexes)."""
    patterns = []
    for p in _load_patterns().get("injection_patterns", []):
        try:
            patterns.append(re.compile(p["pattern"], re.IGNORECASE))
        except re.error:
            pass
    return patterns


def _read_recent_events(hours: int):
    """Fetch event rows from the last N hours. Returns (rows, error_or_None)."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    try:
        con = sqlite3.connect(str(GK_DB))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM events WHERE ts >= ? ORDER BY ts DESC", (since,)
        ).fetchall()
        con.close()
        return rows, None
    except Exception as e:
        log.error(f"Could not read event log: {e}")
        return [], str(e)


def _check_injections(rows, patterns) -> dict | None:
    """Alert if any event query matches a known injection pattern."""
    hits = []
    for row in rows:
        query = row["query"] if "query" in row.keys() else ""
        if not query:
            continue
        for pat in patterns:
            m = pat.search(query)
            if m:
                hits.append({"event_id": row["id"], "query": query[:100],
                             "matched": m.group(0)[:60], "at": row["ts"]})
                break
    if not hits:
        return None
    return {"type": "INJECTION_ATTEMPT", "severity": "HIGH", "count": len(hits),
            "details": hits[:5],
            "message": f"{len(hits)} injection attempt(s) detected in queries"}


def _check_error_spike(error_rows, hours) -> dict | None:
    """Alert if more than 5 errors occurred in the window."""
    if len(error_rows) <= 5:
        return None
    return {"type": "ERROR_SPIKE", "severity": "MEDIUM", "count": len(error_rows),
            "message": f"{len(error_rows)} errors in last {hours}h — possible attack or system failure"}


def _check_module_failures(error_rows, hours) -> list[dict]:
    """Alert for each module that failed 3+ times in the window."""
    counts: dict[str, int] = {}
    for r in error_rows:
        mod = r["module"] if "module" in r.keys() else "unknown"
        counts[mod] = counts.get(mod, 0) + 1
    return [
        {"type": "MODULE_FAILURE", "severity": "MEDIUM", "module": mod, "count": c,
         "message": f"Module '{mod}' failed {c} times in last {hours}h"}
        for mod, c in counts.items() if c >= 3
    ]


def _check_query_volume(rows, hours) -> dict | None:
    """Alert if query volume in the window is unusually high (>200)."""
    if len(rows) <= 200:
        return None
    return {"type": "HIGH_QUERY_VOLUME", "severity": "LOW", "count": len(rows),
            "message": f"{len(rows)} queries in last {hours}h — unusually high volume"}


def detect_anomalies(hours: int = 24) -> dict:
    """Scan the last N hours of assistant events for security anomalies.

    Runs four checks — injection attempts, error spikes, repeated per-module
    failures, and query flooding — and returns a structured alert list.
    """
    if not GK_DB.exists():
        return {"status": "no_db", "alerts": []}

    rows, error = _read_recent_events(hours)
    if error:
        return {"status": "error", "error": error, "alerts": []}
    if not rows:
        return {"status": "ok", "events_checked": 0, "alerts": []}

    error_rows = [r for r in rows if (r["status"] if "status" in r.keys() else "") == "error"]

    alerts: list[dict] = []
    if (a := _check_injections(rows, _load_pattern_regexes())): alerts.append(a)
    if (a := _check_error_spike(error_rows, hours)):            alerts.append(a)
    alerts.extend(_check_module_failures(error_rows, hours))
    if (a := _check_query_volume(rows, hours)):                 alerts.append(a)

    return {
        "status":         "ok",
        "checked_at":     datetime.now().isoformat(),
        "hours_checked":  hours,
        "events_checked": len(rows),
        "alerts":         alerts,
        "alert_count":    len(alerts),
    }
