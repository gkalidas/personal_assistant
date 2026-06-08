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

from core.log import setup_security_logging
setup_security_logging()
log = logging.getLogger("guardian")

# ── Scan schedules (seconds) ──────────────────────────────────────────────────

# Minimum interval between runs (seconds). Tasks run only when BOTH:
#   1. min_interval has elapsed since the last run
#   2. the system is currently idle (CPU/RAM/queries below thresholds)
# If the system stays busy longer than 3× the interval, the task runs anyway
# (at the next idle moment) so nothing is skipped indefinitely.
MIN_INTERVAL = {
    "anomaly_detect":       3600,   # at most once/h — quick check, low load
    "pattern_update":   6 * 3600,   # every 6h — light NVD + file write
    "threat_intel":    12 * 3600,   # every 12h — network + sanitizer tests
    "vuln_scan":       24 * 3600,   # daily — OSV batch query
    "code_audit":       7 * 86400,  # weekly — bandit + regex scan
}

# Cool-down between back-to-back tasks (seconds).
# One task finishes → wait this long before starting the next one.
TASK_COOLDOWN = 3 * 60   # 3 minutes

# Maximum overdue multiplier before forcing a run regardless of load.
MAX_OVERDUE   = 3.0      # 3× interval = forced run (e.g. anomaly at 3h, vuln at 72h)

_last_run: dict[str, float] = {k: 0.0 for k in MIN_INTERVAL}

# Persist last-run timestamps so other processes (SystemModule) can read them
_STATE_FILE = LOG_DIR / "guardian_state.json"


def _load_state() -> None:
    """Load persisted last-run wall-clock times back to monotonic offsets."""
    if not _STATE_FILE.exists():
        return
    try:
        data = json.loads(_STATE_FILE.read_text())
        now_wall = datetime.now().timestamp()
        now_mono = time.monotonic()
        for task, wall_ts in data.get("last_run_wall", {}).items():
            if task in _last_run:
                age = now_wall - wall_ts           # seconds since last run
                _last_run[task] = now_mono - age   # convert to monotonic
    except Exception as e:
        log.debug(f"Could not load guardian state: {e}")


def _save_state() -> None:
    """Persist current last-run times as wall-clock timestamps."""
    now_wall = datetime.now().timestamp()
    now_mono = time.monotonic()
    wall_times = {
        task: now_wall - (now_mono - mono_ts)
        for task, mono_ts in _last_run.items()
        if mono_ts > 0.0
    }
    try:
        _STATE_FILE.write_text(json.dumps({
            "last_run_wall": wall_times,
            "updated_at":    datetime.now().isoformat(),
        }, indent=2))
    except Exception as e:
        log.debug(f"Could not save guardian state: {e}")


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


def task_threat_intel() -> dict:
    from security.threat_intel import run_threat_intel

    log.info("=== Threat Intelligence — AI/LLM Attack News & Replication ===")
    result = run_threat_intel(hours=48)

    n = result.get("vulnerabilities_found", 0)
    tested = result.get("new_threats_tested", 0)
    fetched = result.get("total_fetched", 0)

    if n > 0:
        log.warning(f"  !! {n} VULNERABILITY(-IES) FOUND — manual fix required")
        for alert in result.get("alerts", []):
            log.warning(alert)
    else:
        log.info(f"  {tested} new threats tested ({fetched} fetched) — not vulnerable")

    _save_report("threat_intel", result)
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
        ("threat_intel",    task_threat_intel),
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
        elif task == "threat_intel":
            n  = res.get("vulnerabilities_found", 0)
            t  = res.get("new_threats_tested", 0)
            src = res.get("sources", 4)
            flag = " !! REQUIRES MANUAL FIX" if n > 0 else ""
            print(f"  {'Threat Intel':<20} {t} threats tested, {n} vuln(s) found [{src} sources]{flag}")
    print(f"\n  Reports: {LOG_DIR}")
    print("="*60)
    return summary


# ── Idle-aware scheduler ──────────────────────────────────────────────────────

def _overdue_ratio(task: str) -> float:
    """How overdue is this task?  1.0 = just due, 2.0 = twice overdue, etc."""
    elapsed = time.monotonic() - _last_run[task]
    return elapsed / MIN_INTERVAL[task]


def _pick_next_task() -> str | None:
    """
    Return the name of the task to run next, or None if nothing is due.

    Selection rules:
      1. Task must have elapsed >= its MIN_INTERVAL (overdue_ratio >= 1.0)
      2. Among eligible tasks, pick the most overdue (highest ratio)
      3. If overdue_ratio >= MAX_OVERDUE → eligible even if system is busy
         (caller decides whether to honour that)
    """
    due = [(name, _overdue_ratio(name)) for name in MIN_INTERVAL]
    due = [(n, r) for n, r in due if r >= 1.0]
    if not due:
        return None
    due.sort(key=lambda x: -x[1])   # most overdue first
    return due[0][0]


