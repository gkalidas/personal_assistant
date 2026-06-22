#!/usr/bin/env python3
"""
Auto-Defense — closes gaps found by red_team.py.

For each payload that bypassed the sanitizer:
  1. Ask the local LLM to draft a regex pattern that would catch it
  2. Validate: pattern must catch the bypass AND not false-positive on 20 normal inputs
  3. If valid → add to security/patterns.json + hot-reload sanitizer
  4. Re-run the specific bypass to confirm the fix

Usage:
    python security/autodefense.py                  # process red_team_latest.json
    python security/autodefense.py --report FILE    # process specific report
    python security/autodefense.py --dry-run        # show proposals, don't write
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

log = logging.getLogger("security.autodefense")

PATTERNS_FILE = PROJECT / "security" / "patterns.json"
LOG_DIR       = PROJECT / "logs" / "security"

# Normal inputs that a valid defense pattern must NOT flag
_NORMAL_INPUTS = [
    "what is the weather at Solapur today",
    "show me my blood pressure history for the last 7 days",
    "log my BP 130 over 85, feeling okay",
    "how much did I spend on seeds this month",
    "write diary from my photos",
    "add plot north_field, 3 acres, black soil, Barloni",
    "plant sugarcane on south_field on 2025-06-01",
    "log spray Copper Oxychloride 250g on north_field",
    "show my crop history for pomegranate",
    "earned 45000 from pomegranate sale today",
    "what are the pomegranate mandi prices in Solapur",
    "set monthly budget 8000 for fertilizer",
    "my fasting sugar was 105 mg/dL this morning",
    "slept 7.5 hours last night",
    "show open observations on north_field",
    "what diseases affect pomegranate in monsoon",
    "add savings goal: buy tractor, target 2 lakh rupees",
    "show weekly health summary",
    "approve diary for this week",
    "7-day weather forecast for Barloni",
]


@dataclass
class DefenseProposal:
    bypass_id:       str
    bypass_payload:  str
    bypass_category: str
    pattern:         str
    catches_bypass:  bool
    false_positives: list[str]
    is_valid:        bool
    promoted:        bool = False
    reason:          str  = ""


def _load_patterns() -> dict:
    """Load security/patterns.json, or an empty skeleton if it is missing."""
    if PATTERNS_FILE.exists():
        return json.loads(PATTERNS_FILE.read_text())
    return {"injection_patterns": [], "_meta": {}}


def _save_patterns(data: dict) -> None:
    """Write the patterns dict back to security/patterns.json."""
    PATTERNS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _existing_ids(data: dict) -> set[str]:
    """Return the set of pattern IDs already present in the patterns data."""
    return {p["id"] for p in data.get("injection_patterns", [])}


def _next_auto_id(data: dict) -> str:
    """Generate next auto-defense pattern ID."""
    existing = _existing_ids(data)
    for n in range(200, 999):
        candidate = f"INJ-AD{n}"
        if candidate not in existing:
            return candidate
    return f"INJ-AD{int(time.time())}"


# ── LLM-based pattern generation ──────────────────────────────────────────────

_PROMPT = """You are a security engineer building a regex-based injection detector for a personal AI assistant.

The following attack payload BYPASSED our sanitizer (it was NOT flagged):
---
{payload}
---

Category: {category}
Description: {desc}

Write ONE Python regex pattern (raw string syntax, re.IGNORECASE flag is assumed) that would detect this type of attack.

Requirements:
- The pattern MUST match the payload above
- Keep it general enough to catch variations of the same attack, but NOT so broad it catches normal conversation
- Pattern must work with Python re.compile(..., re.IGNORECASE)
- Output ONLY the raw regex string — no quotes, no explanation, no code, just the pattern itself

