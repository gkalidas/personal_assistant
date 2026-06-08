import json
import logging
import os
import re
import time
from datetime import date
from typing import Any

import httpx

from core.base_module import BaseModule, ModuleResponse
from core.memory import recent_events
from core.sanitizer import validate_action
from modules.finance import db, tools

log = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
TEXT_MODEL = os.getenv("TEXT_MODEL", "qwen3:1.7b")

_SYSTEM = """You are the finance advisor inside GK, a private personal assistant.
The user is Ganesh — a farmer and entrepreneur tracking personal and farm finances.
Every answer should move him toward wealth.

FIRST-PRINCIPLES RULE — before choosing any action, silently ask:
  1. KNOWN: What numbers/facts did the user explicitly give? (amount, month, category, goal)
  2. MISSING: What is absent but needed to act correctly?
  3. DERIVE: What can you compute from what is given?
  4. If a key fact is MISSING (how much? for which month? which category?), use {"action": "chat", "reply": "...question..."} to ask.
     Never invent amounts, rates, or scheme details.

Respond ONLY with one JSON action object. No markdown, no explanation.

Actions:
  {"action": "log", "amount": <number>, "type": "expense"|"income", "category": "<string>", "description": "<string or null>"}
  {"action": "summary", "month": "<YYYY-MM or null>"}
  {"action": "budget_status", "month": "<YYYY-MM or null>"}
  {"action": "set_budget", "category": "<string>", "monthly_cap": <number>}
  {"action": "add_goal", "name": "<string>", "target": <number>, "deadline": "<YYYY-MM-DD or null>"}
  {"action": "list_goals"}
  {"action": "chat", "reply": "<response for conversational or ambiguous queries>"}

Examples (follow this format exactly):
  User: spent 800 on drip repair
  → {"action": "log", "amount": 800, "type": "expense", "category": "Farm Maintenance", "description": "Drip repair"}

  User: earned 25000 from pomegranate sale
  → {"action": "log", "amount": 25000, "type": "income", "category": "Crop Sale", "description": "Pomegranate"}

  User: show this month's summary
  → {"action": "summary", "month": null}

  User: set budget 10000 for seeds per month
  → {"action": "set_budget", "category": "Seeds", "monthly_cap": 10000}

  User: add goal buy pump target 50000
  → {"action": "add_goal", "name": "Buy Water Pump", "target": 50000, "deadline": null}"""


def _lean_profile(context: dict) -> str:
    """One-line profile summary for the LLM — avoids dumping full JSON (tokens + privacy)."""
    p = context.get("profile", {})
    if not p:
        return ""
    name = p.get("name") or p.get("alias") or "Ganesh"
    farms = p.get("farms", [])
    crops = []
    for f in farms:
        if f.get("primary_crop"):
            crops.append(f["primary_crop"])
        crops.extend(f.get("other_crops") or [])
    crop_str = ", ".join(dict.fromkeys(crops))  # deduplicate, preserve order
    parts = [f"User: {name}"]
    if crop_str:
        parts.append(f"Crops: {crop_str}")
    if farms:
        parts.append(f"Location: {farms[0].get('district', '')}, {farms[0].get('state', 'Maharashtra')}")
    return "\n" + " | ".join(parts)


def _recent_history(limit: int = 4) -> list[dict]:
    """Fetch last N finance query/response pairs for conversation context."""
    try:
        evts = recent_events(module="finance", limit=limit * 2)
        pairs = []
        for e in reversed(evts):
            q = e.get("query", "").strip()
            r = e.get("response", "").strip()
            if q and r and len(pairs) < limit:
                pairs.append({"role": "user", "content": q})
                pairs.append({"role": "assistant", "content": r})
        return pairs
    except Exception:
        return []


def _call_llm(query: str, context: dict) -> dict:
    profile_note = _lean_profile(context)
    today = date.today().isoformat()
    profile_note += f"\nToday: {today} (use this as current date)"

    history = _recent_history(limit=4)
    messages = (
        [{"role": "system", "content": _SYSTEM + profile_note}]
        + history
        + [{"role": "user", "content": query}]
    )
    payload = {
        "model": TEXT_MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",
        "think": False,  # disable qwen3 extended thinking for faster JSON output
    }
    t0 = time.monotonic()
    resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=120.0)
    resp.raise_for_status()
    result = json.loads(resp.json()["message"]["content"])
    ms = int((time.monotonic() - t0) * 1000)
    log.debug("LLM %dms → action=%s", ms, result.get("action"))
    if ms > 30_000:
        log.warning("slow LLM response: %dms for query=%r", ms, query[:60])
    return result


