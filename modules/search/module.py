"""
Web search module — SearXNG (self-hosted) with automatic DDGS fallback.

Set SEARXNG_URL in .env to enable the self-hosted backend, e.g.:
  docker run -d -p 8888:8080 --name searxng searxng/searxng
  SEARXNG_URL=http://localhost:8888

When SEARXNG_URL is empty, falls back to DuckDuckGo (DDGS).
"""

import logging
import time
from typing import Any

import httpx

from core.base_module import BaseModule, ModuleResponse
from core.config import SEARXNG_URL
from core.sanitizer import sanitize_external_text

log = logging.getLogger(__name__)

_SYSTEM = """You are a search assistant inside GK — a personal assistant for Ganesh, a farmer in Maharashtra, India.
You have access to live web search results. Use them to answer the user's question clearly and concisely.

RULES:
- Summarise the most relevant results in 3-5 sentences.
- Always cite the source title and URL for each key fact.
- If results are irrelevant or empty, say so honestly.
- Do not fabricate information not present in the search results.
- Prefer results from authoritative Indian sources (gov.in, APMC, ICAR, etc.) for farming/price queries.

Respond in plain text. No JSON."""

_TIMEOUT = 8


# ── Search backends ───────────────────────────────────────────────────────────

def _searxng_search(query: str, news: bool = False, max_results: int = 5) -> list[dict]:
    """Query a self-hosted SearXNG instance; returns normalised result dicts."""
    category = "news" if news else "general"
    params   = {
        "q":          query,
        "format":     "json",
        "categories": category,
        "language":   "en",
        "safesearch": "0",
    }
    try:
        resp = httpx.get(f"{SEARXNG_URL}/search", params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in (data.get("results") or [])[:max_results]:
            results.append({
                "title": r.get("title", ""),
                "href":  r.get("url", ""),
                "body":  r.get("content", ""),
            })
        log.debug("searxng %s %d results for %r", category, len(results), query[:60])
        return results
    except Exception as e:
        log.warning("searxng failed (%s): %s — falling back to DDGS", SEARXNG_URL, e)
        return []


def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """Run a DuckDuckGo web search; returns result dicts ([] on failure)."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        log.error("DDGS text search failed: %s", e)
        from core.mistake_log import log_service_failure
        log_service_failure("ddgs_search", f"text search failed: {e}")
        return []


def _ddg_news(query: str, max_results: int = 5) -> list[dict]:
    """Run a DuckDuckGo news search; returns result dicts ([] on failure)."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            return list(ddgs.news(query, max_results=max_results))
    except Exception as e:
        log.error("DDGS news search failed: %s", e)
        from core.mistake_log import log_service_failure
        log_service_failure("ddgs_search", f"news search failed: {e}")
        return []


def _search(query: str, news: bool = False, max_results: int = 5) -> tuple[list[dict], str]:
    """Try SearXNG first; fall back to DDGS. Returns (results, backend_name)."""
    if SEARXNG_URL:
        results = _searxng_search(query, news=news, max_results=max_results)
        if results:
            return results, "searxng"
    results = _ddg_news(query, max_results) if news else _ddg_search(query, max_results)
    return results, "ddgs"


# ── Format + summarise ────────────────────────────────────────────────────────

def _format_results(results: list[dict]) -> str:
    """Format search results into a numbered, source-scrubbed text block."""
    if not results:
        return "No results found."
    parts = []
    for i, r in enumerate(results, 1):
        title = sanitize_external_text(r.get("title", ""),        label="search:title")
        url   = r.get("href") or r.get("url", "")
        body  = sanitize_external_text(r.get("body", "")[:400],   label="search:body")
        parts.append(f"[{i}] {title}\n    {url}\n    {body}")
    return "\n\n".join(parts)


def _llm_summarise(query: str, search_text: str, stream: bool = False) -> str:
    """Summarise search results with the LLM under a read-only guardrail prompt."""
    from core.llm import call as llm_call
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user",   "content": f"User query: {query}\n\nSearch results:\n{search_text}"},
    ]
    t0 = time.monotonic()
    text = llm_call(
        messages, think=False,
        stream_to_stdout=stream,
        prefix="\nGK [search]: " if stream else "",
        use_fallback=True,
    )
    log.debug("LLM summarise %.0fms stream=%s", (time.monotonic() - t0) * 1000, stream)
    return text


_NEWS_WORDS = {
    "news", "latest", "today", "recent", "current", "new", "update",
    "aaj", "kal", "abhi", "khabar",
}


# ── Module ────────────────────────────────────────────────────────────────────

class SearchModule(BaseModule):
    name = "search"
    description = (
        "Answers questions requiring live web information: news, current prices, "
        "government schemes, regulations, general knowledge not in local databases."
    )

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        """Run a web/news search and return an LLM summary (or raw results on failure)."""
        t0 = time.monotonic()
        is_news = any(w in query.lower() for w in _NEWS_WORDS)

        results, backend = _search(query, news=is_news, max_results=5)
        log.info("search backend=%s kind=%s results=%d q=%r latency=%dms",
                 backend, "news" if is_news else "web", len(results),
                 query[:60], int((time.monotonic() - t0) * 1000))

        if not results:
            return ModuleResponse(
                text="Search returned no results. Try rephrasing or check your internet connection.",
                module=self.name,
            )

        search_text = _format_results(results)
        try:
            summary = _llm_summarise(query, search_text, stream=True)
            return ModuleResponse(text=summary, module=self.name, streamed=True)
        except Exception as e:
            log.error("LLM summarise failed: %s", e)
            from core.mistake_log import log_mistake
            log_mistake("llm_timeout", query=query, module=self.name,
                        details=str(e), severity="medium")
            return ModuleResponse(text=f"Search results for: {query}\n\n{search_text}",
                                  module=self.name)