Example output format:
forget\\s+(everything|all)(\\s+(you|i)\\s+(know|said|told))?
"""

def _ask_llm_for_pattern(payload: str, category: str, desc: str) -> str | None:
    """Ask Ollama to generate a defense regex for the given bypass."""
    try:
        from core.config import OLLAMA_URL, TEXT_MODEL
        prompt = _PROMPT.format(payload=payload[:300], category=category, desc=desc)
        resp   = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model":   TEXT_MODEL,
                "prompt":  prompt,
                "stream":  False,
                "think":   False,
                "options": {"num_predict": 80, "temperature": 0.1},
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        # Clean up: remove surrounding quotes, backticks, r"..." prefix
        raw = re.sub(r'^[r\'"`]+|[\'"`]+$', '', raw.strip())
        raw = raw.split("\n")[0].strip()  # first line only
        log.info("LLM proposed pattern: %r", raw[:80])
        return raw
    except Exception as e:
        log.error("LLM call failed: %s", e)
        return None


# ── Pattern validation ─────────────────────────────────────────────────────────

def _validate_pattern(pattern: str, bypass_payload: str) -> tuple[bool, list[str], str]:
    """
    Validate a candidate defense pattern.
    Returns: (is_valid, false_positive_inputs, reason)
    """
    # 1. Can it compile?
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return False, [], f"regex compile error: {e}"

    # 2. Does it catch the bypass?
    if not compiled.search(bypass_payload):
        return False, [], "pattern does NOT match the bypass payload"

    # 3. False positive check
    fp = [inp for inp in _NORMAL_INPUTS if compiled.search(inp)]
    if fp:
        return False, fp, f"false positives on {len(fp)} normal input(s)"

    return True, [], "valid — catches bypass, no false positives"


# ── Promote pattern to patterns.json ──────────────────────────────────────────

def _promote_pattern(pat_str: str, bypass: dict, pat_id: str) -> None:
    """Add a validated pattern to patterns.json and hot-reload sanitizer."""
    data = _load_patterns()
    data["injection_patterns"].append({
        "id":          pat_id,
        "pattern":     pat_str,
        "severity":    bypass.get("severity", "medium"),
        "category":    bypass.get("category", "autodefense"),
        "source":      f"autodefense:{bypass.get('id','?')}",
        "auto_generated": True,
        "promoted_at": datetime.now().isoformat(),
        "bypassed_payload": bypass.get("payload", "")[:100],
    })
    data["_meta"]["last_updated"]   = datetime.now().strftime("%Y-%m-%d")
    data["_meta"]["total_patterns"] = len(data["injection_patterns"])
    _save_patterns(data)

    # Hot-reload sanitizer
    try:
        import core.sanitizer as san
        patterns_list = [p["pattern"] for p in data["injection_patterns"]]
        combined = "|".join(f"(?:{p})" for p in patterns_list)
        san._INJECTION_RE = re.compile(combined, re.IGNORECASE)
        log.info("Sanitizer hot-reloaded with %d patterns", len(patterns_list))
    except Exception as e:
        log.warning("Could not hot-reload sanitizer: %s", e)


# ── Confirm fix works ──────────────────────────────────────────────────────────

def _confirm_fix(bypass_payload: str) -> bool:
    """Re-test the payload after pattern promotion to confirm it's now blocked."""
    try:
        from core.sanitizer import sanitize_input
        result = sanitize_input(bypass_payload)
        return bool(result.warnings)
    except Exception:
        return False


# ── Main runner ────────────────────────────────────────────────────────────────

def _resolve_red_team_report(red_team_report: dict | None) -> dict | None:
    """Return the report to process: the passed dict, or red_team_latest.json.

    Returns None (and logs) if no report was passed and no latest file exists.
    """
    if red_team_report is not None:
        return red_team_report
    latest = LOG_DIR / "red_team_latest.json"
    if not latest.exists():
        log.error("No red_team_latest.json found. Run red_team.py first.")
        return None
    return json.loads(latest.read_text())


def _process_bypass(
    bypass: dict, data: dict, dry_run: bool, verbose: bool
) -> DefenseProposal | None:
    """Generate, validate, and (optionally) promote a defense for one bypass.

    Returns the resulting DefenseProposal, or None for action_injection bypasses
    (those are handled by the validator layer, not by sanitizer patterns).
    Mutates nothing except, on promotion, security/patterns.json via
    :func:`_promote_pattern`.
    """
    payload  = bypass.get("payload", "")
    category = bypass.get("category", "unknown")
    desc     = bypass.get("description", "")
    bid      = bypass.get("id", "?")

    if verbose:
        print(f"\n  [{bid}] {desc[:60]}")
        print(f"  Category: {category}  |  Severity: {bypass.get('severity','?')}")
        print(f"  Payload: {payload[:70]!r}")

    if category == "action_injection":
        if verbose: print("  → Skipped (action_injection handled by validator layer)")
        return None

    if verbose: print("  → Asking LLM to draft defense pattern...")
    pat_str = _ask_llm_for_pattern(payload, category, desc)
    if not pat_str:
        if verbose: print("  ✗ LLM could not generate pattern")
        return DefenseProposal(
            bypass_id=bid, bypass_payload=payload, bypass_category=category,
            pattern="", catches_bypass=False, false_positives=[],
            is_valid=False, reason="LLM unavailable or no pattern generated",
        )

    if verbose: print(f"  → Proposed: {pat_str[:70]!r}")
    is_valid, fp_inputs, reason = _validate_pattern(pat_str, payload)
    proposal = DefenseProposal(
        bypass_id=bid, bypass_payload=payload, bypass_category=category,
        pattern=pat_str, catches_bypass=is_valid or not fp_inputs,
        false_positives=fp_inputs, is_valid=is_valid, reason=reason,
    )
    _apply_proposal_outcome(proposal, bypass, data, dry_run, verbose)
    return proposal


