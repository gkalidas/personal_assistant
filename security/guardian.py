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
    "red_team":        24 * 3600,   # daily — input-side attack simulation
    "jailbreak":       24 * 3600,   # daily — output-side jail containment test
    "source_intel":    24 * 3600,   # daily — LLM-security feeds → safe auto-harden
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
    now_wall = datetime.now().timestamp()
    now_mono = time.monotonic()
    known: set[str] = set()
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text())
            for task, wall_ts in data.get("last_run_wall", {}).items():
                if task in _last_run:
                    age = now_wall - wall_ts           # seconds since last run
                    _last_run[task] = now_mono - age   # convert to monotonic
                    known.add(task)
        except Exception as e:
            log.debug(f"Could not load guardian state: {e}")
    # Tasks with no saved timestamp have never run — treat as immediately overdue
    # so the scheduler picks them up on the next idle window, not after N days of uptime.
    for task in MIN_INTERVAL:
        if task not in known:
            _last_run[task] = now_mono - MIN_INTERVAL[task] * (MAX_OVERDUE + 1)


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

    """Scan dependencies for CVEs (OSV.dev), optionally auto-patch, and save the report."""
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


    """Run the code security audit (bandit + custom checks) and save the report."""
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


    """Refresh injection patterns from intel sources and hot-reload the sanitizer."""
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


    """Fetch recent AI/LLM attack intel, replicate against our defenses, and save the report."""
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


def task_red_team() -> dict:
    """Run the red team, auto-defend any bypasses, then jailbreak-sim and posture.

    If attacks bypass the sanitizer, runs autodefense and re-runs the red team
    to confirm the fixes. Returns combined attack/jailbreak/posture stats.
    """
    from security.red_team      import run_red_team, save_report
    from security.autodefense   import run_autodefense
    from security.posture       import calculate_posture

    log.info("=== Red Team Simulation + Auto-Defense ===")
    report = run_red_team(llm=False, verbose=False)
    path   = save_report(report)
    log.info(f"  {report.blocked}/{report.total_attacks} attacks blocked "
             f"({report.bypass_rate_pct:.0f}% bypass rate)")

    if report.bypassed > 0:
        log.warning(f"  !! {report.bypassed} bypass(es) found — running auto-defense")
        rt_data = json.loads(path.read_text())
        run_autodefense(red_team_report=rt_data, dry_run=False, verbose=False)
        # Re-run to confirm fixes
        report2 = run_red_team(llm=False, verbose=False)
        save_report(report2)
        log.info(f"  After auto-defense: {report2.blocked}/{report2.total_attacks} blocked")
    else:
        log.info("  All attacks blocked — no auto-defense needed")

    # Run jailbreak sim then compute fresh posture
    jb_results = task_jailbreak(quiet=True)
    posture = calculate_posture(verbose=False)
    log.info(f"  Posture score: {posture.overall}/100 Grade {posture.grade}")

    return {
        "total_attacks":   report.total_attacks,
        "blocked":         report.blocked,
        "bypassed":        report.bypassed,
        "bypass_rate_pct": report.bypass_rate_pct,
        "jailbreak_escaped": jb_results.get("escaped", 0),
        "posture_score":   posture.overall,
        "posture_grade":   posture.grade,
    }


def task_source_intel() -> dict:
    """Read LLM-security feeds, reproduce bypasses, and safely self-harden.

    Tier-1/2 reproduced bypasses flow through autodefense (validated,
    rate-limited, rollback-guarded); the rest go to the review queue.
    """
    from security.source_intel import run_source_intel
    log.info("=== Source Intel — LLM-security feeds → self-hardening ===")
    summary = run_source_intel(dry_run=False, verbose=False)
    log.info("  fetched=%d reproduced=%d auto_promoted=%d review_queued=%d",
             summary.get("items_fetched", 0), summary.get("reproduced_bypasses", 0),
             summary.get("auto_promoted", 0), summary.get("review_queued", 0))
    _save_report("source_intel", summary)
    return summary


def task_jailbreak(quiet: bool = False) -> dict:
    """Run the output-side jailbreak simulation and return contained/escaped counts."""
    from security.jailbreak_sim import run_jailbreak_sim
    log.info("=== Jailbreak Simulation (output-side jail) ===")
    results  = run_jailbreak_sim(llm=False, verbose=not quiet)
    contained = sum(1 for r in results if r.contained)
    escaped   = sum(1 for r in results if not r.contained)
    log.info(f"  {contained}/{len(results)} contained, {escaped} escaped")
    if escaped > 0:
        log.warning(f"  !! {escaped} jail escape(s) detected")
    return {"total": len(results), "contained": contained, "escaped": escaped}


def task_anomaly_detect() -> dict:


    """Scan the last 24h of events for anomalies and save the report."""
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
    """Write a task report to logs/security/<task>_{ts}.json and <task>_latest.json."""
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = LOG_DIR / f"{task}_{ts}.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    # Also write as "latest" for easy access
    latest = LOG_DIR / f"{task}_latest.json"
    latest.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# Tasks run by a full scan, in order (lightest first).
