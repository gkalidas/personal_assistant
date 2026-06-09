"""Reads guardian security reports from logs/security/ for dashboard display."""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_LOG_DIR = Path(__file__).parent.parent / "logs" / "security"

_INTERVALS = {
    "anomaly_detect":   3600,
    "pattern_update":   6 * 3600,
    "threat_intel":    12 * 3600,
    "vuln_scan":       24 * 3600,
    "code_audit":       7 * 86400,
}


def _read(name: str) -> dict:
    path = _LOG_DIR / f"{name}_latest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _state() -> dict:
    path = _LOG_DIR / "guardian_state.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def get_guardian_status() -> dict[str, Any]:
    anomaly    = _read("anomaly_detect")
    vuln       = _read("vuln_scan")
    threat     = _read("threat_intel")
    audit      = _read("code_audit")
    state      = _state()
    last_wall  = state.get("last_run_wall", {})
    now        = time.time()

    # Overall health: any alerts / vulns / critical issues?
    alert_count = anomaly.get("alert_count", 0)
    vuln_count  = vuln.get("total_vulns", 0)
    threat_vulns= threat.get("vulnerabilities_found", 0)
    audit_issues= audit.get("total_issues", 0)
    audit_sev   = audit.get("severity_counts", {})
    critical    = audit_sev.get("CRITICAL", 0) + audit_sev.get("HIGH", 0)

    if alert_count or threat_vulns or critical:
        overall = "danger"
    elif vuln_count or audit_issues:
        overall = "warn"
    else:
        overall = "ok"

    # Schedule rows
    schedule = []
    for task, interval in _INTERVALS.items():
        wall_ts = last_wall.get(task, 0.0)
        if wall_ts == 0.0:
            elapsed = now
            last_label = "never"
        else:
            elapsed = now - wall_ts
            m = int(elapsed / 60)
            last_label = f"{m}m ago" if m < 60 else f"{m//60}h ago"

        ratio = elapsed / interval
        if ratio >= 3.0:
            next_label = f"OVERDUE ({ratio:.0f}×)"
            next_status = "danger"
        elif ratio >= 1.0:
            next_label = "DUE — waiting idle"
            next_status = "warn"
        else:
            remain_m = int((interval - elapsed) / 60)
            next_label = f"in ~{remain_m}m"
            next_status = "ok"

        schedule.append({
            "task":        task,
            "last":        last_label,
            "next":        next_label,
            "next_status": next_status,
        })

    return {
        "overall":       overall,
        "alert_count":   alert_count,
        "vuln_count":    vuln_count,
        "threat_vulns":  threat_vulns,
        "audit_issues":  audit_issues,
        "audit_critical":critical,
        # compact scan summaries
        "anomaly": {
            "at":      (anomaly.get("checked_at") or "")[:16],
            "alerts":  alert_count,
            "checked": anomaly.get("events_checked", 0),
        },
        "vuln": {
            "at":       (vuln.get("scanned_at") or "")[:16],
            "total":    vuln_count,
            "packages": vuln.get("scanned_packages", "?"),
            "severity": vuln.get("severity_counts", {}),
        },
        "threat": {
            "at":     (threat.get("checked_at") or "")[:16],
            "tested": threat.get("new_threats_tested", 0),
            "vulns":  threat_vulns,
        },
        "audit": {
            "at":       (audit.get("audited_at") or "")[:16],
            "total":    audit_issues,
            "severity": audit_sev,
            "issues":   audit.get("issues", [])[:5],
        },
        "schedule": schedule,
        "updated_at": state.get("updated_at", "")[:16],
    }
