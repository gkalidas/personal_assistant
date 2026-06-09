"""Cached agriculture / India news — auto-refreshes every 2.5 minutes in background."""

import logging
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Sources known for sensationalism, partisan framing, or unreliability
# ("godi" media that distorts agricultural policy and farmer news)
_SOURCE_DENYLIST = {
    "republicworld.com", "republic.com", "republic.in",
    "indiatvnews.com", "zeenews.india.com", "zeenews.com",
    "timesnowonline.com", "timesnow.tv",
    "newsnationtv.com", "abplive.com",
    "opindia.com", "swarajyamag.com",
    "postcard.news", "rightlog.in",
    "newsbharati.com",
}

# Focused farming/agriculture queries; -site: exclusions embedded directly
_TOPICS = [
    "india farming agriculture news 2026 -site:republicworld.com -site:zeenews.com",
    "pomegranate solapur mandi market price maharashtra APMC 2026",
    "maharashtra kisan farmer news agrowon krishijagran 2026",
    "india monsoon 2026 IMD rainfall forecast farmer",
    "MSP minimum support price india kisan 2026",
]


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lstrip("www.")
    except Exception:
        return ""


class NewsCache:
    TTL       = 150   # 2.5 minutes — background thread keeps it fresh
    MAX_ITEMS = 12

    def __init__(self):
        self._items: list[dict] = []
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()
        threading.Thread(target=self._bg_loop, daemon=True, name="news-bg").start()

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self) -> dict:
        with self._lock:
            age = time.time() - self._fetched_at
            if self._items and age < self.TTL:
                return {"items": list(self._items), "age_min": int(age / 60)}
        return self._fetch()

    def refresh(self) -> dict:
        with self._lock:
            self._fetched_at = 0.0
        return self._fetch()

    # ── Background ────────────────────────────────────────────────────────────

    def _bg_loop(self):
        time.sleep(10)   # wait for server startup
        while True:
            with self._lock:
                age = time.time() - self._fetched_at
            if age >= self.TTL:
                try:
                    self._fetch()
                except Exception as e:
                    log.debug("news bg-refresh error: %s", e)
            time.sleep(30)  # check every 30s; only fetches when TTL has expired

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def _fetch(self) -> dict:
        try:
            from ddgs import DDGS
            articles: list[dict] = []
            for topic in _TOPICS[:3]:
                try:
                    for r in DDGS().news(topic, max_results=5):
                        src = _domain(r.get("url") or "")
                        if any(d in src for d in _SOURCE_DENYLIST):
                            continue
                        articles.append({
                            "title":  (r.get("title") or "")[:160],
                            "source": r.get("source") or "",
                            "url":    r.get("url") or "",
                            "date":   (r.get("date") or "")[:10],
                            "body":   (r.get("body") or "")[:700],
                            "image":  r.get("image") or "",
                        })
                except Exception as e:
                    log.debug("news topic %r: %s", topic, e)

            # Deduplicate by URL
            seen: set[str] = set()
            deduped: list[dict] = []
            for a in articles:
                if a["url"] and a["url"] not in seen:
                    seen.add(a["url"])
                    deduped.append(a)

            with self._lock:
                self._items      = deduped[:self.MAX_ITEMS]
                self._fetched_at = time.time()
            log.info("news: %d articles cached", len(self._items))

        except Exception as e:
            log.error("news fetch failed: %s", e)

        with self._lock:
            age = time.time() - self._fetched_at
            return {"items": list(self._items), "age_min": int(age / 60)}
