"""
System Module — answers queries about system load, usage patterns, and security guardian status.

No LLM call needed: all responses come from structured data in load_monitor + guardian reports.
Handles queries like:
  "is the system busy?"          → current load snapshot
  "load pattern" / "heatmap"     → 24h usage heatmap + best idle hours
  "security status"              → latest guardian scan results
  "when does the guardian run?"  → task schedule + overdue state
  "system status"                → everything at once
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from core.base_module import BaseModule, ModuleResponse

PROJECT  = Path(__file__).parent.parent.parent
LOG_DIR  = PROJECT / "logs" / "security"

# ── Intent detection ───────────────────────────────────────────────────────────

_LOAD_WORDS    = {"load", "busy", "idle", "cpu", "ram", "memory", "heavy", "free",
                  "using", "resource", "utilization", "performance", "slow"}
_PATTERN_WORDS = {"pattern", "heatmap", "heat", "best time",
                  "good time", "predict", "typical", "usually", "normally"}
_GUARDIAN_WORDS= {"guardian", "security", "scan", "audit", "vulnerability", "vuln",
                  "cve", "threat", "intel", "patch", "injection", "attack"}
_SCHEDULE_WORDS= {"next", "queue", "due", "overdue", "task", "run", "running",
                  "will run", "when", "schedule"}


def _intent(query: str) -> str:
    """Classify a system query into an intent (load/guardian/schedule/pattern/all)."""
    q = query.lower()
    # guardian + schedule words together → user is asking about guardian timing
    if any(w in q for w in _GUARDIAN_WORDS) and any(w in q for w in _SCHEDULE_WORDS):
        return "schedule"
    # explicit pattern words override ambiguous load matches
    if "pattern" in q or "heatmap" in q or "heat map" in q:
        return "pattern"
    hits = {
        "load":     sum(1 for w in _LOAD_WORDS     if w in q),
        "pattern":  sum(1 for w in _PATTERN_WORDS  if w in q),
        "guardian": sum(1 for w in _GUARDIAN_WORDS if w in q),
        "schedule": sum(1 for w in _SCHEDULE_WORDS if w in q),
    }
    if sum(hits.values()) == 0:
        return "all"
    return max(hits, key=hits.get)


# ── Data readers ───────────────────────────────────────────────────────────────

def _latest_report(name: str) -> dict | None:
    """Load logs/security/<name>_latest.json for the system view, or None."""
    path = LOG_DIR / f"{name}_latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _load_now() -> dict:
    """Return the current load snapshot from the load monitor."""
    try:
        from security.load_monitor import report
        return report()
    except Exception as e:
        return {"error": str(e)}


def _pattern_lines() -> list[str]:
    """Return lines for the 24-hour load heatmap."""
    try:
        from security.load_monitor import pattern_by_hour, predicted_idle_hours, IDLE_SCORE_MAX
        pat    = pattern_by_hour(days=7)
        idle_h = predicted_idle_hours(n=4, days=7)
        now_h  = datetime.now().hour

        if not pat:
            return ["  Not enough data yet — check back after a day or two of use."]

        lines = ["  Hour   Load   Bar"]
        for h in range(24):
            score = pat.get(h)
            marker = " ◄ now" if h == now_h else ""
            if score is None:
                bar, label = "··· (no data)", "  ?"
            else:
                filled = int(score / 5)
                bar    = "█" * filled + "░" * (20 - filled)
                label  = f"{score:4.0f}%"
                if score < IDLE_SCORE_MAX:
                    bar += "  ✓"
            lines.append(f"  {h:02d}:xx  {label}  {bar}{marker}")

        if idle_h:
            lines.append(f"\n  Best upcoming hours for background work: "
                         f"{', '.join(f'{h:02d}:xx' for h in idle_h)}")
        return lines
    except Exception as e:
        return [f"  Pattern read failed: {e}"]


def _guardian_schedule_lines() -> list[str]:
    """Show task schedule and overdue state. Reads persisted state file."""
    MIN_INTERVAL = {
        "anomaly_detect":       3600,
        "pattern_update":   6 * 3600,
        "threat_intel":    12 * 3600,
        "vuln_scan":       24 * 3600,
        "code_audit":       7 * 86400,
    }
    MAX_OVERDUE = 3.0

    # Read persisted last-run wall-clock times written by the guardian daemon
    state_file = LOG_DIR / "guardian_state.json"
    last_run_wall: dict[str, float] = {}
    updated_at = ""
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            last_run_wall = data.get("last_run_wall", {})
            updated_at    = data.get("updated_at", "")[:16]
        except Exception:
            pass

    now_wall = datetime.now().timestamp()
    lines = [f"  Guardian state as of: {updated_at or 'unknown'}\n",
             "  Task             Interval   Last run      Next run"]
    for name, interval in MIN_INTERVAL.items():
        h = interval // 3600
        d = h // 24
        iv_label = f"{d}d" if d >= 1 else f"{h}h"

        wall_ts = last_run_wall.get(name, 0.0)
        if wall_ts == 0.0:
            last_label = "never"
            elapsed    = now_wall   # treat as maximally overdue
        else:
            elapsed  = now_wall - wall_ts
            mins_ago = int(elapsed / 60)
            last_label = f"{mins_ago}m ago" if mins_ago < 60 else f"{mins_ago // 60}h ago"

        ratio = elapsed / interval
        if ratio >= MAX_OVERDUE:
            status = f"FORCED — {ratio:.1f}× overdue"
        elif ratio >= 1.0:
            status = f"DUE — waiting for idle"
        else:
            remain_m = int((interval - elapsed) / 60)
            status = f"in ~{remain_m}m (if idle)"

        lines.append(f"  {name:<18} {iv_label:<9}  {last_label:<12}  {status}")
    return lines


# ── Response builders ──────────────────────────────────────────────────────────

def _fmt_load() -> tuple[str, dict]:
    """Format the current load + usage pattern into a display block."""
    r = _load_now()
    if "error" in r:
        return f"Could not read load monitor: {r['error']}", {}

    c = r["current"]
    score   = c["load_score"]
    is_idle = c["is_idle"]

    verdict = "idle — safe for background tasks" if is_idle else "busy — background tasks are paused"
    lines = [
        f"System is currently {verdict}.",
        f"  CPU: {c['cpu_pct']}%   RAM: {c['ram_pct']}%   Disk I/O: {c['io_busy_pct']}%",
        f"  Load score: {score:.0f}/100  (tasks run when score < 25)",
    ]
    if c["recent_queries"] > 0:
        lines.append(f"  Recent assistant queries (last 8m): {c['recent_queries']} — treated as active use")
    if c["ollama_busy"]:
        lines.append("  Ollama is actively inferring — waiting for it to finish")

    p = r.get("pattern", {})
    obs = p.get("observations_total", 0)
    if obs < 100:
        lines.append(f"\n  Usage pattern: still building ({obs} observations so far, need ~100+)")
    else:
        idle_h = p.get("predicted_idle_hours", [])
        if idle_h:
            lines.append(f"\n  Predicted next idle hours: {', '.join(f'{h:02d}:xx' for h in idle_h)}")

    return "\n".join(lines), c


def _fmt_pattern() -> tuple[str, dict]:
    """Format the usage-pattern heatmap into a display block."""
    plines = _pattern_lines()
    text   = "Your 7-day system load pattern:\n" + "\n".join(plines)
    return text, {}


def _anomaly_lines(anomaly: dict) -> list[str]:
    """Format the latest anomaly-detection report into display lines."""
    n  = anomaly.get("alert_count", 0)
    at = (anomaly.get("checked_at") or "")[:16]
    if n == 0:
        return [f"Anomaly check ({at}): clean — {anomaly.get('events_checked', 0)} events checked, no alerts."]
    lines = [f"Anomaly check ({at}): {n} ALERT(S):"]
    lines += [f"  [{a['severity']}] {a['message']}" for a in anomaly.get("alerts", [])[:3]]
    return lines


def _cve_lines(vuln: dict) -> list[str]:
    """Format the latest CVE-scan report into display lines."""
    n  = vuln.get("total_vulns", 0)
    at = (vuln.get("scanned_at") or "")[:16]
    if n == 0:
        return [f"CVE scan ({at}): all {vuln.get('scanned_packages', '?')} packages clean."]
    sc = vuln.get("severity_counts", {})
    return [f"CVE scan ({at}): {n} vulnerability(-ies) found — "
            f"CRITICAL={sc.get('CRITICAL',0)} HIGH={sc.get('HIGH',0)} MEDIUM={sc.get('MEDIUM',0)}"]


def _threat_intel_lines(ti: dict) -> list[str]:
    """Format the latest threat-intel report into display lines."""
    n  = ti.get("vulnerabilities_found", 0)
    at = (ti.get("checked_at") or "")[:16]
    if n == 0:
        return [f"Threat intel ({at}): {ti.get('new_threats_tested', 0)} new threats tested — not vulnerable."]
    lines = [f"Threat intel ({at}): {n} VULNERABILITY(-IES) — MANUAL FIX REQUIRED:"]
    for v in ti.get("vulnerabilities", [])[:2]:
        th, res = v.get("threat", {}), v.get("result", {})
        lines.append(f"  [{th.get('severity')}] {th.get('id')}: {th.get('title','')[:60]}")
        lines.append(f"    Fix: {res.get('fix','')[:120]}")
    return lines


def _code_audit_lines(audit: dict) -> list[str]:
    """Format the latest code-audit report into a display line."""
    sc = audit.get("severity_counts", {})
    at = (audit.get("audited_at") or "")[:16]
    return [f"Code audit ({at}): {audit.get('total_issues', 0)} issue(s) — "
            f"CRITICAL={sc.get('CRITICAL',0)} HIGH={sc.get('HIGH',0)} "
            f"MEDIUM={sc.get('MEDIUM',0)} LOW={sc.get('LOW',0)}"]


def _fmt_guardian() -> tuple[str, dict]:
    """Format the latest guardian reports (anomaly, CVE, threat intel, code audit)."""
    builders = [
        ("anomaly_detect", _anomaly_lines),
        ("vuln_scan",      _cve_lines),
        ("threat_intel",   _threat_intel_lines),
        ("code_audit",     _code_audit_lines),
    ]
    lines: list[str] = []
    for name, builder in builders:
        report = _latest_report(name)
        if report:
            lines += builder(report)
    if not lines:
        lines = ["No guardian reports found yet. The guardian runs in the background — check back soon."]
    return "\n".join(lines), {}


def _fmt_schedule() -> tuple[str, dict]:
    """Format the guardian task schedule into a display block."""
    slines = _guardian_schedule_lines()
    text = "Guardian task schedule:\n" + "\n".join(slines)
    return text, {}


def _fmt_all() -> tuple[str, dict]:
    """Format the combined system + guardian + schedule view."""
    load_text,   ldata = _fmt_load()
    guard_text,  _     = _fmt_guardian()
    sched_text,  _     = _fmt_schedule()
    text = "\n\n".join([load_text, guard_text, sched_text])
    return text, ldata


# ── Module class ───────────────────────────────────────────────────────────────

class SystemModule(BaseModule):
    name        = "system"
    description = (
        "system load, CPU, RAM, idle, busy, load pattern, heatmap, "
        "security guardian, CVE scan, threat intel, anomaly, audit, "
        "background tasks, task schedule"
    )

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        """Route a system query to the matching formatter by intent."""
        intent = _intent(query)

        if intent == "load":
            text, data = _fmt_load()
        elif intent == "pattern":
            text, data = _fmt_pattern()
        elif intent == "guardian":
            text, data = _fmt_guardian()
        elif intent == "schedule":
            text, data = _fmt_schedule()
        else:
            text, data = _fmt_all()

        return ModuleResponse(
            text=text,
            module=self.name,
            data=data or None,
            follow_up=self._follow_up(intent),
        )

    def _follow_up(self, intent: str) -> str | None:
        """Return a context-appropriate follow-up suggestion for a system intent."""
        if intent == "load":
            return "Want to see the full 24-hour usage pattern?"
        if intent == "guardian":
            return "Want to see the task schedule and when each scan runs next?"
        return None
