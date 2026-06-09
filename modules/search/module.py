"""
Web search module — DuckDuckGo (privacy-first, no API key needed).

Routes queries that need current/external information not in the local KB:
news, prices, regulations, generic "what is X" questions, etc.
"""

import json
import logging
import time
from typing import Any

from core.base_module import BaseModule, ModuleResponse

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


def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """Run a DuckDuckGo search, return list of {title, href, body}."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results
    except Exception as e:
        log.error("DDG search failed: %s", e)
        return []


def _ddg_news(query: str, max_results: int = 5) -> list[dict]:
    """DuckDuckGo news search."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.news(query, max_results=max_results))
        return results
    except Exception as e:
        log.error("DDG news failed: %s", e)
        return []


def _format_results(results: list[dict]) -> str:
    if not results:
        return "No results found."
    parts = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url   = r.get("href") or r.get("url", "")
        body  = r.get("body", "")[:200]
        parts.append(f"[{i}] {title}\n    {url}\n    {body}")
    return "\n\n".join(parts)


def _llm_summarise(query: str, search_text: str, stream: bool = False) -> str:
    from core.llm import call as llm_call
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user",   "content": f"User query: {query}\n\nSearch results:\n{search_text}"},
    ]
    t0 = time.monotonic()
    text = llm_call(
        messages, think=False,
        stream_to_stdout=stream,
        prefix=f"\nGK [search]: " if stream else "",
        use_fallback=True,
    )
    log.debug("LLM summarise %.0fms stream=%s", (time.monotonic() - t0) * 1000, stream)
    return text


_NEWS_WORDS = {
    "news", "latest", "today", "recent", "current", "new", "update",
    "aaj", "kal", "abhi", "khabar",  # Hindi/Marathi equivalents
}


class SearchModule(BaseModule):
    name = "search"
    description = (
        "Answers questions requiring live web information: news, current prices, "
        "government schemes, regulations, general knowledge not in local databases."
    )

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        t0 = time.monotonic()
        q_lower = query.lower()
        is_news = any(w in q_lower for w in _NEWS_WORDS)

        if is_news:
            results = _ddg_news(query, max_results=5)
            kind = "news"
        else:
            results = _ddg_search(query, max_results=5)
            kind = "web"

        log.info("search kind=%s results=%d q=%r latency=%dms",
                 kind, len(results), query[:60], int((time.monotonic() - t0) * 1000))

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
            summary = f"Search results for: {query}\n\n{search_text}"
            return ModuleResponse(text=summary, module=self.name)
