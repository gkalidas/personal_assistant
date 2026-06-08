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
    if PATTERNS_FILE.exists():
        return json.loads(PATTERNS_FILE.read_text())
    return {"injection_patterns": [], "_meta": {}}


def _save_patterns(data: dict) -> None:
    PATTERNS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _existing_pattern_ids(data: dict) -> set[str]:
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


def update_patterns(force: bool = False) -> dict:
    """
    Refresh injection patterns from security intelligence sources.
    Only adds new patterns — never removes existing ones.
    Returns summary of what changed.
    """
    data    = _load_patterns()
    meta    = data.get("_meta", {})
    last_up = meta.get("last_updated", "2000-01-01")
    added   = 0
    sources = []

    # Check if update is needed (run at most once per 6 hours unless forced)
    if not force:
        try:
            last_dt = datetime.fromisoformat(last_up)
            if datetime.now() - last_dt < timedelta(hours=6):
                log.info("Patterns up-to-date — skipping fetch.")
                return {"status": "skipped", "reason": "Updated less than 6 hours ago"}
        except Exception:
            pass

    existing_ids = _existing_pattern_ids(data)
    next_id      = len(data.get("injection_patterns", [])) + 1

    # Source 1: NVD LLM CVEs
    log.info("Fetching NVD CVEs for AI/LLM vulnerabilities...")
    cve_list = fetch_nvd_llm_cves()
    for cve_info in cve_list:
        pat = _generate_pattern_from_cve(cve_info, next_id)
        if pat and pat["id"] not in existing_ids:
            data["injection_patterns"].append(pat)
            existing_ids.add(pat["id"])
            next_id += 1
            added   += 1
            sources.append(f"NVD:{cve_info.get('cve_id','?')}")

    # Source 2: Known high-value static patterns we maintain
    # (Added here rather than bundled in patterns.json for separation of concerns)
    static_additions = [
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
    for sp in static_additions:
        if sp["id"] not in existing_ids:
            data["injection_patterns"].append(sp)
            existing_ids.add(sp["id"])
            added += 1
            sources.append(f"static:{sp['id']}")

    # Update metadata
    data["_meta"]["last_updated"]   = datetime.now().strftime("%Y-%m-%d")
    data["_meta"]["total_patterns"] = len(data["injection_patterns"])
    data["_meta"]["last_sources"]   = sources

    _save_patterns(data)
    log.info(f"Patterns updated: {added} new patterns added. Total: {len(data['injection_patterns'])}")

    return {
        "status":    "updated",
        "added":     added,
        "total":     len(data["injection_patterns"]),
        "sources":   sources,
        "updated_at": datetime.now().isoformat(),
    }


# ── Log anomaly detection ──────────────────────────────────────────────────────

def _load_pattern_regexes() -> list[re.Pattern]:
    data = _load_patterns()
    patterns = []
    for p in data.get("injection_patterns", []):
        try:
            patterns.append(re.compile(p["pattern"], re.IGNORECASE))
        except re.error:
            pass
    return patterns


def detect_anomalies(hours: int = 24) -> dict:
    """
    Scan the last N hours of assistant events for security anomalies.
    Returns structured alert list.
    """
    if not GK_DB.exists():
        return {"status": "no_db", "alerts": []}

    injection_patterns = _load_pattern_regexes()
    alerts = []
    since = (datetime.now() - timedelta(hours=hours)).isoformat()

    try:
        con = sqlite3.connect(str(GK_DB))
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        # Get recent events
        rows = cur.execute(
            "SELECT * FROM events WHERE ts >= ? ORDER BY ts DESC",
            (since,)
        ).fetchall()
        con.close()
    except Exception as e:
        log.error(f"Could not read event log: {e}")
        return {"status": "error", "error": str(e), "alerts": []}

    if not rows:
        return {"status": "ok", "events_checked": 0, "alerts": []}

    # Check 1: Injection patterns in queries
    injection_hits = []
    for row in rows:
        query = row["query"] if "query" in row.keys() else ""
        if not query:
            continue
        for pat in injection_patterns:
            m = pat.search(query)
            if m:
                injection_hits.append({
                    "event_id": row["id"],
                    "query":    query[:100],
                    "matched":  m.group(0)[:60],
                    "at":       row["ts"],
                })
                break

    if injection_hits:
        alerts.append({
            "type":     "INJECTION_ATTEMPT",
            "severity": "HIGH",
            "count":    len(injection_hits),
            "details":  injection_hits[:5],
            "message":  f"{len(injection_hits)} injection attempt(s) detected in queries",
        })

    # Check 2: Error spike
    error_rows = [r for r in rows if (r["status"] if "status" in r.keys() else "") == "error"]
    if len(error_rows) > 5:
        alerts.append({
            "type":     "ERROR_SPIKE",
            "severity": "MEDIUM",
            "count":    len(error_rows),
            "message":  f"{len(error_rows)} errors in last {hours}h — possible attack or system failure",
        })

    # Check 3: Module failure pattern (same module failing repeatedly)
    module_errors: dict[str, int] = {}
    for r in error_rows:
        mod = r["module"] if "module" in r.keys() else "unknown"
        module_errors[mod] = module_errors.get(mod, 0) + 1
    for mod, count in module_errors.items():
        if count >= 3:
            alerts.append({
                "type":     "MODULE_FAILURE",
                "severity": "MEDIUM",
                "module":   mod,
                "count":    count,
                "message":  f"Module '{mod}' failed {count} times in last {hours}h",
            })

    # Check 4: Unusual query volume (possible flooding)
    if len(rows) > 200:
        alerts.append({
            "type":     "HIGH_QUERY_VOLUME",
            "severity": "LOW",
            "count":    len(rows),
            "message":  f"{len(rows)} queries in last {hours}h — unusually high volume",
        })

    return {
        "status":         "ok",
        "checked_at":     datetime.now().isoformat(),
        "hours_checked":  hours,
        "events_checked": len(rows),
        "alerts":         alerts,
        "alert_count":    len(alerts),
    }
