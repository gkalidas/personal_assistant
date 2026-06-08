"""
Safe auto-patcher for vulnerable Python dependencies.
Only patches same-major-version upgrades (patch/minor bumps) to avoid
breaking changes. Flags major-version updates for manual review.
"""

import importlib.metadata
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from security.scanner import Vulnerability

log = logging.getLogger("security.patcher")

PYTHON = sys.executable
PROJECT = Path(__file__).parent.parent


def _current_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _is_safe_to_auto_patch(current: str, target: str) -> bool:
    """
    Auto-patch only if major version stays the same.
    e.g. 0.28.1 → 0.28.5 = safe   (patch bump)
         0.28.1 → 0.29.0 = safe   (minor bump, same major)
         0.28.1 → 1.0.0  = UNSAFE (major bump — may break API)
    """
    try:
        cur_parts = [int(x) for x in current.split(".")[:3]]
        tgt_parts = [int(x) for x in target.split(".")[:3]]
        return cur_parts[0] == tgt_parts[0]  # same major
    except Exception:
        return False


def patch_vulnerability(vuln: Vulnerability, dry_run: bool = False) -> dict:
    """Attempt to patch a single vulnerable package. Returns result dict."""
    if not vuln.fix_version:
        return {"status": "no_fix", "package": vuln.package, "reason": "No fix version available yet"}

    if not _is_safe_to_auto_patch(vuln.version, vuln.fix_version):
        return {
            "status":  "manual_required",
            "package": vuln.package,
            "current": vuln.version,
            "target":  vuln.fix_version,
            "reason":  "Major version change — review changelog before upgrading",
        }

    log.info(f"Patching {vuln.package} {vuln.version} → {vuln.fix_version} (CVE: {vuln.vuln_id})")

    if dry_run:
        return {
            "status":  "dry_run",
            "package": vuln.package,
            "current": vuln.version,
            "target":  vuln.fix_version,
        }

    try:
        result = subprocess.run(
            [PYTHON, "-m", "pip", "install", "--quiet",
             f"{vuln.package}>={vuln.fix_version}"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            new_ver = _current_version(vuln.package) or "unknown"
            log.info(f"  Patched: {vuln.package} now at {new_ver}")
            return {
                "status":       "patched",
                "package":      vuln.package,
                "from_version": vuln.version,
                "to_version":   new_ver,
                "vuln_id":      vuln.vuln_id,
                "patched_at":   datetime.now().isoformat(),
            }
        else:
            log.error(f"  Patch failed: {result.stderr[:200]}")
            return {
                "status":  "failed",
                "package": vuln.package,
                "reason":  result.stderr[:200],
            }
    except Exception as e:
        log.error(f"  Exception patching {vuln.package}: {e}")
        return {"status": "error", "package": vuln.package, "reason": str(e)}


def auto_patch_all(
    vulns: list[Vulnerability],
    severity_threshold: str = "HIGH",
    dry_run: bool = False,
) -> dict:
    """
    Patch all safe, patchable vulnerabilities at or above the severity threshold.
    Returns summary of what was patched, skipped, and flagged for manual review.
    """
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
    threshold_level = severity_order.get(severity_threshold, 1)

    patched   = []
    skipped   = []
    manual    = []
    no_fix    = []

    # Deduplicate by package (keep highest severity)
    seen_packages: dict[str, Vulnerability] = {}
    for v in vulns:
        if v.package not in seen_packages:
            seen_packages[v.package] = v
        else:
            existing = seen_packages[v.package]
            if severity_order.get(v.severity, 99) < severity_order.get(existing.severity, 99):
                seen_packages[v.package] = v

    for vuln in seen_packages.values():
        vuln_level = severity_order.get(vuln.severity, 99)

        if vuln_level > threshold_level:
            skipped.append({"package": vuln.package, "severity": vuln.severity,
                            "reason": f"Below threshold ({severity_threshold})"})
            continue

        if not vuln.patchable:
            if vuln.fix_version:
                manual.append({
                    "package":    vuln.package,
                    "current":    vuln.version,
                    "fix":        vuln.fix_version,
                    "severity":   vuln.severity,
                    "vuln_id":    vuln.vuln_id,
                    "reason":     "Major version upgrade required — review manually",
                })
            else:
                no_fix.append({"package": vuln.package, "vuln_id": vuln.vuln_id,
                                "severity": vuln.severity, "reason": "No fix available"})
            continue

        result = patch_vulnerability(vuln, dry_run=dry_run)
        if result["status"] in ("patched", "dry_run"):
            patched.append(result)
        elif result["status"] == "manual_required":
            manual.append(result)
        else:
            skipped.append(result)

    return {
        "run_at":            datetime.now().isoformat(),
        "dry_run":           dry_run,
        "severity_threshold": severity_threshold,
        "patched":           patched,
        "manual_review":     manual,
        "skipped":           skipped,
        "no_fix_available":  no_fix,
        "summary": {
            "patched":        len(patched),
            "manual_needed":  len(manual),
            "skipped":        len(skipped),
            "no_fix":         len(no_fix),
        },
    }
