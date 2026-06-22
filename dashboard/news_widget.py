"""
News cache — refreshes every 5 minutes.
Two lanes:
  • INDIA / FARMING  — DDGS news (last 24 h, fallback last week)
  • GLOBAL BREAKING  — RSS feeds from BBC, Reuters, AP, Guardian, Al Jazeera
                       articles older than 48 h are discarded
Dismissed article URLs are persisted to logs/news_dismissed.json (7-day TTL).
"""

import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_DISMISSED_FILE = Path(__file__).parent.parent / "logs" / "news_dismissed.json"

# ── Source deny-list (sensationalism / partisan) ──────────────────────────────
_SOURCE_DENYLIST = {
    "republicworld.com", "republic.com", "republic.in",
    "indiatvnews.com", "zeenews.india.com", "zeenews.com",
    "timesnowonline.com", "timesnow.tv",
    "newsnationtv.com", "abplive.com",
    "opindia.com", "swarajyamag.com",
    "postcard.news", "rightlog.in",
    "newsbharati.com",
}

# ── India / Farming topics — no year hardcoded; timelimit keeps results fresh ─
_FARMING_TOPICS = [
    "india farming agriculture news -site:republicworld.com -site:zeenews.com",
    "pomegranate solapur mandi market price maharashtra APMC",
    "maharashtra kisan farmer news agrowon krishijagran",
    "india monsoon IMD rainfall forecast farmer",
    "MSP minimum support price india kisan",
]

# ── Global breaking news — RSS feeds from trusted outlets ────────────────────
_GLOBAL_FEEDS = {
    "BBC World":    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera":   "https://www.aljazeera.com/xml/rss/all.xml",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "NPR News":     "https://feeds.npr.org/1001/rss.xml",
    # Via Google News RSS (direct feeds require subscription)
    "Reuters":      "https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en",
    "AP News":      "https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en",
}

_RSS_TIMEOUT   = 8
_GLOBAL_MAX    = 6    # articles per feed
_FARMING_MAX   = 5    # articles per DDGS topic
_RSS_MAX_AGE_H = 48   # discard RSS articles older than 48 h


def _domain(url: str) -> str:
    """Extract the registrable domain (sans www) from a URL."""
    try:
        return urlparse(url).netloc.lstrip("www.")
    except Exception:
        return ""