def _execute_action(action: dict) -> tuple[str, dict | None]:
    """Run the parsed action against tools. Returns (human text, raw data)."""
    a = action.get("action")

    if a == "log":
        result = tools.add_transaction(
            amount=action["amount"],
            type_=action["type"],
            category=action["category"],
            description=action.get("description", ""),
        )
        sign = "+" if action["type"] == "income" else "-"
        return (
            f"Logged: {sign}₹{action['amount']:,.0f} [{action['category']}]"
            + (f" — {action['description']}" if action.get("description") else ""),
            result,
        )

    if a == "summary":
        s = tools.monthly_summary(action.get("month"))
        lines = [
            f"Summary for {s['month']}",
            f"  Income:   ₹{s['income']:>10,.0f}",
            f"  Expenses: ₹{s['expenses']:>10,.0f}",
            f"  Net:      ₹{s['net']:>10,.0f}",
        ]
        if s["breakdown"]:
            lines.append("\nBy category:")
            for cat, types in s["breakdown"].items():
                for t, amt in types.items():
                    lines.append(f"  {cat} ({t}): ₹{amt:,.0f}")
        return "\n".join(lines), s

    if a == "budget_status":
        items = tools.budget_status(action.get("month"))
        if not items:
            return "No budgets set yet. Try: 'set budget ₹5000 for groceries'", None
        lines = [f"Budget status — {action.get('month') or date.today().strftime('%Y-%m')}:"]
        for item in items:
            flag = " ⚠ OVER" if item["over_budget"] else ""
            lines.append(
                f"  {item['category']}: ₹{item['spent']:,.0f} / ₹{item['cap']:,.0f}{flag}"
            )
        return "\n".join(lines), items

    if a == "set_budget":
        result = tools.set_budget(action["category"], action["monthly_cap"])
        return f"Budget set: ₹{action['monthly_cap']:,.0f}/month for {action['category']}", result

    if a == "add_goal":
        result = tools.add_goal(
            action["name"], action["target"], action.get("deadline")
        )
        deadline_str = f" by {action['deadline']}" if action.get("deadline") else ""
        return f"Goal added: {action['name']} — ₹{action['target']:,.0f}{deadline_str}", result

    if a == "list_goals":
        goals = tools.list_goals()
        if not goals:
            return "No savings goals yet. Try: 'add goal: buy tractor, ₹2,00,000'", None
        lines = ["Savings goals:"]
        for g in goals:
            pct = (g["saved"] / g["target"] * 100) if g["target"] else 0
            lines.append(
                f"  {g['name']}: ₹{g['saved']:,.0f} / ₹{g['target']:,.0f} ({pct:.0f}%)"
                + (f" — deadline {g['deadline']}" if g.get("deadline") else "")
            )
        return "\n".join(lines), goals

    if a == "chat":
        return action.get("reply", ""), None

    return f"Unknown action: {a}", None


_FOLLOW_UPS = {
    "log": "Want to check how this affects your monthly budget?",
    "summary": "Would you like to see where you can cut back this month?",
    "budget_status": "Want me to suggest which categories to trim?",
    "add_goal": "Want to set a monthly savings amount toward this goal?",
}


class FinanceModule(BaseModule):
    name = "finance"
    description = (
        "Handles personal and farm finances: logging income/expenses, "
        "monthly summaries, budget tracking, savings goals, wealth advice."
    )

    def __init__(self):
        db.init()

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        t0 = time.monotonic()
        try:
            action = _call_llm(query, context)
        except Exception as e:
            log.error("LLM call failed: %s", e, exc_info=True)
            return ModuleResponse(
                text="I couldn't process that — please try again.",
                module=self.name,
            )

        v = validate_action("finance", action)
        action_name = v.action.get("action", "unknown")

        if not v.valid:
            log.warning("action blocked action=%s errors=%s", action_name, v.errors)
            return ModuleResponse(
                text=f"Action blocked by validator: {'; '.join(v.errors)}",
                module=self.name,
            )
        for w in v.warnings:
            log.warning("validator: %s", w)

        log.info("action=%s latency=%dms", action_name, int((time.monotonic() - t0) * 1000))
        text, data = _execute_action(v.action)
        follow_up = _FOLLOW_UPS.get(action_name)
        return ModuleResponse(
            text=text,
            module=self.name,
            data=data,
            follow_up=follow_up,
        )
