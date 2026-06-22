#!/usr/bin/env python3
"""
Official LLM-security intelligence sources.

Extends the CVE-centric feeds in threat_intel.py with LLM-specific, curated
sources: the OWASP LLM Top 10, MITRE ATLAS, and vendor/researcher red-team
blogs. Each source carries a trust *tier* that gates auto-apply downstream:

    Tier 1  curated taxonomies (OWASP, MITRE ATLAS)        → may auto-apply
    Tier 2  vendor security/red-team blogs                 → may auto-apply
    Tier 3  independent researchers (RSS)                  → review queue only

SECURITY: every fetched string is scrubbed with sanitize_external_text() before
it is returned — reading an external feed is itself an indirect-injection vector.
All network/parse failures degrade to an empty list (never raise).

Usage:
    from security.sources import fetch_all_sources, SOURCE_TIER
    items = fetch_all_sources(hours=72)
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

from security.threat_intel import ThreatItem, _classify, _keywords  # reuse taxonomy
from core.sanitizer import sanitize_external_text

log = logging.getLogger("security.sources")

_UA = {"User-Agent": "GK-Security-Guardian/1.0"}

# Only keep items whose text mentions an LLM/AI-security concept.
_RELEVANT = {
    "llm", "language model", "prompt injection", "jailbreak", "ai model",
    "chatbot", "agent", "rag", "tool use", "system prompt", "adversarial",
    "data poisoning", "model", "ai security", "guardrail", "alignment",
    "indirect injection", "exfiltrat", "prompt leak", "openai", "anthropic",
}


@dataclass(frozen=True)
class Source:
    """A single intelligence feed."""
    name: str
    url:  str
    fmt:  str   # "rss" | "yaml" | "markdown"
    tier: int   # 1, 2, or 3


# ── Registry ────────────────────────────────────────────────────────────────────
SOURCES = [
    # Tier 1 — curated taxonomies
    Source("atlas", "https://raw.githubusercontent.com/mitre-atlas/atlas-data/main/dist/ATLAS.yaml", "yaml", 1),
    Source("owasp", "https://raw.githubusercontent.com/OWASP/www-project-top-10-for-large-language-model-applications/main/README.md", "markdown", 1),
    # Tier 2 — vendor security/red-team blogs
    Source("googlepzero", "https://googleprojectzero.blogspot.com/feeds/posts/default", "rss", 2),
    Source("msrc", "https://msrc.microsoft.com/blog/feed/", "rss", 2),
    # Tier 3 — independent researchers
    Source("willison", "https://simonwillison.net/atom/everything/", "rss", 3),
    Source("embracethered", "https://embracethered.com/blog/index.xml", "rss", 3),
]

SOURCE_TIER = {s.name: s.tier for s in SOURCES}


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _is_relevant(text: str) -> bool:
    """True if the text mentions any tracked LLM/AI-security concept."""
    t = text.lower()
    return any(k in t for k in _RELEVANT)


def _make_item(source: str, ext_id: str, title: str, desc: str,
               severity: str = "INFO") -> ThreatItem:
    """Build a ThreatItem from a feed entry, scrubbing the title/description.

    The scrub is mandatory: feed text is untrusted and flows toward the LLM.
    """
    clean_title = sanitize_external_text(title[:200], label=f"source:{source}")
    clean_desc  = sanitize_external_text(desc[:600],  label=f"source:{source}")
    combined    = f"{clean_title} {clean_desc}"
    return ThreatItem(
        id=f"{source}-{ext_id}"[:120],
        source=source,
        title=clean_title,
        description=clean_desc,
        severity=severity,
        threat_type=_classify(combined),
        published=datetime.now().strftime("%Y-%m-%d"),
        keywords=_keywords(combined),
    )


def _get(url: str, timeout: float = 20.0):
    """HTTP GET with the guardian UA. Returns the Response, or None on failure."""
    try:
        resp = httpx.get(url, timeout=timeout, headers=_UA, follow_redirects=True)
        if resp.status_code != 200:
            log.debug("source %s -> HTTP %s", url, resp.status_code)
            return None
        return resp
    except Exception as e:
        log.warning("source fetch failed %s: %s", url, e)
        return None


# ── Per-format fetchers ──────────────────────────────────────────────────────────

def _fetch_rss(source: Source, max_items: int = 15) -> list[ThreatItem]:
    """Parse an RSS/Atom feed, keeping only LLM-security-relevant entries."""
    resp = _get(source.url)
    if resp is None:
        return []
    import feedparser
    feed = feedparser.parse(resp.text)
    items = []
    for entry in feed.entries[:50]:
        title = entry.get("title", "")
        desc  = entry.get("summary", entry.get("description", ""))
        if not _is_relevant(f"{title} {desc}"):
            continue
        ext_id = entry.get("id") or entry.get("link", "")[-40:] or title[:40]
        items.append(_make_item(source.name, ext_id, title, desc))
        if len(items) >= max_items:
            break
    return items


def _fetch_yaml(source: Source, max_items: int = 30) -> list[ThreatItem]:
    """Parse the MITRE ATLAS YAML, emitting one item per technique."""
    resp = _get(source.url)
    if resp is None:
        return []
    import yaml
    try:
        data = yaml.safe_load(resp.text)
    except Exception as e:
        log.warning("yaml parse failed for %s: %s", source.name, e)
        return []

    techniques = []
    for matrix in (data.get("matrices") or []):
        techniques.extend(matrix.get("techniques") or [])
    techniques = techniques or (data.get("techniques") or [])

    items = []
    for tech in techniques[:200]:
        title = tech.get("name", "")
        desc  = tech.get("description", "")
        if not _is_relevant(f"{title} {desc}"):
            continue
        tid = tech.get("id", title[:40])
        items.append(_make_item(source.name, tid, title, desc, severity="MEDIUM"))
        if len(items) >= max_items:
            break
    return items


def _fetch_markdown(source: Source, max_items: int = 15) -> list[ThreatItem]:
    """Extract OWASP-style 'LLMxx: Title' headings from a raw markdown doc."""
    resp = _get(source.url)
    if resp is None:
        return []
    import re
    items = []
    # Match headings like "## LLM01:2025 Prompt Injection" or "LLM01: Prompt Injection"
    for m in re.finditer(r"(LLM0?\d{1,2})[:\s][^\n]{3,80}", resp.text):
        heading = m.group(0).strip(" #")
        if not _is_relevant(heading):
            continue
        items.append(_make_item(source.name, m.group(1), heading, heading, severity="HIGH"))
        if len(items) >= max_items:
            break
    return items


_FETCHERS = {"rss": _fetch_rss, "yaml": _fetch_yaml, "markdown": _fetch_markdown}


def fetch_source(source: Source) -> list[ThreatItem]:
    """Fetch and normalize a single source. Never raises — returns [] on failure."""
    fetcher = _FETCHERS.get(source.fmt)
    if fetcher is None:
        log.warning("no fetcher for format %r (source %s)", source.fmt, source.name)
        return []
    try:
        items = fetcher(source)
        log.info("source %-14s tier=%d -> %d items", source.name, source.tier, len(items))
        return items
    except Exception as e:
        log.warning("source %s fetch error: %s", source.name, e)
        return []


def fetch_all_sources(hours: int = 72) -> list[ThreatItem]:
    """Fetch every registered source. Returns the combined, de-duplicated list."""
    seen: set[str] = set()
    out: list[ThreatItem] = []
    for source in SOURCES:
        for item in fetch_source(source):
            if item.id in seen:
                continue
            seen.add(item.id)
            out.append(item)
    log.info("sources: %d items across %d feeds", len(out), len(SOURCES))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for it in fetch_all_sources():
        print(f"  [{SOURCE_TIER[it.source]}] {it.source:<14} {it.threat_type:<18} {it.title[:60]}")
