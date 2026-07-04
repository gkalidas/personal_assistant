"""Weekly query analysis + diary auto-draft pipeline."""

import json
import os
from collections import Counter
from datetime import datetime, date, timedelta
from typing import Any

import httpx

from core.config import OLLAMA_URL, TEXT_MODEL

from core.memory import events_for_week, save_diary_draft, get_diary_draft



def iso_week(d: date) -> str:
    """Return the ISO week label 'YYYY-Www' for a date.

    Uses the ISO year (isocalendar()[0]), NOT the calendar year — they differ in
    late December / early January, so mixing them splits a single week across two
    keys. This is the one place week labels are formatted, so the photo diary and
    the weekly analyser can't disagree on which week a date belongs to.
    """
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def current_iso_week() -> str:
    """Return the current ISO week as 'YYYY-Www' (e.g. '2026-W25')."""
    return iso_week(date.today())


def _time_of_day_buckets(events: list[dict]) -> dict[str, int]:
    """Count events into morning/afternoon/evening/night buckets by their hour."""
    buckets = {"morning (6-12)": 0, "afternoon (12-17)": 0,
               "evening (17-21)": 0, "night (21-6)": 0}
    for e in events:
        try:
            h = datetime.fromisoformat(e["ts"]).hour
        except Exception:
            continue
        if 6 <= h < 12:
            buckets["morning (6-12)"] += 1
        elif 12 <= h < 17:
            buckets["afternoon (12-17)"] += 1
        elif 17 <= h < 21:
            buckets["evening (17-21)"] += 1
        else:
            buckets["night (21-6)"] += 1
    return buckets


def _transcript_events_for_week(week: str) -> list[dict]:
    """Dashboard chat turns from the session transcripts, filtered to `week`.

    The chatbox never writes to the events DB — its turns live only in the
    per-session JSONL transcripts, so the weekly analysis reads both sources.
    (CLI transcripts are excluded upstream; those turns ARE in the events DB.)
    """
    from core.chat_transcript import dashboard_turns
    out = []
    for t in dashboard_turns():
        try:
            d = datetime.fromisoformat(t["ts"]).date()
        except (TypeError, ValueError):
            continue
        if iso_week(d) == week:
            out.append(t)
    return out


def analyse_week(week: str | None = None) -> dict[str, Any]:
    """Analyse all events in a week (events DB + dashboard chat transcripts).

    Returns stats + patterns."""
    week = week or current_iso_week()
    events = events_for_week(week) + _transcript_events_for_week(week)
    events.sort(key=lambda e: e.get("ts") or "")

    if not events:
        return {"week": week, "total_queries": 0, "message": "No activity this week."}

    modules = Counter(e["module"] for e in events)
    total = len(events)
    hour_buckets = _time_of_day_buckets(events)

    # average latency per module
    latencies: dict[str, list[int]] = {}
    for e in events:
        if e.get("latency_ms"):
            latencies.setdefault(e["module"], []).append(e["latency_ms"])
    avg_latency = {m: round(sum(v) / len(v)) for m, v in latencies.items()}

    # repeated queries (same query asked >1 time)
    query_counts = Counter(e["query"].strip().lower() for e in events)
    repeated = {q: c for q, c in query_counts.items() if c > 1}

    # error rate
    errors = sum(1 for e in events if e.get("status") == "error")

    return {
        "week": week,
        "total_queries": total,
        "by_module": dict(modules.most_common()),
        "by_time_of_day": hour_buckets,
        "avg_latency_ms": avg_latency,
        "repeated_queries": repeated,
        "error_count": errors,
        "error_rate_pct": round(errors / total * 100, 1) if total else 0,
        "queries": [{"ts": e["ts"], "module": e["module"], "query": e["query"],
                     "response": e["response"][:200]} for e in events],
    }


def _llm_diary_draft(analysis: dict) -> str:
    """Ask the LLM to write a diary draft from the week's analysis."""
    summary = {
        "week": analysis["week"],
        "total_queries": analysis["total_queries"],
        "by_module": analysis.get("by_module", {}),
        "by_time_of_day": analysis.get("by_time_of_day", {}),
        "repeated_queries": list(analysis.get("repeated_queries", {}).keys())[:5],
        "sample_queries": [q["query"] for q in analysis.get("queries", [])[:15]],
    }

    prompt = f"""You are writing a weekly diary entry for Ganesh — a farmer and entrepreneur in Maharashtra.
Based on what he asked his AI assistant this week, write a short, personal diary entry (150-200 words).

Write in first person as if Ganesh is writing it. Mention:
- What he was focused on this week (based on which modules he used most)
- Any patterns you notice (time of day, repeated questions)
- One insight or reflection about the week

Keep it warm, honest, and specific. Not a bullet list — flowing prose.

Week data:
{json.dumps(summary, indent=2, ensure_ascii=False)}

Write only the diary entry. No preamble."""

    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": TEXT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": False,  # disable qwen3 extended thinking — prose task, keeps it under the timeout
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    except Exception as e:
        return f"[Diary draft failed: {e}]\n\nRaw summary: {json.dumps(summary, indent=2)}"


# Header that delimits the query-history "week in review" summary from the
# photo-based diary entries within a single week's draft. The photo diary is
# canonical: it always sits first; this summary is appended as one labeled
# section, pinned last, and is the only part run_weekly_pipeline rewrites.
WEEKLY_HEADER = "═══ Week in review ═══"


def split_weekly_section(text: str) -> tuple[str, str]:
    """Split a week's draft into (photo_part, weekly_section).

    weekly_section includes the WEEKLY_HEADER, or '' if there isn't one yet.
    """
    idx = text.find(WEEKLY_HEADER)
    if idx == -1:
        return text.rstrip(), ""
    return text[:idx].rstrip(), text[idx:].rstrip()


def run_weekly_pipeline(week: str | None = None, force: bool = False) -> dict[str, Any]:
    """Analyse the week's query history and add/refresh its 'week in review'
    summary, without touching the photo diary entries in the same week.

    The summary is one labeled section appended after the photo entries. Skips if
    that section already exists, unless force=True (which regenerates only the
    summary — the photo entries are always preserved).
    """
    week = week or current_iso_week()

    existing = get_diary_draft(week)
    existing_text = (existing or {}).get("draft") or ""
    photo_part, weekly_part = split_weekly_section(existing_text)

    if weekly_part and not force:
        return {"week": week, "skipped": True,
                "reason": "Weekly summary already added. Pass force=True to regenerate it."}

    analysis = analyse_week(week)
    if analysis["total_queries"] == 0:
        return {"week": week, "skipped": True, "reason": "No activity this week."}

    summary = _llm_diary_draft(analysis)
    section = f"{WEEKLY_HEADER}\n\n{summary}"
    merged  = f"{photo_part}\n\n{section}" if photo_part else section
    save_diary_draft(week, merged)

    return {
        "week": week,
        "analysis": analysis,
        "diary_draft": summary,
        "saved": True,
    }
