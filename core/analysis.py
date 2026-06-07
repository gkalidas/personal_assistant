"""Weekly query analysis + diary auto-draft pipeline."""

import json
import os
from collections import Counter
from datetime import datetime, date, timedelta
from typing import Any

import httpx

from core.memory import events_for_week, save_diary_draft, get_diary_draft

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
TEXT_MODEL = os.getenv("TEXT_MODEL", "qwen2.5:3b")


def current_iso_week() -> str:
    today = date.today()
    return f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"


def analyse_week(week: str | None = None) -> dict[str, Any]:
    """Analyse all events in a week. Returns stats + patterns."""
    week = week or current_iso_week()
    events = events_for_week(week)

    if not events:
        return {"week": week, "total_queries": 0, "message": "No activity this week."}

    modules = Counter(e["module"] for e in events)
    total = len(events)

    # time-of-day distribution
    hour_buckets = {"morning (6-12)": 0, "afternoon (12-17)": 0,
                    "evening (17-21)": 0, "night (21-6)": 0}
    for e in events:
        try:
            h = datetime.fromisoformat(e["ts"]).hour
            if 6 <= h < 12:
                hour_buckets["morning (6-12)"] += 1
            elif 12 <= h < 17:
                hour_buckets["afternoon (12-17)"] += 1
            elif 17 <= h < 21:
                hour_buckets["evening (17-21)"] += 1
            else:
                hour_buckets["night (21-6)"] += 1
        except Exception:
            pass

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
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    except Exception as e:
        return f"[Diary draft failed: {e}]\n\nRaw summary: {json.dumps(summary, indent=2)}"


def run_weekly_pipeline(week: str | None = None, force: bool = False) -> dict[str, Any]:
    """Full pipeline: analyse week → draft diary entry → save.
    Skips if draft already exists for the week (unless force=True).
    """
    week = week or current_iso_week()

    existing = get_diary_draft(week)
    if existing and not force:
        return {"week": week, "skipped": True, "reason": "Draft already exists. Pass force=True to regenerate."}

    analysis = analyse_week(week)
    if analysis["total_queries"] == 0:
        return {"week": week, "skipped": True, "reason": "No activity this week."}

    draft = _llm_diary_draft(analysis)
    save_diary_draft(week, draft)

    return {
        "week": week,
        "analysis": analysis,
        "diary_draft": draft,
        "saved": True,
    }