def _forced_task() -> str | None:
    """
    Return a task that is so overdue it must run regardless of system load,
    or None. This prevents tasks from being skipped forever on a busy machine.
    """
    for name, ratio in sorted(
        [(n, _overdue_ratio(n)) for n in MIN_INTERVAL],
        key=lambda x: -x[1],
    ):
        if ratio >= MAX_OVERDUE:
            return name
    return None


def _run_one(name: str) -> None:
    """Run a single named task and record its completion time."""
    fn_map = {
        "anomaly_detect":  task_anomaly_detect,
        "pattern_update":  task_pattern_update,
        "threat_intel":    task_threat_intel,
        "vuln_scan":       lambda: task_vuln_scan(auto_patch=True),
        "code_audit":      task_code_audit,
    }
    fn = fn_map.get(name)
    if fn is None:
        return
    log.info(f"[scheduler] Starting: {name}")
    try:
        fn()
    except Exception as e:
        log.error(f"[scheduler] {name} failed: {e}")
    _last_run[name] = time.monotonic()
    _save_state()   # persist so SystemModule can read actual run times
    log.info(f"[scheduler] Done: {name}  (cooldown {TASK_COOLDOWN//60}m before next task)")


def run_daemon() -> None:
    from security.load_monitor import observe, is_idle, predicted_idle_hours

    _load_state()   # restore last-run times from previous daemon run

    log.info("=" * 60)
    log.info("GK Security Guardian — idle-aware scheduler starting")
    log.info("  Tasks run ONE AT A TIME, only when system is idle.")
    log.info("  Minimum intervals:")
    for name, secs in MIN_INTERVAL.items():
        h = secs // 3600
        d = h // 24
        label = f"{d}d" if d >= 1 else f"{h}h"
        log.info(f"    {name:<20} every {label}  (forced after {label}×{MAX_OVERDUE:.0f})")
    log.info(f"  Cool-down between tasks: {TASK_COOLDOWN // 60}m")
    log.info("=" * 60)

    # First run: anomaly + pattern only (lightest tasks) so startup is fast
    for name in ("anomaly_detect", "pattern_update"):
        try:
            _run_one(name)
        except Exception as e:
            log.error(f"Startup task {name} failed: {e}")

    last_task_time = time.monotonic()   # tracks when we last finished a task
    last_log_busy  = 0.0               # throttle "system busy" log messages

    while True:
        time.sleep(60)

        # Always observe load (builds the usage pattern database)
        try:
            sample = observe()
            score  = sample["load_score"]
            idle   = sample["load_score"] < 25 and sample["recent_queries"] == 0
        except Exception:
            idle  = False
            score = 0.0

        # Check for a forced task (critically overdue regardless of load)
        forced = _forced_task()
        if forced:
            log.warning(
                f"[scheduler] Task '{forced}' is >{MAX_OVERDUE:.0f}× overdue — "
                f"running despite load (score={score:.0f})"
            )
            _run_one(forced)
            last_task_time = time.monotonic()
            continue

        # Enforce cooldown between tasks
        since_last = time.monotonic() - last_task_time
        if since_last < TASK_COOLDOWN:
            continue

        # Only proceed if system is idle
        if not idle:
            now = time.monotonic()
            if now - last_log_busy > 600:   # log at most every 10 minutes
                next_h = predicted_idle_hours(n=2)
                hint   = f"  Next predicted idle window: {next_h}" if next_h else ""
                log.info(f"[scheduler] System busy (score={score:.0f}) — tasks paused.{hint}")
                last_log_busy = now
            continue

        # System is idle — pick the most overdue task
        next_task = _pick_next_task()
        if next_task:
            log.info(f"[scheduler] System idle (score={score:.0f}) — running: {next_task}")
            _run_one(next_task)
            last_task_time = time.monotonic()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    def _cmd_load():
        from security.load_monitor import report, print_pattern
        r = report()
        c = r["current"]
        p = r["pattern"]
        print(f"\n  Current load:  CPU={c['cpu_pct']}%  RAM={c['ram_pct']}%  "
              f"IO={c['io_busy_pct']}%  score={c['load_score']}")
        print(f"  Recent queries: {c['recent_queries']}   "
              f"Ollama busy: {c['ollama_busy']}   "
              f"Idle now: {c['is_idle']}")
        print(f"\n  Pattern data:  {p['observations_total']} observations "
              f"since {p['observing_since']}  ({p['data_quality']})")
        print(f"  Predicted idle hours: {p['predicted_idle_hours']}")
        print_pattern()

    dispatch = {
        "daemon":   run_daemon,
        "scan":     _full_scan,
        "vuln":     lambda: task_vuln_scan(auto_patch=("--patch" in sys.argv)),
        "audit":    task_code_audit,
        "patterns": task_pattern_update,
        "anomaly":  task_anomaly_detect,
        "patch":    lambda: task_vuln_scan(auto_patch=True),
        "intel":    task_threat_intel,
        "load":     _cmd_load,        # show current load + usage pattern heatmap
    }

    fn = dispatch.get(cmd)
    if not fn:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(dispatch)}")
        sys.exit(1)

    fn()


if __name__ == "__main__":
    main()
