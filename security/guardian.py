#!/usr/bin/env python3
"""
GK Security Guardian — background security monitor.

Continuously watches for:
  • CVEs in Python dependencies       (every 24h via OSV.dev)
  • Code vulnerabilities              (every 7 days via bandit + custom)
  • New LLM injection techniques      (every 6h via NVD + curated sources)
  • Log anomalies / injection attempts (every 1h)
  • Sensitive file permissions        (every 24h)

Auto-patches safe dependency upgrades (same-major-version, HIGH+ CVEs).
Saves reports to logs/security/.

Usage:
  # Run daemon (never exits):
  python security/guardian.py daemon

  # One-shot full scan + report:
  python security/guardian.py scan

  # Specific tasks:
  python security/guardian.py vuln       # dependency CVE scan only
  python security/guardian.py audit      # code audit only
  python security/guardian.py patterns   # update injection patterns
  python security/guardian.py anomaly    # check logs for anomalies
  python security/guardian.py patch      # auto-patch vulnerable packages
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).parent.parent
os.chdir(PROJECT)
sys.path.insert(0, str(PROJECT))

# ── Logging setup ─────────────────────────────────────────────────────────────

LOG_DIR = PROJECT / "logs" / "security"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "guardian.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("guardian")

# ── Scan schedules (seconds) ──────────────────────────────────────────────────

SCHEDULE = {
    "vuln_scan":       24 * 3600,   # dependency CVEs — daily
    "code_audit":       7 * 86400,  # code analysis — weekly
    "pattern_update":   6 * 3600,   # injection patterns — every 6h
    "anomaly_detect":       3600,   # log anomaly — hourly
}

_last_run: dict[str, float] = {k: 0.0 for k in SCHEDULE}


# ── Task implementations ──────────────────────────────────────────────────────

def task_vuln_scan(auto_patch: bool = True) -> dict:
    from security.scanner import scan_packages, scan_report
    from security.patcher import auto_patch_all

    log.info("=== CVE Scan (OSV.dev — worldwide threat intelligence) ===")
    vulns = scan_packages()
    report = scan_report(vulns)

    if vulns:
        log.warning(f"Found {len(vulns)} vulnerabilities: "
                    f"CRITICAL={report['severity_counts'].get('CRITICAL',0)} "
                    f"HIGH={report['severity_counts'].get('HIGH',0)} "
                    f"MEDIUM={report['severity_counts'].get('MEDIUM',0)}")
        for v in vulns:
            fix = f" → fix: {v.fix_version}" if v.fix_version else " (no fix)"
            log.warning(f"  [{v.severity}] {v.package} {v.version} — {v.vuln_id}{fix}")
    else:
        log.info("  No vulnerabilities found. All packages clean.")

    patch_result = {}
    if auto_patch and vulns:
        log.info("Auto-patching safe upgrades (HIGH+ CVEs, same-major version)...")
        patch_result = auto_patch_all(vulns, severity_threshold="HIGH")
        if patch_result["summary"]["patched"] > 0:
            log.info(f"  Patched {patch_result['summary']['patched']} package(s)")
        if patch_result["summary"]["manual_needed"] > 0:
            log.warning(f"  {patch_result['summary']['manual_needed']} package(s) need manual review")
            for m in patch_result["manual_review"]:
                log.warning(f"    {m['package']} {m.get('current','')} → {m.get('fix','')} (MANUAL)")

    combined = {**report, "patch_result": patch_result}
    _save_report("vuln_scan", combined)
    return combined


def task_code_audit() -> dict:
    from security.auditor import run_audit

    log.info("=== Code Security Audit ===")
    report = run_audit(auto_fix_permissions=True)

    total = report["total_issues"]
    counts = report["severity_counts"]
    log.info(f"  Issues: CRITICAL={counts.get('CRITICAL',0)} HIGH={counts.get('HIGH',0)} "
             f"MEDIUM={counts.get('MEDIUM',0)} LOW={counts.get('LOW',0)}")

    if report.get("fixed_permissions"):
        log.info(f"  Auto-fixed permissions: {', '.join(report['fixed_permissions'])}")
    if report.get("ollama_exposed"):
        log.warning(f"  !! OLLAMA EXPOSED: {report['ollama_details']}")

    for issue in report["issues"]:
        if issue.get("severity") in ("CRITICAL", "HIGH"):
            log.warning(f"  [{issue['severity']}] {issue.get('file','')}:{issue.get('line','')} — {issue['issue']}")

    _save_report("code_audit", report)
    return report


def task_pattern_update() -> dict:
    from security.watcher import update_patterns

    log.info("=== Injection Pattern Update ===")
    result = update_patterns()
    if result.get("status") == "updated":
        log.info(f"  Added {result['added']} new patterns (total: {result['total']})")
        # Reload patterns into sanitizer dynamically
        try:
            _reload_sanitizer_patterns()
        except Exception as e:
            log.warning(f"  Could not reload sanitizer patterns: {e}")
    else:
        log.info(f"  {result.get('reason', 'No update needed')}")
    _save_report("pattern_update", result)
    return result


def task_anomaly_detect() -> dict:
    from security.watcher import detect_anomalies

    result = detect_anomalies(hours=24)
    if result.get("alerts"):
        log.warning(f"=== SECURITY ALERTS: {result['alert_count']} ===")
        for alert in result["alerts"]:
            log.warning(f"  [{alert['severity']}] {alert['type']}: {alert['message']}")
    else:
        log.info(f"Anomaly check: clean ({result.get('events_checked', 0)} events checked)")
    _save_report("anomaly_detect", result)
    return result


def _reload_sanitizer_patterns() -> None:
    """Hot-reload injection patterns from patterns.json into the sanitizer."""
    import importlib
    from pathlib import Path
    import json
    import re

    patterns_file = PROJECT / "security" / "patterns.json"
    if not patterns_file.exists():
        return

    data = json.loads(patterns_file.read_text())
    new_patterns = [p["pattern"] for p in data.get("injection_patterns", [])]

    import core.sanitizer as san
    try:
        combined = "|".join(new_patterns)
        san._INJECTION_RE = re.compile(combined, re.IGNORECASE)
        log.info(f"  Sanitizer reloaded: {len(new_patterns)} patterns active")
    except Exception as e:
        log.error(f"  Failed to reload sanitizer: {e}")


# ── Report storage ────────────────────────────────────────────────────────────

def _save_report(task: str, data: dict) -> None:
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = LOG_DIR / f"{task}_{ts}.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    # Also write as "latest" for easy access
    latest = LOG_DIR / f"{task}_latest.json"
    latest.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _full_scan() -> dict:
    """Run all tasks once and return combined report."""
    started = datetime.now().isoformat()
    results = {}
    for task_name, fn in [
        ("anomaly_detect",  task_anomaly_detect),
        ("pattern_update",  task_pattern_update),
        ("vuln_scan",       lambda: task_vuln_scan(auto_patch=True)),
        ("code_audit",      task_code_audit),
    ]:
        try:
            results[task_name] = fn()
        except Exception as e:
            log.error(f"Task {task_name} failed: {e}")
            results[task_name] = {"error": str(e)}

    summary = {
        "scan_started": started,
        "scan_finished": datetime.now().isoformat(),
        "results": {
            k: {
                "total_vulns":   v.get("total_vulns"),
                "total_issues":  v.get("total_issues"),
                "alert_count":   v.get("alert_count"),
                "added_patterns":v.get("added"),
                "error":         v.get("error"),
            }
            for k, v in results.items()
        },
    }
    _save_report("full_scan", summary)

    print("\n" + "="*60)
    print(f"  GK Security Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*60)
    for task, res in results.items():
        if "error" in res:
            print(f"  {task:<20} ERROR: {res['error'][:50]}")
        elif task == "vuln_scan":
            n = res.get("total_vulns", 0)
            p = res.get("patch_result", {}).get("summary", {}).get("patched", 0)
            print(f"  {'Dependencies':<20} {n} CVEs found, {p} auto-patched")
        elif task == "code_audit":
            n = res.get("total_issues", 0)
            c = res.get("severity_counts", {})
            print(f"  {'Code':<20} {n} issues (CRIT={c.get('CRITICAL',0)} HIGH={c.get('HIGH',0)})")
        elif task == "anomaly_detect":
            n = res.get("alert_count", 0)
            print(f"  {'Anomalies':<20} {n} alert(s) in last 24h")
        elif task == "pattern_update":
            n = res.get("total", 0)
            a = res.get("added", 0)
            print(f"  {'Patterns':<20} {n} total (+{a} new)")
    print(f"\n  Reports: {LOG_DIR}")
    print("="*60)
    return summary


# ── Daemon loop ───────────────────────────────────────────────────────────────

def _is_due(task: str) -> bool:
    return time.monotonic() - _last_run[task] >= SCHEDULE[task]


def _mark_done(task: str) -> None:
    _last_run[task] = time.monotonic()


def run_daemon() -> None:
    log.info("="*60)
    log.info("GK Security Guardian — starting daemon")
    log.info(f"Schedules: vuln={SCHEDULE['vuln_scan']//3600}h  "
             f"code={SCHEDULE['code_audit']//3600}h  "
             f"patterns={SCHEDULE['pattern_update']//3600}h  "
             f"anomaly={SCHEDULE['anomaly_detect']//3600}h")
    log.info("="*60)

    # Run all tasks immediately on startup
    _full_scan()
    for k in _last_run:
        _last_run[k] = time.monotonic()

    while True:
        time.sleep(60)  # Check every minute which tasks are due

        if _is_due("anomaly_detect"):
            try:
                task_anomaly_detect()
            except Exception as e:
                log.error(f"anomaly_detect failed: {e}")
            _mark_done("anomaly_detect")

        if _is_due("pattern_update"):
            try:
                task_pattern_update()
            except Exception as e:
                log.error(f"pattern_update failed: {e}")
            _mark_done("pattern_update")

        if _is_due("vuln_scan"):
            try:
                task_vuln_scan(auto_patch=True)
            except Exception as e:
                log.error(f"vuln_scan failed: {e}")
            _mark_done("vuln_scan")

        if _is_due("code_audit"):
            try:
                task_code_audit()
            except Exception as e:
                log.error(f"code_audit failed: {e}")
            _mark_done("code_audit")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    dispatch = {
        "daemon":   run_daemon,
        "scan":     _full_scan,
        "vuln":     lambda: task_vuln_scan(auto_patch=("--patch" in sys.argv)),
        "audit":    task_code_audit,
        "patterns": task_pattern_update,
        "anomaly":  task_anomaly_detect,
        "patch":    lambda: task_vuln_scan(auto_patch=True),
    }

    fn = dispatch.get(cmd)
    if not fn:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(dispatch)}")
        sys.exit(1)

    fn()


if __name__ == "__main__":
    main()
