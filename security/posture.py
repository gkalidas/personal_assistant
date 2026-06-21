#!/usr/bin/env python3
"""
Security Posture Score — single 0-100 metric for how well-protected the PA is.

Reads the latest report from each guardian task and computes a weighted score.
Grade: A (90+), B (75+), C (60+), D (45+), F (<45)

Usage:
    python security/posture.py          # print score + breakdown
    python security/posture.py --json   # output JSON only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

LOG_DIR = PROJECT / "logs" / "security"
log     = logging.getLogger("security.posture")


@dataclass
class PostureComponent:
    name:        str
    score:       float      # 0–100 contribution after weighting
    weight:      float      # how much this component counts (0–1, sum = 1)
    raw_score:   float      # component's own 0–100 before weight
    detail:      str        # human-readable explanation
    stale:       bool       # True if last report is old
    last_run:    str        # ISO timestamp or "never"


@dataclass
class PostureReport:
    overall:     float
    grade:       str
    components:  list[PostureComponent]
    computed_at: str
    alerts:      list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


# ── Component weights ──────────────────────────────────────────────────────────
WEIGHTS = {
    "cve":         0.20,   # dependency vulnerabilities
    "code_audit":  0.15,   # bandit + custom code issues
    "red_team":    0.25,   # input-side attack simulation bypass rate
    "jailbreak":   0.20,   # output-side jail containment (new)
    "patterns":    0.10,   # injection pattern freshness and count
    "anomaly":     0.10,   # recent log anomaly alerts
}


def _read_latest(name: str) -> dict | None:
    path = LOG_DIR / f"{name}_latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _is_stale(data: dict | None, max_hours: int) -> tuple[bool, str]:
    """Return (is_stale, last_run_str)."""
    if data is None:
        return True, "never"
    # Try common timestamp fields
    for field in ("run_at", "checked_at", "updated_at", "scan_started"):
        ts_raw = data.get(field, "")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw)
                age_h = (datetime.now() - ts).total_seconds() / 3600
                return age_h > max_hours, ts.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
    return True, "unknown"


# ── Per-component scorers ──────────────────────────────────────────────────────

def _score_cve(data: dict | None) -> tuple[float, str]:
    if data is None:
        return 40.0, "No CVE scan run yet — assume vulnerable"

    total = data.get("total_vulns", 0)
    if total == 0:
        return 100.0, "No CVEs found in dependencies"

    counts = data.get("severity_counts", {})
    crit   = counts.get("CRITICAL", 0)
    high   = counts.get("HIGH", 0)
    med    = counts.get("MEDIUM", 0)
    low    = counts.get("LOW", 0)

    # Check if anything was auto-patched
    patched = data.get("patch_result", {}).get("summary", {}).get("patched", 0)

    penalty = crit * 30 + high * 12 + med * 4 + low * 1
    penalty = min(penalty, 80)   # cap at -80 so base never goes below 20
    score   = max(20.0, 100.0 - penalty)

    detail = f"{total} CVEs (CRIT={crit} HIGH={high} MED={med} LOW={low})"
    if patched > 0:
        score = min(100.0, score + patched * 5)
        detail += f"; {patched} auto-patched"

    return round(score, 1), detail


def _score_code_audit(data: dict | None) -> tuple[float, str]:
    if data is None:
        return 50.0, "No code audit run yet"

    total  = data.get("total_issues", 0)
    counts = data.get("severity_counts", {})
    crit   = counts.get("CRITICAL", 0)
    high   = counts.get("HIGH", 0)
    med    = counts.get("MEDIUM", 0)
    low    = counts.get("LOW", 0)

    if total == 0:
        return 100.0, "No code issues found"

    penalty = crit * 25 + high * 10 + med * 3 + low * 0.5
    penalty = min(penalty, 75)
    score   = max(25.0, 100.0 - penalty)
    detail  = f"{total} code issues (CRIT={crit} HIGH={high} MED={med} LOW={low})"

    if data.get("ollama_exposed"):
        score   = max(0.0, score - 20)
        detail += " + OLLAMA EXPOSED"

    return round(score, 1), detail


def _score_red_team(data: dict | None) -> tuple[float, str]:
    if data is None:
        return 50.0, "No red team run yet — unknown attack surface"

    total    = data.get("total_attacks", 0)
    bypassed = data.get("bypassed", 0)
    indirect = data.get("indirect_gap", False)

    if total == 0:
        return 50.0, "Red team ran but found no attacks to test"

    # Core bypass rate penalty
    bypass_pct = bypassed / total * 100
    score = max(10.0, 100.0 - bypass_pct * 1.5)

    # Extra penalty per bypass by severity
    for b in data.get("bypasses", []):
        sev_penalty = {"critical": 8, "high": 5, "medium": 2, "low": 1}.get(b.get("severity", "low"), 1)
        score = max(0.0, score - sev_penalty)

    if indirect:
        score = max(0.0, score - 10)

    detail = (
        f"{total - bypassed}/{total} attacks blocked "
        f"({bypass_pct:.0f}% bypass rate)"
    )
    if indirect:
        detail += "; indirect injection gap"

    return round(score, 1), detail


def _score_jailbreak(data: dict | None) -> tuple[float, str]:
    if data is None:
        return 50.0, "No jailbreak simulation run yet"

    total     = data.get("total", 0)
    escaped   = data.get("escaped", 0)
    contained = data.get("contained", 0)

    if total == 0:
        return 50.0, "Jailbreak sim ran but no tests found"

    escape_pct = escaped / total * 100
    score = max(10.0, 100.0 - escape_pct * 3)   # each escape costs 3 points per %
    detail = f"{contained}/{total} contained ({escape_pct:.0f}% escape rate)"
    return round(score, 1), detail


def _score_patterns(data: dict | None) -> tuple[float, str]:
    if data is None:
        # Try reading patterns.json directly
        pf = PROJECT / "security" / "patterns.json"
        if pf.exists():
            try:
                pd = json.loads(pf.read_text())
                n  = len(pd.get("injection_patterns", []))
                last = pd.get("_meta", {}).get("last_updated", "unknown")
                score = min(100.0, 60 + n * 0.5)
                return round(score, 1), f"{n} patterns (last updated: {last})"
            except Exception:
                pass
        return 30.0, "No pattern update data"

    total = data.get("total", 0)
    added = data.get("added", 0)
    score = min(100.0, 50 + total * 0.5 + added * 2)
    detail = f"{total} patterns total (+{added} recently)"
    return round(score, 1), detail


def _score_anomaly(data: dict | None) -> tuple[float, str]:
    if data is None:
        return 60.0, "No anomaly detection run yet"

    alerts = data.get("alert_count", 0)
    events = data.get("events_checked", 0)

    if alerts == 0:
        return 100.0, f"Clean — {events} events checked, no anomalies"

    severity_weight = 0
    for a in data.get("alerts", []):
        sev = a.get("severity", "LOW")
        severity_weight += {"CRITICAL": 20, "HIGH": 12, "MEDIUM": 6, "LOW": 2}.get(sev, 2)

    score  = max(20.0, 100.0 - severity_weight)
    detail = f"{alerts} alert(s) in last 24h"
    return round(score, 1), detail


# ── Main calculator ────────────────────────────────────────────────────────────

def calculate_posture(verbose: bool = True) -> PostureReport:
    computed_at = datetime.now().isoformat()
    components  = []
    alerts      = []
    recommendations = []

    # ── CVE ──────────────────────────────────────────────────────────────────
    cve_data  = _read_latest("vuln_scan")
    stale, lr = _is_stale(cve_data, max_hours=48)
    raw, det  = _score_cve(cve_data)
    components.append(PostureComponent(
        name="CVE Scan", score=raw * WEIGHTS["cve"], weight=WEIGHTS["cve"],
        raw_score=raw, detail=det, stale=stale, last_run=lr
    ))
    if stale:
        recommendations.append("Run `python security/guardian.py vuln` — CVE scan is overdue")
    if raw < 60:
        alerts.append(f"CVE: critical/high vulnerabilities in dependencies ({det})")

    # ── Code audit ────────────────────────────────────────────────────────────
    audit_data = _read_latest("code_audit")
    stale, lr  = _is_stale(audit_data, max_hours=8 * 24)
    raw, det   = _score_code_audit(audit_data)
    components.append(PostureComponent(
        name="Code Audit", score=raw * WEIGHTS["code_audit"], weight=WEIGHTS["code_audit"],
        raw_score=raw, detail=det, stale=stale, last_run=lr
    ))
    if stale:
        recommendations.append("Run `python security/guardian.py audit` — code audit is overdue")
    if raw < 60:
        alerts.append(f"Code: serious issues found ({det})")

    # ── Red team ──────────────────────────────────────────────────────────────
    rt_data   = _read_latest("red_team")
    stale, lr = _is_stale(rt_data, max_hours=72)
    raw, det  = _score_red_team(rt_data)
    components.append(PostureComponent(
        name="Red Team", score=raw * WEIGHTS["red_team"], weight=WEIGHTS["red_team"],
        raw_score=raw, detail=det, stale=stale, last_run=lr
    ))
    if stale:
        recommendations.append("Run `python security/red_team.py` — attack simulation is overdue")
    if rt_data and rt_data.get("bypassed", 0) > 0:
        alerts.append(f"Red team: {rt_data['bypassed']} attack(s) bypassed sanitizer")
        recommendations.append("Run `python security/autodefense.py` to close bypass gaps")
    if rt_data and rt_data.get("indirect_gap"):
        alerts.append("Indirect injection gap: external API responses not sanitized")
        recommendations.append(
            "Add sanitize_input() call on all external API response strings "
            "before returning them in module handlers"
        )

    # ── Jailbreak sim ────────────────────────────────────────────────────────
    jb_data   = _read_latest("jailbreak")
    stale, lr = _is_stale(jb_data, max_hours=72)
    raw, det  = _score_jailbreak(jb_data)
    components.append(PostureComponent(
        name="Jailbreak Sim", score=raw * WEIGHTS["jailbreak"], weight=WEIGHTS["jailbreak"],
        raw_score=raw, detail=det, stale=stale, last_run=lr
    ))
    if stale:
        recommendations.append("Run `python security/jailbreak_sim.py` — jail test overdue")
    if raw < 70:
        alerts.append(f"Jailbreak: escape(s) detected in output jail ({det})")

    # ── Patterns ──────────────────────────────────────────────────────────────
    pat_data  = _read_latest("pattern_update")
    stale, lr = _is_stale(pat_data, max_hours=12)
    raw, det  = _score_patterns(pat_data)
    components.append(PostureComponent(
        name="Patterns", score=raw * WEIGHTS["patterns"], weight=WEIGHTS["patterns"],
        raw_score=raw, detail=det, stale=stale, last_run=lr
    ))
    if stale:
        recommendations.append("Run `python security/guardian.py patterns` — pattern update overdue")

    # ── Anomaly ───────────────────────────────────────────────────────────────
    anom_data = _read_latest("anomaly_detect")
    stale, lr = _is_stale(anom_data, max_hours=2)
    raw, det  = _score_anomaly(anom_data)
    components.append(PostureComponent(
        name="Anomaly", score=raw * WEIGHTS["anomaly"], weight=WEIGHTS["anomaly"],
        raw_score=raw, detail=det, stale=stale, last_run=lr
    ))
    if stale:
        recommendations.append("Run `python security/guardian.py anomaly` — anomaly check overdue")
    if raw < 70:
        alerts.append(f"Anomaly: recent security alerts in logs ({det})")

    # ── Overall ───────────────────────────────────────────────────────────────
    overall = sum(c.score for c in components)
    overall = round(min(100.0, max(0.0, overall)), 1)
    grade   = "A" if overall >= 90 else "B" if overall >= 75 else "C" if overall >= 60 else "D" if overall >= 45 else "F"

    report = PostureReport(
        overall=overall, grade=grade,
        components=components, computed_at=computed_at,
        alerts=alerts, recommendations=recommendations,
    )

    if verbose:
        _print_posture(report)

    # Save report
    out    = LOG_DIR / "posture_latest.json"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "overall":   overall,
        "grade":     grade,
        "computed_at": computed_at,
        "components": [
            {"name": c.name, "raw_score": c.raw_score, "weight": c.weight,
             "weighted": c.score, "detail": c.detail, "stale": c.stale, "last_run": c.last_run}
            for c in components
        ],
        "alerts":         alerts,
        "recommendations": recommendations,
    }, indent=2))

    return report


def _grade_color(grade: str) -> str:
    return {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴", "F": "⛔"}.get(grade, "⚪")


def _print_posture(report: PostureReport) -> None:
    bar_filled = int(report.overall / 5)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    print(f"\n{'='*65}")
    print(f"  SECURITY POSTURE SCORE")
    print(f"{'='*65}")
    print(f"\n  [{bar}]  {report.overall:.0f}/100  Grade: {report.grade}")
    print(f"\n  {'Component':<18} {'Raw':>6}  {'Weight':>7}  {'Weighted':>9}  {'Last Run':<20}  Detail")
    print(f"  {'─'*100}")
    for c in report.components:
        stale_mark = " ⚠" if c.stale else ""
        print(f"  {c.name:<18} {c.raw_score:>5.0f}%  {c.weight*100:>5.0f}%   {c.score:>8.1f}  "
              f"{c.last_run:<20}{stale_mark}  {c.detail}")

    if report.alerts:
        print(f"\n  ALERTS ({len(report.alerts)}):")
        for a in report.alerts:
            print(f"    !! {a}")

    if report.recommendations:
        print(f"\n  RECOMMENDATIONS:")
        for r in report.recommendations:
            print(f"    -> {r}")

    print(f"\n{'─'*65}")
    print(f"  Computed: {report.computed_at[:19]}")
    print(f"{'='*65}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if args.json:
        r = calculate_posture(verbose=False)
        print(json.dumps({"overall": r.overall, "grade": r.grade,
                          "alerts": r.alerts, "recommendations": r.recommendations}, indent=2))
    else:
        calculate_posture(verbose=True)