_FULL_SCAN_TASKS = [
    ("anomaly_detect",  task_anomaly_detect),
    ("pattern_update",  task_pattern_update),
    ("red_team",        task_red_team),
    ("jailbreak",       task_jailbreak),
    ("source_intel",    task_source_intel),
    ("vuln_scan",       lambda: task_vuln_scan(auto_patch=True)),
    ("code_audit",      task_code_audit),
    ("threat_intel",    task_threat_intel),
]


def _print_scan_report(results: dict) -> None:
    """Print the human-readable full-scan summary table to stdout."""
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
            print(f"  {'Anomalies':<20} {res.get('alert_count', 0)} alert(s) in last 24h")
        elif task == "pattern_update":
            print(f"  {'Patterns':<20} {res.get('total', 0)} total (+{res.get('added', 0)} new)")
        elif task == "threat_intel":
            n  = res.get("vulnerabilities_found", 0)
            t  = res.get("new_threats_tested", 0)
            flag = " !! REQUIRES MANUAL FIX" if n > 0 else ""
            print(f"  {'Threat Intel':<20} {t} threats tested, {n} vuln(s) found [{res.get('sources', 4)} sources]{flag}")
    print(f"\n  Reports: {LOG_DIR}")
    print("="*60)


def _full_scan() -> dict:
    """Run every guardian task once, print a summary, and return the combined report."""
    started = datetime.now().isoformat()
    results = {}
    for task_name, fn in _FULL_SCAN_TASKS:
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
    _print_scan_report(results)
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
        "red_team":        task_red_team,
        "jailbreak":       task_jailbreak,
        "source_intel":    task_source_intel,
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


def _log_startup_banner() -> None:
    """Log the daemon banner and the configured task intervals."""
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


def _run_startup_tasks() -> None:
    """Run the lightest tasks once at startup so the daemon comes up fast."""
    for name in ("anomaly_detect", "pattern_update"):
        try:
            _run_one(name)
        except Exception as e:
            log.error(f"Startup task {name} failed: {e}")


def run_daemon() -> None:
    """Idle-aware scheduler loop: observe load every 60s and run due tasks.

    Runs forever. Forces critically-overdue tasks regardless of load; otherwise
    runs the most-overdue task only when the system is idle and the inter-task
    cooldown has elapsed.
    """
    from security.load_monitor import observe, is_idle, predicted_idle_hours

    _load_state()   # restore last-run times from previous daemon run
    _log_startup_banner()
    _run_startup_tasks()

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

def _cmd_load():
    """CLI: print the current load snapshot and usage-pattern heatmap."""
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


def _cmd_redteam():
    """CLI: run the red-team simulation and save its report."""
    from security.red_team import run_red_team, save_report
    r = run_red_team(llm="--llm" in sys.argv, verbose=True)
    save_report(r)


def _cmd_autodefense():
    """CLI: run autodefense over the latest red-team bypasses."""
    from security.autodefense import run_autodefense
    run_autodefense(dry_run="--dry-run" in sys.argv, verbose=True)


def _cmd_posture():
    """CLI: compute and print the security posture score."""
    from security.posture import calculate_posture
    calculate_posture(verbose=True)


def _cmd_jailbreak():
    """CLI: run the jailbreak simulation (output-side jail)."""
    from security.jailbreak_sim import run_jailbreak_sim
    run_jailbreak_sim(llm="--llm" in sys.argv, verbose=True)


def _build_dispatch() -> dict:
    """Map CLI command names to their handlers."""
    return {
        "daemon":      run_daemon,
        "scan":        _full_scan,
        "vuln":        lambda: task_vuln_scan(auto_patch=("--patch" in sys.argv)),
        "audit":       lambda: (_load_state(), _run_one("code_audit")),
        "patterns":    task_pattern_update,
        "anomaly":     task_anomaly_detect,
        "patch":       lambda: task_vuln_scan(auto_patch=True),
        "intel":       task_threat_intel,
        "redteam":     _cmd_redteam,       # input-side attack simulation
        "jailbreak":   _cmd_jailbreak,     # output-side jail simulation
        "sourceintel": lambda: __import__("security.source_intel", fromlist=["run_source_intel"]).run_source_intel(dry_run="--dry-run" in sys.argv, verbose=True),
        "autodefense": _cmd_autodefense,   # auto-fix bypasses from red team
        "posture":     _cmd_posture,       # show security score
        "load":        _cmd_load,
    }


def main() -> None:
    """CLI entry point: dispatch the sub-command (default 'scan')."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    dispatch = _build_dispatch()
    fn = dispatch.get(cmd)
    if not fn:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(dispatch)}")
        sys.exit(1)
    fn()


if __name__ == "__main__":
    main()
