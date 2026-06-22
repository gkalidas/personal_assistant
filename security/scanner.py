"""
CVE / vulnerability scanner for installed Python packages.
Uses OSV.dev (Google) — aggregates NVD, GitHub Advisories, PyPI Advisory DB,
Debian, Red Hat, Alpine, and 20+ other global security databases.
No API key required.
"""

import importlib.metadata
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
NVD_URL       = "https://services.nvd.nist.gov/rest/json/cves/2.0"

log = logging.getLogger("security.scanner")


@dataclass
class Vulnerability:
    package:     str
    version:     str
    vuln_id:     str          # CVE-XXXX-XXXX or GHSA-xxxx
    severity:    str          # CRITICAL / HIGH / MEDIUM / LOW / UNKNOWN
    summary:     str
    fix_version: str | None   # earliest version with the fix (None = no fix)
    aliases:     list[str] = field(default_factory=list)
    source:      str = "OSV"

    @property
    def patchable(self) -> bool:
        """True if a fix version is known and it's a patch/minor bump."""
        if not self.fix_version:
            return False
        try:
            cur = [int(x) for x in self.version.split(".")[:3]]
            fix = [int(x) for x in self.fix_version.split(".")[:3]]
            # Only auto-patch if major version matches (no breaking changes)
            return cur[0] == fix[0]
        except Exception:
            return False


def _installed_packages() -> list[tuple[str, str]]:
    """Return list of (name, version) for all installed packages."""
    pkgs = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name", "")
        version = dist.metadata.get("Version", "")
        if name and version:
            pkgs.append((name, version))
    return sorted(set(pkgs))


def _cvss_to_severity(score_str: str) -> str | None:
    """Map a numeric CVSS score string to a severity band, or None if unparseable."""
    if not score_str:
        return None
    try:
        score = float(score_str)
    except ValueError:
        return None
    if score >= 9.0: return "CRITICAL"
    if score >= 7.0: return "HIGH"
    if score >= 4.0: return "MEDIUM"
    return "LOW"


def _severity_from_osv(vuln: dict) -> str:
    """Extract the highest severity band from an OSV vuln object.

    Checks the top-level ``severity`` list first, then the per-affected CVSS
    scores as a fallback. Returns "UNKNOWN" when no score is present.
    """
    for sev in vuln.get("severity", []):
        band = _cvss_to_severity(sev.get("score", ""))
        if band:
            return band
    for aff in vuln.get("affected", []):
        for sev in aff.get("severity", []):
            if "CVSS" in sev.get("type", ""):
                band = _cvss_to_severity(sev.get("score", ""))
                if band:
                    return band
    return "UNKNOWN"


def _fix_version_from_osv(vuln: dict, pkg_name: str) -> str | None:
    """Extract first fixed version for a package from OSV affected list."""
    for affected in vuln.get("affected", []):
        pkg = affected.get("package", {})
        if pkg.get("name", "").lower() != pkg_name.lower():
            continue
        for rng in affected.get("ranges", []):
            for ev in rng.get("events", []):
                fix = ev.get("fixed")
                if fix:
                    return fix
    return None


_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}


def _query_osv(packages: list[tuple[str, str]]) -> list[dict]:
    """Batch-query OSV.dev for the given packages. Returns the per-package
    result list (parallel to ``packages``), or [] on any request failure."""
    queries = [
        {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
        for name, version in packages
    ]
    log.info(f"Querying OSV.dev for {len(queries)} packages...")
    try:
        resp = httpx.post(
            OSV_BATCH_URL, json={"queries": queries}, timeout=30.0,
            headers={"User-Agent": "GK-Security-Guardian/1.0"},
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        log.error(f"OSV query failed: {e}")
        return []


def _parse_osv_results(
    packages: list[tuple[str, str]], results: list[dict]
) -> list[Vulnerability]:
    """Turn OSV batch results into Vulnerability objects, sorted by severity."""
    vulns: list[Vulnerability] = []
    for (name, version), result in zip(packages, results):
        for vuln in result.get("vulns", []):
            vulns.append(Vulnerability(
                package=name, version=version,
                vuln_id=vuln.get("id", "UNKNOWN"),
                severity=_severity_from_osv(vuln),
                summary=vuln.get("summary", vuln.get("details", "No summary"))[:200],
                fix_version=_fix_version_from_osv(vuln, name),
                aliases=vuln.get("aliases", []),
            ))
    vulns.sort(key=lambda v: _SEVERITY_ORDER.get(v.severity, 99))
    return vulns


def scan_packages(packages: list[tuple[str, str]] | None = None) -> list[Vulnerability]:
    """Scan installed (or given) packages for known CVEs via OSV.dev.

    Issues one batched request and returns Vulnerability objects sorted by
    severity. Returns [] when there are no packages or the query fails.
    """
    if packages is None:
        packages = _installed_packages()
    if not packages:
        return []

    results = _query_osv(packages)
    vulns   = _parse_osv_results(packages, results)
    log.info(f"Found {len(vulns)} vulnerabilities across {len(packages)} packages.")
    return vulns


def scan_report(vulns: list[Vulnerability]) -> dict:
    """Build a structured report dict from vulnerability list."""
    severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}
    for v in vulns:
        severity_counts[v.severity] = severity_counts.get(v.severity, 0) + 1

    return {
        "scanned_at":       datetime.now().isoformat(),
        "source":           "OSV.dev (NVD + GitHub Advisories + PyPI + 20+ databases)",
        "total_vulns":      len(vulns),
        "severity_counts":  severity_counts,
        "patchable_count":  sum(1 for v in vulns if v.patchable),
        "vulnerabilities":  [
            {
                "package":     v.package,
                "version":     v.version,
                "vuln_id":     v.vuln_id,
                "aliases":     v.aliases,
                "severity":    v.severity,
                "summary":     v.summary,
                "fix_version": v.fix_version,
                "patchable":   v.patchable,
            }
            for v in vulns
        ],
    }