def _parse_rss_date(date_str: str) -> datetime | None:
    """Parse RFC 2822 / ISO 8601 pubDate → timezone-aware datetime."""
    if not date_str:
        return None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(date_str[:30], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


_RSS_NS = {"atom": "http://www.w3.org/2005/Atom",
           "media": "http://search.yahoo.com/mrss/"}


def _rss_text(item, tag: str) -> str:
    """Read an RSS/Atom child tag's text (trying the atom: namespace too)."""
    el = item.find(tag)
    if el is None:
        el = item.find(f"atom:{tag}", _RSS_NS)
    return (el.text or "").strip() if el is not None else ""


def _rss_item_image(item) -> str:
    """Extract a media thumbnail or image enclosure URL from an RSS item ('' if none)."""
    thumb = item.find("media:thumbnail", _RSS_NS)
    if thumb is not None and thumb.get("url"):
        return thumb.get("url", "")
    enc = item.find("enclosure")
    if enc is not None and enc.get("type", "").startswith("image"):
        return enc.get("url", "")
    return ""


def _parse_rss_item(item, source_name: str, cutoff) -> dict | None:
    """Parse one RSS/Atom item to an article dict, or None if old/incomplete."""
    title = _rss_text(item, "title")
    url   = _rss_text(item, "link")
    if not url:
        lnk = item.find("link")
        if lnk is not None:
            url = (lnk.get("href") or lnk.text or "").strip()
    if not (title and url):
        return None

    date_raw = (_rss_text(item, "pubDate") or _rss_text(item, "updated")
                or _rss_text(item, "published"))[:30]
    dt = _parse_rss_date(date_raw)
    if dt and dt < cutoff:                       # older than the freshness window
        return None

    body = _rss_text(item, "description") or _rss_text(item, "summary") or _rss_text(item, "content")
    if body:
        body = re.sub(r"<[^>]+>", "", body).strip()
    src_el = item.find("source")
    display_source = (src_el.text or "").strip() if src_el is not None else source_name

    return {
        "title": title[:160], "source": display_source or source_name, "url": url,
        "date": date_raw[:10], "body": body[:700], "image": _rss_item_image(item),
        "category": "world", "_dt": dt.isoformat() if dt else "",
    }


def _fetch_rss(source_name: str, feed_url: str, max_items: int) -> list[dict]:
    """Fetch one RSS feed; return only articles from the last _RSS_MAX_AGE_H hours."""
    try:
        resp = httpx.get(feed_url, timeout=_RSS_TIMEOUT, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; GKAssistant/1.0)"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)  # nosec B314
    except Exception as e:
        log.debug("rss feed %s failed: %s", source_name, e)
        return []

    raw_items = root.findall(".//item") or root.findall(".//atom:entry", _RSS_NS)
    cutoff    = datetime.now(timezone.utc) - timedelta(hours=_RSS_MAX_AGE_H)
    results = []
    for item in raw_items:
        if len(results) >= max_items:
            break
        article = _parse_rss_item(item, source_name, cutoff)
        if article:
            results.append(article)
    log.debug("rss %s: %d items (cutoff: %s)", source_name, len(results), cutoff.date())
    return results


# ── Dismissed URL persistence ─────────────────────────────────────────────────

def _load_dismissed() -> set[str]:
    """Load dismissed URLs from disk; prune entries older than 7 days."""
    try:
        if _DISMISSED_FILE.exists():
            data   = json.loads(_DISMISSED_FILE.read_text())
            cutoff = time.time() - 7 * 86400
            return {url for url, ts in data.items() if ts > cutoff}
    except Exception:
        pass
    return set()


def _save_dismissed(dismissed: set[str]) -> None:
    """Persist dismissed article URLs to disk, pruning entries older than 7 days."""
    try:
        _DISMISSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        now      = time.time()
        existing: dict = {}
        if _DISMISSED_FILE.exists():
            try:
                existing = json.loads(_DISMISSED_FILE.read_text())
            except Exception:
                pass
        for url in dismissed:
            existing.setdefault(url, now)
        cutoff   = now - 7 * 86400
        existing = {u: ts for u, ts in existing.items() if ts > cutoff}
        _DISMISSED_FILE.write_text(json.dumps(existing))
    except Exception as e:
        log.debug("save dismissed failed: %s", e)


def _ddgs_to_articles(raw: list[dict], dismissed: set[str]) -> list[dict]:
    """Convert DDGS news results to article dicts, skipping denylisted/dismissed URLs."""
    articles = []
    for r in raw:
        url = r.get("url") or ""
        if not url or url in dismissed:
            continue
        if any(d in _domain(url) for d in _SOURCE_DENYLIST):
            continue
        articles.append({
            "title":    (r.get("title") or "")[:160],
            "source":   r.get("source") or "",
            "url":      url,
            "date":     (r.get("date") or "")[:10],
            "body":     (r.get("body") or "")[:700],
            "image":    r.get("image") or "",
            "category": "farming",
        })
    return articles


def _dedupe_by_url(articles: list[dict]) -> list[dict]:
    """Drop articles with a duplicate (or empty) URL, preserving order."""
    seen: set[str] = set()
    out = []
    for a in articles:
        if a["url"] and a["url"] not in seen:
            seen.add(a["url"])
            out.append(a)
    return out


# ── Cache ─────────────────────────────────────────────────────────────────────

class NewsCache:
    TTL      = 300   # 5 minutes
    MAX_FARM = 10
    MAX_GLOB = 24    # up to 6 feeds × 4 kept

    def __init__(self):
        """Init the news cache (empty state, dismissed-URL set, background refresh thread)."""
        self._farming: list[dict] = []
        self._global:  list[dict] = []
        self._fetched_at: float   = 0.0
        self._lock = threading.Lock()
        self._dismissed: set[str] = _load_dismissed()
        threading.Thread(target=self._bg_loop, daemon=True, name="news-bg").start()

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self) -> dict:
        """Return the cached news payload (articles + meta)."""
        with self._lock:
            age = time.time() - self._fetched_at
            if (self._farming or self._global) and age < self.TTL:
                return self._payload(age)
        return self._fetch()

    def refresh(self) -> dict:
        """Force a synchronous refresh of the news cache."""
        with self._lock:
            self._fetched_at = 0.0
        return self._fetch()

    def dismiss(self, url: str) -> None:
        """Mark an article URL dismissed so it is filtered from future results."""
        with self._lock:
            self._dismissed.add(url)
            self._farming = [a for a in self._farming if a["url"] != url]
            self._global  = [a for a in self._global  if a["url"] != url]
        _save_dismissed(self._dismissed)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _payload(self, age: float) -> dict:
        """Build the cache payload dict (articles, counts, updated time)."""
        items = [
            {k: v for k, v in a.items() if k != "_dt"}
            for a in ([dict(a, category="farming") for a in self._farming] + self._global)
        ]
        return {"items": items, "age_min": int(age / 60)}

    def _bg_loop(self):
        """Background loop: refresh news on the configured interval."""
        time.sleep(10)
        while True:
            with self._lock:
                age = time.time() - self._fetched_at
            if age >= self.TTL:
                try:
                    self._fetch()
                except Exception as e:
                    log.debug("news bg-refresh error: %s", e)
            time.sleep(30)

    def _fetch(self) -> dict:
        """Fetch + merge farming and global news into the cache."""
        farming = self._fetch_farming()
        glob    = self._fetch_global()
        with self._lock:
            self._farming    = farming[:self.MAX_FARM]
            self._global     = glob[:self.MAX_GLOB]
            self._fetched_at = time.time()
            log.info("news: %d farming + %d global articles", len(self._farming), len(self._global))
            return self._payload(0.0)

    def _fetch_farming(self) -> list[dict]:
        """Fetch farming news per topic via DDGS (day-then-week window), deduped."""
        try:
            from ddgs import DDGS
        except Exception as e:
            log.error("farming news fetch failed: %s", e)
            return []
        dismissed = self._dismissed.copy()
        articles: list[dict] = []
        for topic in _FARMING_TOPICS[:3]:
            for timelimit in ("d", "w"):    # last-day first, fall back to last-week
                try:
                    raw = list(DDGS().news(topic, region="in-en",
                                           timelimit=timelimit, max_results=_FARMING_MAX))
                except Exception as e:
                    log.debug("ddgs topic %r (%s): %s", topic, timelimit, e)
                    break
                if not raw:
                    continue
                articles.extend(_ddgs_to_articles(raw, dismissed))
                break   # got results — stop widening for this topic
        return _dedupe_by_url(articles)

    def _fetch_global(self) -> list[dict]:
        """Fetch global headlines from the configured RSS feeds."""
        dismissed = self._dismissed.copy()
        articles: list[dict] = []
        for name, url in _GLOBAL_FEEDS.items():
            for item in _fetch_rss(name, url, _GLOBAL_MAX):
                if item["url"] not in dismissed:
                    articles.append(item)

        seen: set[str] = set()
        deduped = []
        for a in articles:
            if a["url"] and a["url"] not in seen:
                seen.add(a["url"])
                deduped.append(a)

        # Newest first
        deduped.sort(key=lambda a: a.get("_dt", ""), reverse=True)
        return deduped
