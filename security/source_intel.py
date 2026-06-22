#!/usr/bin/env python3
"""
Source intelligence bridge — turns LLM-security feeds into self-hardening.

Pipeline (all safety gates from docs/self_improving_security.md §4 apply):

    sources.fetch_all_sources()         # scrubbed on ingest
        → for each item, try to REPRODUCE it as a concrete sanitizer bypass
            → reproduced AND tier ≤ 2  → autodefense (validated auto-apply,
                                          rate-limited, rollback-guarded)
            → tier 3, or not reproduced → logs/security/review_queue.json

Only *reproduced* bypasses from trusted tiers can change defenses automatically;
everything else is queued for human review. Never raises.

Usage:
    python security/source_intel.py            # live run
    python security/source_intel.py --dry-run  # fetch + reproduce, no changes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

from security.sources import fetch_all_sources, SOURCE_TIER
from security.threat_intel import ThreatItem, _payloads_for_threat

log = logging.getLogger("security.source_intel")

LOG_DIR     = PROJECT / "logs" / "security"
REVIEW_FILE = LOG_DIR / "review_queue.json"

# Tiers allowed to drive automatic defense changes (curated taxonomies + vendors).
_AUTO_APPLY_TIERS = {1, 2}


def _reproduce_bypass(item: ThreatItem) -> str | None:
    """Return a concrete payload from the item that bypasses sanitize_input, else None.

    This is the 'reproduce, don't trust prose' gate: a finding only counts if a
    derived payload actually slips past the current sanitizer.
    """
    from core.sanitizer import sanitize_input
    for payload in _payloads_for_threat(item):
        try:
            if not sanitize_input(payload).warnings:
                return payload
        except Exception:
            continue
    return None


def _to_bypass_record(item: ThreatItem, payload: str) -> dict:
    """Shape a reproduced bypass as the dict autodefense expects."""
    return {
        "id":          f"SRC-{item.id}"[:60],
        "category":    item.threat_type or "source_intel",
        "severity":    (item.severity or "medium").lower() if item.severity != "INFO" else "medium",
        "payload":     payload,
        "description": item.title[:120],
    }


def _save_review_queue(entries: list[dict]) -> None:
    """Append entries to the human-review queue (deduped by id)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    if REVIEW_FILE.exists():
        try:
            existing = json.loads(REVIEW_FILE.read_text())
        except Exception:
            existing = []
    seen = {e.get("id") for e in existing}
    existing.extend(e for e in entries if e.get("id") not in seen)
    REVIEW_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False))


def _triage(items: list[ThreatItem]) -> tuple[list[dict], list[dict]]:
    """Split fetched items into (auto-apply bypasses, review-queue entries)."""
    auto: list[dict] = []
    review: list[dict] = []
    for item in items:
        tier    = SOURCE_TIER.get(item.source, 3)
        payload = _reproduce_bypass(item)
        if payload and tier in _AUTO_APPLY_TIERS:
            auto.append(_to_bypass_record(item, payload))
        else:
            review.append({
                "id":       f"SRC-{item.id}"[:60],
                "source":   item.source,
                "tier":     tier,
                "title":    item.title[:120],
                "type":     item.threat_type,
                "reproduced": payload is not None,
                "reason":   ("tier-3 source — review before applying" if payload
                             else "not reproduced as a bypass — informational"),
                "queued_at": datetime.now().isoformat(),
            })
    return auto, review


def run_source_intel(dry_run: bool = False, verbose: bool = True) -> dict:
    """Fetch LLM-security sources, reproduce bypasses, and self-harden safely.

    Tier-1/2 reproduced bypasses go through autodefense (validated, rate-limited,
    rollback-guarded); everything else is queued for review. Returns a summary.
    """
    if verbose:
        print(f"\n{'='*65}\n  SOURCE INTEL — LLM-security feeds\n{'='*65}")

    items = fetch_all_sources()
    auto, review = _triage(items)

    if verbose:
        print(f"  fetched {len(items)} items → {len(auto)} reproduced bypass(es), "
              f"{len(review)} queued for review")

    promoted = 0
    if auto and not dry_run:
        from security.autodefense import run_autodefense
        report = {"bypassed": len(auto), "bypasses": auto, "run_at": datetime.now().isoformat()}
        proposals = run_autodefense(red_team_report=report, dry_run=False, verbose=verbose)
        promoted = sum(1 for p in proposals if p.promoted)

    _save_review_queue(review)

    summary = {
        "run_at":            datetime.now().isoformat(),
        "items_fetched":     len(items),
        "reproduced_bypasses": len(auto),
        "auto_promoted":     promoted,
        "review_queued":     len(review),
        "dry_run":           dry_run,
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "source_intel_latest.json").write_text(json.dumps(summary, indent=2))
    if verbose:
        print(f"  → {promoted} pattern(s) auto-promoted, {len(review)} in review queue")
        print(f"{'─'*65}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    run_source_intel(dry_run=args.dry_run, verbose=True)
