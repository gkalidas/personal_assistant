"""
Personal preferences module — GK learns and recalls the user's likes (music,
food, colour, and anything else), instead of guessing or refusing.

Behaviour:
- You *state* a like ("I love jazz", "my favourite colour is blue") → it's stored
  silently and briefly confirmed.
- You *ask* about a like ("what music do I like?") → GK answers from what it knows;
  if it doesn't know, it searches past chats for an earlier mention; if it still
  can't find one, it *asks you* and remembers the answer.

The multi-turn ask/answer works via a pending flag in the profile
(core.memory.set_pending_personal): once GK has asked, the router sends the next
message here (even a bare "jazz") so it lands as the answer.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Iterator

from core.base_module import BaseModule, ModuleResponse
from core import memory

log = logging.getLogger(__name__)

# Keep the extraction cheap and bounded — it only ever returns a tiny JSON object.
_EXTRACT_OPTIONS = {"num_predict": 128}

# Categories where a stated taste maps to shareable recommendations, so it's worth
# pre-fetching a "top X" list. Non-content likes (colour, sport team) are skipped.
_CONTENT_CATEGORIES = {"music", "movie", "book", "song", "artist", "show", "podcast", "genre"}
# Words that signal "give me a list" rather than store/recall of a single value.
_LIST_TRIGGERS = ("list", "top", "recommend", "suggest", "best", "playlist")
# How long a cached list stays fresh before the idle scan re-fetches it.
_LIST_MAX_AGE_DAYS = 30

_EXTRACT_SYSTEM = """You extract personal-preference intent from a short message.

Return ONLY JSON: {"intent": "...", "category": "...", "value": "..."}

intent:
- "store"  — the user is stating a preference/like (e.g. "I love jazz", "my favourite colour is blue").
- "recall" — the user is asking what their preference is (e.g. "what music do I like?", "do you know my favourite food?").
- "other"  — neither.

category: the single lowercase topic word, e.g. "music", "food", "colour", "movie",
"sport", "book". Normalise "color"->"colour". Empty string if none.

value: for "store", the preference itself (e.g. "jazz", "spicy food"). Empty for "recall"/"other".

Examples:
"I love jazz" -> {"intent":"store","category":"music","value":"jazz"}
"my favourite colour is blue" -> {"intent":"store","category":"colour","value":"blue"}
"I really like spicy Maharashtrian food" -> {"intent":"store","category":"food","value":"spicy Maharashtrian food"}
"what music do I like?" -> {"intent":"recall","category":"music","value":""}
"do you know my favourite food?" -> {"intent":"recall","category":"food","value":""}
"what's the weather" -> {"intent":"other","category":"","value":""}"""

# Patterns to mine an earlier stated preference out of past chat text. EVERY
# pattern must mention {cat} so a past "I love X" for one topic is never
# misattributed to a different one (e.g. asking about "movie" must not return an
# earlier "I love the colour green").
_PAST_PATTERNS = [
    # "my favourite food is dal", "my favourite colour are blue"
    r"my favou?rite {cat} (?:is|are)\s+([^.,;!?\n]{{2,40}})",
    # "jazz is my favourite music"
    r"([^.,;!?\n]{{2,40}})\s+is my favou?rite {cat}",
    # "I love the colour green", "I like the sport cricket"
    r"i (?:like|love|prefer|enjoy)\s+(?:the\s+)?{cat}\s+(?:is\s+|of\s+)?([^.,;!?\n]{{2,40}})",
    # "I love jazz music", "I like spicy food"
    r"i (?:like|love|prefer|enjoy)\s+([^.,;!?\n]{{2,40}}?)\s+{cat}\b",
]

# Batch extraction used by the idle scan — mines *many* messages at once for any
# stated preferences (richer than the reactive regex, still one small JSON reply).
_SCAN_SYSTEM = """You read a user's past chat messages and extract personal preferences they stated about themselves.

Return ONLY JSON: {"prefs": [{"category": "...", "value": "..."}]}