def _apply_proposal_outcome(
    proposal: DefenseProposal, bypass: dict, data: dict, dry_run: bool, verbose: bool
) -> None:
    """Promote a valid pattern (unless dry-run) and update the proposal in place.

    On promotion, writes the pattern to patterns.json and confirms the bypass is
    now blocked. Invalid patterns are left as-is (the proposal already carries
    the validation reason). Prints progress when ``verbose``.
    """
    if proposal.is_valid and not dry_run:
        pat_id = _next_auto_id(data)
        _promote_pattern(proposal.pattern, bypass, pat_id)
        confirmed = _confirm_fix(proposal.bypass_payload)
        proposal.promoted = True
        proposal.reason   = f"Promoted as {pat_id}. Fix confirmed: {confirmed}"
        if verbose:
            print(f"  {'✓' if confirmed else '?'} Promoted as {pat_id}. Confirmed blocked: {confirmed}")
    elif proposal.is_valid and dry_run:
        proposal.reason = "DRY RUN — pattern is valid but not promoted"
        if verbose: print("  ✓ Pattern valid (dry run — not promoted)")
    elif verbose:
        print(f"  ✗ Invalid: {proposal.reason}")
        if proposal.false_positives:
            print(f"    False positives: {proposal.false_positives[:2]}")


def run_autodefense(
    red_team_report: dict | None = None,
    dry_run: bool = False,
    verbose: bool = True,
) -> list[DefenseProposal]:
    """Process red-team bypasses and generate/promote defense patterns.

    Reads from red_team_latest.json when ``red_team_report`` is None. Delegates
    each bypass to :func:`_process_bypass`, saves an autodefense report (unless
    ``dry_run``), and returns the list of proposals.
    """
    red_team_report = _resolve_red_team_report(red_team_report)
    if red_team_report is None:
        return []

    bypasses = red_team_report.get("bypasses", [])
    if not bypasses:
        if verbose:
            print("\n  No bypasses to defend against — system is clean.")
        return []

    if verbose:
        print(f"\n{'='*65}")
        print(f"  AUTO-DEFENSE — processing {len(bypasses)} bypass(es)")
        print(f"{'='*65}")

    data = _load_patterns()
    proposals: list[DefenseProposal] = []
    for bypass in bypasses:
        proposal = _process_bypass(bypass, data, dry_run, verbose)
        if proposal is None:
            continue
        proposals.append(proposal)
        if proposal.promoted:
            data = _load_patterns()  # reload after a write so IDs stay unique

    if not dry_run:
        _save_autodefense_report(proposals, red_team_report)

    if verbose:
        _print_autodefense_summary(proposals, dry_run)
    return proposals


def _print_autodefense_summary(proposals: list[DefenseProposal], dry_run: bool) -> None:
    """Print the promoted/failed tally for an autodefense run."""
    promoted = sum(1 for p in proposals if p.promoted)
    failed   = sum(1 for p in proposals if not p.is_valid)
    print(f"\n{'─'*65}")
    print(f"  Defense summary:  {promoted} pattern(s) promoted, {failed} failed validation")
    if dry_run:
        print("  (DRY RUN — no changes written)")
    print(f"{'─'*65}")


def _save_autodefense_report(proposals: list[DefenseProposal], rt_report: dict) -> None:
    """Write a timestamped autodefense report plus autodefense_latest.json."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    out  = LOG_DIR / f"autodefense_{ts}.json"
    data = {
        "run_at":         datetime.now().isoformat(),
        "red_team_run":   rt_report.get("run_at", ""),
        "bypasses_in":    len(rt_report.get("bypasses", [])),
        "proposals":      len(proposals),
        "promoted":       sum(1 for p in proposals if p.promoted),
        "failed":         sum(1 for p in proposals if not p.is_valid),
        "details": [
            {
                "bypass_id": p.bypass_id,
                "category":  p.bypass_category,
                "pattern":   p.pattern,
                "is_valid":  p.is_valid,
                "promoted":  p.promoted,
                "reason":    p.reason,
                "false_positives": p.false_positives,
            }
            for p in proposals
        ],
    }
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    latest = LOG_DIR / "autodefense_latest.json"
    latest.write_text(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report",  default=None, help="Path to red_team JSON report")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING)

    rt_data = None
    if args.report:
        rt_data = json.loads(Path(args.report).read_text())

    run_autodefense(red_team_report=rt_data, dry_run=args.dry_run, verbose=True)
