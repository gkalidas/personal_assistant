"""Reads guardian security reports from logs/security/ for dashboard display."""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _extract_pattern(fix_text: str) -> dict | None:
    """Pull the JSON pattern dict out of a threat intel fix instruction string."""
    m = re.search(r'(\{[^}]+\})', fix_text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None

_LOG_DIR = Path(__file__).parent.parent / "logs" / "security"

_INTERVALS = {
    "anomaly_detect":   3600,
    "pattern_update":   6 * 3600,
    "threat_intel":    12 * 3600,
    "vuln_scan":       24 * 3600,
    "code_audit":       7 * 86400,
}


def _read(name: str) -> dict:
    """Load logs/security/<name>_latest.json, or {} if missing/unreadable."""
    path = _LOG_DIR / f"{name}_latest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _state() -> dict:
    """Load the guardian daemon's persisted state file, or {}."""
    path = _LOG_DIR / "guardian_state.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _overall_health(alerts: int, threat_vulns: int, critical: int,
                    vuln_count: int, audit_issues: int) -> str:
    """Roll up findings into 'danger' / 'warn' / 'ok' for the dashboard badge."""
    if alerts or threat_vulns or critical:
        return "danger"
    if vuln_count or audit_issues:
        return "warn"
    return "ok"


def _build_schedule(last_wall: dict, now: float) -> list[dict]:
    """Build the per-task schedule rows (last run + next-run status) for display."""
    rows = []
    for task, interval in _INTERVALS.items():
        wall_ts = last_wall.get(task, 0.0)
        if wall_ts == 0.0:
            rows.append({"task": task, "last": "never",
                         "next": "OVERDUE — never run", "next_status": "danger"})
            continue
        elapsed = now - wall_ts
        m = int(elapsed / 60)
        last_label = f"{m}m ago" if m < 60 else f"{m//60}h ago"
        ratio = elapsed / interval
        if ratio >= 3.0:
            nxt, status = f"OVERDUE ({ratio:.0f}×)", "danger"
        elif ratio >= 1.0:
            nxt, status = "DUE — waiting idle", "warn"
        else:
            nxt, status = f"in ~{int((interval - elapsed) / 60)}m", "ok"
        rows.append({"task": task, "last": last_label, "next": nxt, "next_status": status})
    return rows


def _threat_items(threat: dict) -> list[dict]:
    """Flatten threat-intel vulnerabilities into display items (with fix pattern)."""
    return [
        {
            "id":       v.get("threat", {}).get("id", ""),
            "title":    v.get("threat", {}).get("title", ""),
            "type":     v.get("threat", {}).get("threat_type", "general"),
            "severity": v.get("threat", {}).get("severity", "INFO"),
            "evidence": v.get("result", {}).get("evidence", ""),
            "details":  v.get("result", {}).get("details", ""),
            "pattern":  _extract_pattern(v.get("result", {}).get("fix", "")),
        }
        for v in threat.get("vulnerabilities", [])
    ]


def _vuln_items(vuln: dict) -> list[dict]:
    """Flatten the CVE/advisory scan into compact display items for the modal."""
    return [
        {
            "package":  v.get("package", ""),
            "version":  v.get("version") or v.get("installed_version", ""),
            "id":       v.get("vuln_id") or (v.get("aliases") or [""])[0] or "",
            "severity": v.get("severity", "UNKNOWN"),
            "fix":      v.get("fix_version") or v.get("fixed_version") or "",
            "summary":  (v.get("summary") or "").replace("No summary", ""),
        }
        for v in vuln.get("vulnerabilities", [])
    ]


def get_guardian_status() -> dict[str, Any]:
    """Aggregate the latest guardian reports + schedule into one dashboard dict."""
    anomaly, vuln = _read("anomaly_detect"), _read("vuln_scan")
    threat, audit = _read("threat_intel"), _read("code_audit")
    state = _state()

    alert_count  = anomaly.get("alert_count", 0)
    vuln_count   = vuln.get("total_vulns", 0)
    threat_vulns = threat.get("vulnerabilities_found", 0)
    audit_issues = audit.get("total_issues", 0)
    audit_sev    = audit.get("severity_counts", {})
    critical     = audit_sev.get("CRITICAL", 0) + audit_sev.get("HIGH", 0)

    return {
        "overall":        _overall_health(alert_count, threat_vulns, critical, vuln_count, audit_issues),
        "alert_count":    alert_count,
        "vuln_count":     vuln_count,
        "threat_vulns":   threat_vulns,
        "audit_issues":   audit_issues,
        "audit_critical": critical,
        "anomaly": {"at": (anomaly.get("checked_at") or "")[:16],
                    "alerts": alert_count, "checked": anomaly.get("events_checked", 0)},
        "vuln": {"at": (vuln.get("scanned_at") or "")[:16], "total": vuln_count,
                 "packages": vuln.get("scanned_packages", "?"),
                 "severity": vuln.get("severity_counts", {}),
                 "items": _vuln_items(vuln)},
        "threat": {"at": (threat.get("checked_at") or "")[:16],
                   "tested": threat.get("new_threats_tested", 0),
                   "vulns": threat_vulns, "items": _threat_items(threat)},
        "audit": {"at": (audit.get("audited_at") or "")[:16], "total": audit_issues,
                  "severity": audit_sev, "issues": audit.get("issues", [])[:5]},
        "schedule": _build_schedule(state.get("last_run_wall", {}), time.time()),
        "updated_at": state.get("updated_at", "")[:16],
    }
