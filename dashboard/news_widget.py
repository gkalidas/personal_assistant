"""Cached agriculture / India news from DuckDuckGo."""

import logging
import time

log = logging.getLogger(__name__)

_TOPICS = [
    "india farming agriculture news today",
    "pomegranate solapur mandi market",
    "maharashtra kisan krushi",
]


class NewsCache:
    TTL      = 900    # 15 minutes
    MAX_ITEMS = 8

    def __init__(self):
        self._items: list[dict] = []
        self._fetched_at: float = 0.0

    def get(self) -> dict:
        age = time.time() - self._fetched_at
        if self._items and age < self.TTL:
            return {"items": self._items, "age_min": int(age / 60)}
        return self._fetch()

    def refresh(self) -> dict:
        self._fetched_at = 0.0
        return self._fetch()

    def _fetch(self) -> dict:
        try:
            from ddgs import DDGS
            articles: list[dict] = []
            for topic in _TOPICS[:2]:
                try:
                    for r in DDGS().news(topic, max_results=5):
                        articles.append({
                            "title":  (r.get("title") or "")[:120],
                            "source": r.get("source") or "",
                            "url":    r.get("url") or "",
                            "date":   (r.get("date") or "")[:10],
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

            self._items      = deduped[:self.MAX_ITEMS]
            self._fetched_at = time.time()
            log.info("news: fetched %d articles", len(self._items))

        except Exception as e:
            log.error("news fetch failed: %s", e)

        age = time.time() - self._fetched_at
        return {"items": self._items, "age_min": int(age / 60)}
