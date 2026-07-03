"""
Music discovery — keeps two small, cached lists fresh so the dashboard's Music
panel can help Ganesh keep up with what's new:

- "_trending"  : mainstream songs popular right now (India / Hindi-forward mix),
                 so he can keep up with the newer generation.
- "_new_music" : fresh releases in HIS taste (ghazal / sufi / soft), so he
                 discovers new songs he'll actually like.

Both are fetched via the same SearXNG + LLM-distil path the personal module uses
for "top X" lists, and cached in the profile under `personal_lists` so serving is
instant and the web is only hit on the idle refresh (security/guardian.py).
"""

import json
import logging
import threading
from datetime import datetime, timedelta

from core import memory
from modules.personal.library import _tokenize, _TASTE_SYNONYMS

log = logging.getLogger(__name__)

_TRENDING_KEY = "_trending"
_NEW_MUSIC_KEY = "_new_music"
_MAX_AGE_DAYS = 3          # trending/new lists go stale quickly
_MAX_ITEMS = 12
_DISTIL_OPTIONS = {"num_predict": 384}

_SONGS_SYSTEM = """You turn web search results into a clean list of songs.

Return ONLY JSON: {"items": [{"title": "...", "artist": "..."}]}

Rules:
- Each item is ONE real song. "title" is required; "artist" is "" if unknown.
- Prefer recent / currently popular songs; best first.
- No duplicates, no numbering, no commentary.
- Up to 12 items. Return {"items": []} if the results contain no songs."""


def _is_fresh(iso_ts: str | None) -> bool:
    """True if an ISO timestamp is within the staleness window."""
    if not iso_ts:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(iso_ts) < timedelta(days=_MAX_AGE_DAYS)
    except ValueError:
        return False