Include ONLY clear, self-stated likes/tastes (music, food, colour, movie, book, sport, hobby, etc.).
- category: single lowercase topic word (normalise "color"->"colour").
- value: the thing they like (e.g. "ghazals", "spicy food", "blue").
Ignore questions, requests, commands, and anything that is not a stated personal preference.
Return {"prefs": []} if there are none."""

# Distils raw web results into a clean recommendation list of plain item names.
_LIST_SYSTEM = """You turn web search results into a clean recommendation list.

Return ONLY JSON: {"items": ["...", "..."]}

Each item is one concrete name (a song/album/movie/book/etc.), no numbering, no commentary.
Give up to 10 items, best first. Return {"items": []} if the results contain none."""

_SCAN_OPTIONS = {"num_predict": 256}


def _chunk(seq: list, size: int) -> Iterator[list]:
    """Yield successive `size`-length slices of a list."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _is_fresh(iso_ts: str | None, max_age_days: int) -> bool:
    """True if an ISO timestamp is within max_age_days of now."""
    if not iso_ts:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(iso_ts) < timedelta(days=max_age_days)
    except ValueError:
        return False


def _fetch_top_list(category: str, value: str, max_items: int = 10) -> list[str]:
    """Web-search a "top {value} {category}" list and distil it to plain item names.

    Reuses the search module's SearXNG/DDG backend and sanitiser, then has the LLM
    reduce the results to a clean JSON list. Returns [] on any failure.
    """
    from modules.search.module import _search, _format_results
    from core.llm import call as llm_call

    query = f"top {value} {category}".strip()
    try:
        results, _backend = _search(query, news=False, max_results=6)
        if not results:
            return []
        raw = llm_call(
            [{"role": "system", "content": _LIST_SYSTEM},
             {"role": "user", "content": f"Topic: {query}\n\nSearch results:\n{_format_results(results)}"}],
            format="json", think=False, use_fallback=True, options=_SCAN_OPTIONS,
        )
        items = [str(x).strip() for x in (json.loads(raw) or {}).get("items", []) if str(x).strip()]
        return items[:max_items]
    except Exception as e:
        log.warning("top-list fetch failed for %r: %s", query, e)
        return []


def scan_recent_chats(max_events: int = 400) -> dict:
    """Idle scan: mine new chat messages for stated preferences and store them.

    Two sources, each with its own persisted cursor so a run with no new
    messages is a no-op: the events DB (CLI/voice queries) and the per-session
    transcript files (dashboard chatbox — those never reach the events DB).
    A directly stated/asked value is never overwritten by a chat-mined guess.
    Returns a small summary for the guardian log.
    """
    from core.llm import call as llm_call
    from core.chat_transcript import user_messages_since

    cursor = memory.get_personal_scan_cursor()
    events = memory.events_since_id(cursor, limit=max_events)

    t_cursor = memory.get_transcript_scan_cursor()
    t_texts, t_new_cursor = user_messages_since(t_cursor, max_messages=max_events)

    if not events and not t_texts:
        return {"scanned": 0, "learned": [], "cursor": cursor}

    texts = [(e.get("query") or "").strip() for e in events]
    texts = [t for t in texts if t] + t_texts
    learned: list[dict] = []

    for batch in _chunk(texts, 20):
        joined = "\n".join(f"- {t}" for t in batch)
        try:
            raw = llm_call(
                [{"role": "system", "content": _SCAN_SYSTEM},
                 {"role": "user", "content": joined}],
                format="json", think=False, use_fallback=True, options=_SCAN_OPTIONS,
            )
            prefs = (json.loads(raw) or {}).get("prefs") or []
        except Exception as e:
            log.warning("personal scan batch failed: %s", e)
            continue
        for p in prefs:
            cat = str(p.get("category") or "").strip().lower()
            val = str(p.get("value") or "").strip()
            if not cat or not val:
                continue
            existing = memory.get_personal_pref(cat)
            # A value the user stated or answered directly outranks a chat guess.
            if existing and existing.get("source") in ("stated", "asked"):
                continue
            if existing and existing.get("value", "").lower() == val.lower():
                continue
            memory.set_personal_pref(cat, val, source="chat")
            learned.append({"category": cat, "value": val})

    new_cursor = max(e["id"] for e in events) if events else cursor
    memory.set_personal_scan_cursor(new_cursor)
    if t_new_cursor != t_cursor:
        memory.set_transcript_scan_cursor(t_new_cursor)
    return {"scanned": len(texts), "learned": learned, "cursor": new_cursor}


