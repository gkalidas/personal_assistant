import json
import os
import re
from datetime import date
from typing import Any

import httpx

from core.base_module import BaseModule, ModuleResponse
from modules.finance import db, tools

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
TEXT_MODEL = os.getenv("TEXT_MODEL", "qwen2.5:3b")

_SYSTEM = """You are the finance advisor inside GK, a private personal assistant.
The user is Ganesh — a farmer and entrepreneur tracking personal and farm finances.
Every answer should move him toward wealth.

You have these capabilities:
- Log an expense or income
- Show monthly summary (income, expenses, net, by category)
- Set or check budgets per category
- Track savings goals

When the user wants to log a transaction, extract:
  {"action": "log", "amount": <number>, "type": "expense"|"income", "category": "<string>", "description": "<string>"}

When they want a summary:
  {"action": "summary", "month": "<YYYY-MM or null>"}

When they want budget status:
  {"action": "budget_status", "month": "<YYYY-MM or null>"}

When they want to set a budget:
  {"action": "set_budget", "category": "<string>", "monthly_cap": <number>}

When they want to add a savings goal:
  {"action": "add_goal", "name": "<string>", "target": <number>, "deadline": "<YYYY-MM-DD or null>"}

When they want to see goals:
  {"action": "list_goals"}

Respond ONLY with a JSON action object (no markdown, no explanation).
If the query is ambiguous or conversational (not a clear finance action), respond:
  {"action": "chat", "reply": "<your response>"}"""


def _call_llm(query: str, context: dict) -> dict:
    profile = context.get("profile", {})
    profile_note = ""
    if profile:
        profile_note = f"\nUser profile context: {json.dumps(profile, ensure_ascii=False)}"

    messages = [
        {"role": "system", "content": _SYSTEM + profile_note},
        {"role": "user", "content": query},
    ]
    payload = {
        "model": TEXT_MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",
    }
    resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=120.0)
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    return json.loads(content)


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
        action = _call_llm(query, context)
        text, data = _execute_action(action)
        follow_up = _FOLLOW_UPS.get(action.get("action"))
        return ModuleResponse(
            text=text,
            module=self.name,
            data=data,
            follow_up=follow_up,
        )
