"""
CVE / vulnerability scanner for installed Python packages.
Uses OSV.dev (Google) — aggregates NVD, GitHub Advisories, PyPI Advisory DB,
Debian, Red Hat, Alpine, and 20+ other global security databases.
No API key required.
"""

import importlib.metadata
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import httpx

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_VULN_URL  = "https://api.osv.dev/v1/vulns/"   # per-id detail lookup
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


def _score_to_band(score: float) -> str:
    """Map a numeric CVSS base score to a severity band."""
    if score >= 9.0: return "CRITICAL"
    if score >= 7.0: return "HIGH"
    if score >= 4.0: return "MEDIUM"
    if score > 0.0:  return "LOW"
    return "LOW"


# CVSS v3.x base-score metric weights (per the spec).
_CVSS_W = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.20},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "PR": {"U": {"N": 0.85, "L": 0.62, "H": 0.27},   # scope Unchanged
           "C": {"N": 0.85, "L": 0.68, "H": 0.50}},  # scope Changed
    "CIA": {"N": 0.0, "L": 0.22, "H": 0.56},
}


def _cvss_score_from_vector(vector: str) -> float | None:
    """Compute a CVSS v3.x base score from a vector string, or None if not one."""
    if "CVSS:3" not in vector:
        return None
    try:
        m = dict(part.split(":") for part in vector.split("/") if ":" in part)
        scope = m.get("S", "U")
        iss = 1 - ((1 - _CVSS_W["CIA"][m["C"]]) *
                   (1 - _CVSS_W["CIA"][m["I"]]) *
                   (1 - _CVSS_W["CIA"][m["A"]]))
        if scope == "U":
            impact = 6.42 * iss
        else:
            impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
        expl = (8.22 * _CVSS_W["AV"][m["AV"]] * _CVSS_W["AC"][m["AC"]] *
                _CVSS_W["PR"][scope][m["PR"]] * _CVSS_W["UI"][m["UI"]])
        if impact <= 0:
            return 0.0
        raw = (impact + expl) if scope == "U" else 1.08 * (impact + expl)
        import math
        return math.ceil(min(raw, 10.0) * 10) / 10  # roundup to 1 decimal
    except (KeyError, ValueError, ZeroDivisionError):
        return None


def _band_from_score_str(score_str: str) -> str | None:
    """Map a CVSS score string — numeric *or* vector — to a severity band."""
    if not score_str:
        return None
    try:
        return _score_to_band(float(score_str))
    except ValueError:
        pass
    score = _cvss_score_from_vector(score_str)
    return _score_to_band(score) if score is not None else None


# GitHub/OSV database_specific severity bands → our bands.
_DB_SEVERITY_MAP = {"CRITICAL": "CRITICAL", "HIGH": "HIGH",
                    "MODERATE": "MEDIUM", "MEDIUM": "MEDIUM", "LOW": "LOW"}


def _severity_from_osv(vuln: dict) -> str:
    """Extract the highest severity band from an OSV vuln detail object.

    Tries CVSS scores/vectors (top-level then per-affected), then the
    ``database_specific.severity`` band GitHub advisories carry. Returns
    "UNKNOWN" only when no severity signal is present anywhere.
    """
    for sev in vuln.get("severity", []):
        band = _band_from_score_str(sev.get("score", ""))
        if band:
            return band
    for aff in vuln.get("affected", []):
        for sev in aff.get("severity", []):
            band = _band_from_score_str(sev.get("score", ""))
            if band:
                return band
    # GHSA records often omit CVSS but carry a plain severity band here.
    db = (vuln.get("database_specific") or {}).get("severity", "")
    if band := _DB_SEVERITY_MAP.get(str(db).upper()):
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


_detail_cache: dict[str, dict] = {}


def _fetch_vuln_detail(vuln_id: str) -> dict:
    """Fetch the full OSV record for a vuln id.

    The querybatch endpoint returns only ``{id, modified}`` per vuln — no
    severity, summary, or affected/fixed ranges — so each id must be hydrated
    here to recover that data. Cached per-process; returns {} on failure so the
    caller degrades gracefully instead of crashing.
    """
    if vuln_id in _detail_cache:
        return _detail_cache[vuln_id]
    try:
        resp = httpx.get(
            f"{OSV_VULN_URL}{vuln_id}", timeout=15.0,
            headers={"User-Agent": "GK-Security-Guardian/1.0"},
        )
        resp.raise_for_status()
        detail = resp.json()
    except Exception as e:
        log.warning(f"OSV detail fetch failed for {vuln_id}: {e}")
        detail = {}
    _detail_cache[vuln_id] = detail
    return detail


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
    """Turn OSV batch results into Vulnerability objects, sorted by severity.

    querybatch only returns vuln IDs, so each hit is hydrated with a detail
    lookup (fetched concurrently, bounded) to recover severity, summary, and
    the fixed version that auto-patching depends on.
    """
    # Collect every (package, version, vuln_id) hit from the batch response.
    hits: list[tuple[str, str, str]] = []
    for (name, version), result in zip(packages, results):
        for vuln in result.get("vulns", []):
            vid = vuln.get("id")
            if vid:
                hits.append((name, version, vid))

    # Hydrate all unique ids concurrently (results land in _detail_cache).
    unique_ids = {vid for _, _, vid in hits}
    if unique_ids:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_fetch_vuln_detail, unique_ids))

    vulns: list[Vulnerability] = []
    for name, version, vid in hits:
        detail = _fetch_vuln_detail(vid)   # cached from the concurrent pass above
        summary = (detail.get("summary") or detail.get("details") or "No summary")[:200]
        vulns.append(Vulnerability(
            package=name, version=version,
            vuln_id=vid,
            severity=_severity_from_osv(detail),
            summary=summary,
            fix_version=_fix_version_from_osv(detail, name),
            aliases=detail.get("aliases", []),
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