def _fetch_songs(queries: list[str], max_items: int = _MAX_ITEMS) -> list[dict]:
    """Search each query, then distil the combined results to [{title, artist}].

    Reuses the search module's SearXNG/DDG backend + sanitiser. Returns [] on any
    failure so callers degrade gracefully (an empty panel row, never a crash).
    """
    from modules.search.module import _search, _format_results
    from core.llm import call as llm_call

    results: list[dict] = []
    seen_urls: set[str] = set()
    for q in queries:
        try:
            found, _backend = _search(q, news=False, max_results=4)
        except Exception as e:
            log.debug("music search failed for %r: %s", q, e)
            continue
        for r in found:                       # dedupe across queries by URL
            u = r.get("url") or r.get("title")
            if u and u not in seen_urls:
                seen_urls.add(u)
                results.append(r)
    if not results:
        return []

    # Cap what the distiller sees — a lean prompt keeps the one LLM call fast on
    # this CPU-bound host (the results are already relevance-ordered).
    results = results[:10]
    try:
        raw = llm_call(
            [{"role": "system", "content": _SONGS_SYSTEM},
             {"role": "user", "content": f"Search results:\n{_format_results(results)}"}],
            # Runs in the background (idle task / async thread), so a generous
            # timeout is fine — better to let a slow CPU distil finish than time
            # out at 120s and fall back to the even slower model.
            format="json", think=False, use_fallback=True, options=_DISTIL_OPTIONS,
            timeout=240.0,
        )
        items = (json.loads(raw) or {}).get("items") or []
    except Exception as e:
        log.warning("music distil failed: %s", e)
        return []

    clean, seen = [], set()
    for it in items:
        if isinstance(it, str):
            it = {"title": it, "artist": ""}
        title = str((it or {}).get("title") or "").strip()
        artist = str((it or {}).get("artist") or "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append({"title": title, "artist": artist})
        if len(clean) >= max_items:
            break
    return clean


def _trending_queries() -> list[str]:
    """India/Hindi-forward mainstream trending queries for the current period."""
    year = datetime.now().year
    return [
        f"trending hindi punjabi songs this week {year}",
        f"top india songs this month {year}",
        f"viral bollywood songs right now {year}",
    ]


def _new_in_taste_queries() -> list[str]:
    """Queries for fresh releases in the user's stored music taste."""
    year = datetime.now().year
    pref = (memory.get_personal_pref("music") or {}).get("value", "")
    tokens = set(_tokenize(pref))
    # Expand via synonym bases so "gazal" etc. still triggers the ghazal query.
    genres: list[str] = []
    for base in ("ghazal", "sufi", "soft"):
        syns = set(_TASTE_SYNONYMS.get(base, ()))
        if base in tokens or tokens & syns:
            genres.append(base)
    if not genres:
        genres = ["ghazal", "sufi", "soft"]   # sensible default for his stated taste
    label = {"ghazal": "ghazal", "sufi": "sufi / qawwali", "soft": "soft romantic hindi"}
    return [f"new {label[g]} songs releases {year}" for g in genres]


def fetch_trending() -> list[dict]:
    """Fetch + cache the mainstream trending list. Returns the items."""
    items = _fetch_songs(_trending_queries())
    if items:
        memory.set_personal_list(_TRENDING_KEY, items, query="trending", source="prefetch")
    return items


def fetch_new_in_taste() -> list[dict]:
    """Fetch + cache fresh releases in the user's taste. Returns the items."""
    items = _fetch_songs(_new_in_taste_queries())
    if items:
        memory.set_personal_list(_NEW_MUSIC_KEY, items, query="new_in_taste", source="prefetch")
    return items


def refresh_music() -> dict:
    """Idle refresh: re-fetch trending/new lists only when the cache is stale.

    Called from the guardian idle task. Steady-state runs make no web calls.
    """
    refreshed = []
    if not _is_fresh((memory.get_personal_list(_TRENDING_KEY) or {}).get("updated")):
        if fetch_trending():
            refreshed.append("trending")
    if not _is_fresh((memory.get_personal_list(_NEW_MUSIC_KEY) or {}).get("updated")):
        if fetch_new_in_taste():
            refreshed.append("new_in_taste")
    return {"refreshed": refreshed}


_fetch_lock = threading.Lock()
_fetching = False


def ensure_fetched_async() -> None:
    """Fill any missing discovery list in a background thread (non-blocking).

    Lets the endpoint serve cached data instantly instead of blocking a panel
    load on a slow web+LLM fetch. At most one refresh runs at a time.
    """
    global _fetching
    with _fetch_lock:
        if _fetching:
            return
        need = (not (memory.get_personal_list(_TRENDING_KEY) or {}).get("items")
                or not (memory.get_personal_list(_NEW_MUSIC_KEY) or {}).get("items"))
        if not need:
            return
        _fetching = True

    def _run():
        global _fetching
        try:
            refresh_music()
        except Exception as e:
            log.warning("async music refresh failed: %s", e)
        finally:
            with _fetch_lock:
                _fetching = False

    threading.Thread(target=_run, daemon=True, name="music-fetch").start()


def music_feed(fetch_if_empty: bool = True) -> dict:
    """Return {trending, new_for_you, age_min} for the dashboard panel.

    Serves cached lists; if a list is empty and `fetch_if_empty`, fetches it once
    (bounded). age_min is minutes since the trending list was last updated.
    """
    trending_entry = memory.get_personal_list(_TRENDING_KEY) or {}
    new_entry = memory.get_personal_list(_NEW_MUSIC_KEY) or {}
    trending = trending_entry.get("items") or []
    new_for_you = new_entry.get("items") or []

    if fetch_if_empty and not trending:
        trending = fetch_trending()
    if fetch_if_empty and not new_for_you:
        new_for_you = fetch_new_in_taste()

    age_min = None
    updated = (memory.get_personal_list(_TRENDING_KEY) or {}).get("updated")
    if updated:
        try:
            age_min = int((datetime.now() - datetime.fromisoformat(updated)).total_seconds() // 60)
        except ValueError:
            age_min = None
    return {"trending": trending, "new_for_you": new_for_you, "age_min": age_min}