def refresh_content_lists() -> dict:
    """Pre-fetch "top X" lists for known content tastes (music/movie/book/…).

    Skips a category whose cached list is still fresh and matches the current
    taste, so steady-state idle runs make no web calls. Called after
    scan_recent_chats() in the idle task.
    """
    refreshed: list[dict] = []
    for cat, entry in memory.get_personal_prefs().items():
        if cat not in _CONTENT_CATEGORIES:
            continue
        value = (entry or {}).get("value", "").strip()
        if not value:
            continue
        wanted_query = f"top {value} {cat}"
        cached = memory.get_personal_list(cat)
        if cached and cached.get("query") == wanted_query and _is_fresh(cached.get("updated"), _LIST_MAX_AGE_DAYS):
            continue
        items = _fetch_top_list(cat, value)
        if items:
            memory.set_personal_list(cat, items, query=wanted_query, source="prefetch")
            refreshed.append({"category": cat, "count": len(items)})
    return {"refreshed": refreshed}


class PersonalModule(BaseModule):
    name = "personal"
    description = (
        "Learns and recalls the user's personal preferences and likes (music, "
        "food, colour, movies, sports, etc.). Stores what the user states, and "
        "asks when a preference is unknown."
    )

    # ── intent extraction ─────────────────────────────────────────────────────
    def _extract(self, query: str) -> dict:
        """Ask the LLM for {intent, category, value}; degrade gracefully on failure."""
        from core.llm import call as llm_call
        try:
            raw = llm_call(
                [
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": query},
                ],
                format="json",
                think=False,
                use_fallback=True,
                options=_EXTRACT_OPTIONS,
            )
            data = json.loads(raw)
            return {
                "intent": str(data.get("intent") or "other").lower(),
                "category": str(data.get("category") or "").strip().lower(),
                "value": str(data.get("value") or "").strip(),
            }
        except Exception as e:
            log.warning("personal extract failed: %s", e)
            return {"intent": "other", "category": "", "value": ""}

    def _search_past_chats(self, category: str) -> str | None:
        """Scan recent chat history for an earlier statement of this preference."""
        cat = re.escape(category)
        patterns = [p.format(cat=cat) for p in _PAST_PATTERNS]
        for ev in memory.recent_events(limit=200):
            text = (ev.get("query") or "")
            for pat in patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    value = m.group(1).strip().strip(".").strip()
                    # Tidy leftover articles / a stray category word.
                    value = re.sub(r"^\s*(?:the|a|an)\s+", "", value, flags=re.IGNORECASE)
                    value = re.sub(rf"\b{cat}\b", "", value, flags=re.IGNORECASE).strip()
                    if value:
                        return value
        return None

    # ── list / recommendation serving ─────────────────────────────────────────
    def _list_request_category(self, query: str) -> str | None:
        """Return the content category a "top X"/"recommend" request targets, else None.

        Resolves the category from what we already know about the user, so it fires
        only when there's a stored taste to serve a list for.
        """
        q = f" {query.lower()} "
        if not any(t in q for t in _LIST_TRIGGERS):
            return None
        prefs = memory.get_personal_prefs()
        # (a) query names a content category we know a taste for ("top movies")
        for cat in _CONTENT_CATEGORIES:
            if cat in q and (prefs.get(cat) or {}).get("value"):
                return cat
        # (b) query names the taste value itself ("top ghazals")
        for cat, entry in prefs.items():
            val = (entry or {}).get("value", "").lower()
            if cat in _CONTENT_CATEGORIES and val and val in q:
                return cat
        # (c) self-referential request with exactly one known content taste
        if any(w in q for w in (" me ", " my ", " i ")):
            content = [c for c in prefs if c in _CONTENT_CATEGORIES and (prefs[c] or {}).get("value")]
            if len(content) == 1:
                return content[0]
        return None

    def _serve_list(self, category: str) -> ModuleResponse:
        """Return the cached pre-fetched list for a taste, building one live if absent.

        For music, songs the user already owns (config.MUSIC_DIR) are listed first
        — "in your collection" — before web recommendations, honouring his wish to
        pick from the local collection before searching online.
        """
        value = (memory.get_personal_pref(category) or {}).get("value", "")

        local_lines: list[str] = []
        if category == "music":
            try:
                from modules.personal.library import local_matches
                owned = local_matches(value, limit=10)
                local_lines = [
                    (f"{t['title']} — {t['artist']}" if t.get("artist") else t["title"])
                    for t in owned
                ]
            except Exception as e:
                log.debug("local music match skipped: %s", e)

        cached = memory.get_personal_list(category)
        items = cached.get("items") if cached else None
        if not items:
            items = _fetch_top_list(category, value)
            if items:
                memory.set_personal_list(
                    category, items, query=f"top {value} {category}", source="ondemand")

        if not items and not local_lines:
            return ModuleResponse(
                text=f"I couldn't pull a {value} {category} list right now — try again shortly.",
                module=self.name,
            )

        sections = []
        if local_lines:
            owned_body = "\n".join(f"{i}. {x}" for i, x in enumerate(local_lines, 1))
            sections.append(f"In your collection:\n{owned_body}")
        if items:
            web_body = "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1))
            header = "More to explore:" if local_lines else f"Top {value} {category} for you:"
            sections.append(f"{header}\n{web_body}")
        return ModuleResponse(
            text="\n\n".join(sections),
            module=self.name,
            data={"category": category, "value": value,
                  "local": local_lines, "items": items or []},
        )

    # ── main handler ──────────────────────────────────────────────────────────
    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        # 1) Pending answer capture: GK previously asked about a category, so this
        #    message is the answer (even a bare "jazz").
        pending = memory.get_pending_personal()
        if pending:
            memory.set_personal_pref(pending, query, source="asked")
            memory.clear_pending_personal()
            return ModuleResponse(
                text=f"Got it — I'll remember your {pending}: {query.strip()}.",
                module=self.name,
            )

        # 1b) List/recommendation request tied to the user's taste ("top ghazals",
        #     "recommend music for me") → serve a pre-fetched list if we have one,
        #     else build (and cache) one live.
        list_cat = self._list_request_category(query)
        if list_cat:
            return self._serve_list(list_cat)

        info = self._extract(query)
        intent, category, value = info["intent"], info["category"], info["value"]

        # 2) User stated a like → store silently + confirm briefly.
        if intent == "store" and category and value:
            memory.set_personal_pref(category, value, source="stated")
            return ModuleResponse(
                text=f"Got it — I'll remember you like {value} ({category}).",
                module=self.name,
            )

        # 3) User asked about a like.
        if intent == "recall" and category:
            known = memory.get_personal_pref(category)
            if known and known.get("value"):
                return ModuleResponse(
                    text=f"Your {category}: {known['value']}.",
                    module=self.name,
                )
            # Look through past chats before giving up.
            found = self._search_past_chats(category)
            if found:
                memory.set_personal_pref(category, found, source="chat")
                return ModuleResponse(
                    text=f"You mentioned earlier that your {category} is {found} — I've saved it.",
                    module=self.name,
                )
            # Unknown: ask, and remember that we're waiting for the answer.
            memory.set_pending_personal(category)
            return ModuleResponse(
                text=f"I don't know that yet — what {category} do you like?",
                module=self.name,
            )

        # 4) Not clearly a preference statement/question.
        return ModuleResponse(
            text="Tell me about a preference (like your favourite music, food, or "
                 "colour) and I'll remember it — or ask me what you like.",
            module=self.name,
        )

    def handle_stream(self, query: str, context: dict[str, Any]) -> Iterator[str]:
        """Emit the reply as a single chunk so the SSE endpoint works uniformly."""
        yield self.handle(query, context).text
