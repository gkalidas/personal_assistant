import json
import os
from datetime import date
from typing import Any

import httpx

from core.base_module import BaseModule, ModuleResponse
from core.memory import recent_events
from core.sanitizer import validate_action
from modules.health import db, tools

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
TEXT_MODEL = os.getenv("TEXT_MODEL", "qwen3:1.7b")

_SYSTEM = """You are the health advisor inside GK — a private personal assistant for Ganesh.
Your ONLY job is to track his health data and give grounded, safe observations.

FIRST-PRINCIPLES RULE — before choosing any action, silently ask:
  1. KNOWN: What health data did the user explicitly state? (numbers, type, time)
  2. MISSING: Is a crucial number absent (no BP value? no step count?)?
  3. SAFETY: Never diagnose, never prescribe, never say "you have [disease]".
  4. If data is MISSING, use {"action": "chat", "reply": "..."} to ask for it.
     Never invent or assume health numbers.

Respond ONLY with one JSON action object. No markdown, no explanation.

Actions:
  {"action": "log_bp", "systolic": <int>, "diastolic": <int>, "notes": "<string or null>"}
  {"action": "log_steps", "count": <int>}
  {"action": "log_weight", "kg": <float>}
  {"action": "log_sleep", "hours": <float>}
  {"action": "log_sugar", "mg_dl": <float>, "meal_state": "fasting|post_meal|random"}
  {"action": "history", "type": "bp|steps|weight|sleep|sugar", "days": <int>}
  {"action": "summary"}
  {"action": "trend", "type": "bp|steps|weight|sleep|sugar", "days": <int>}
  {"action": "set_goal", "type": "steps|weight|sleep", "target": <float>}
  {"action": "chat", "reply": "<safe response or clarifying question>"}

Examples (follow this format exactly):
  User: BP was 130 over 85
  → {"action": "log_bp", "systolic": 130, "diastolic": 85, "notes": null}

  User: walked 8500 steps today
  → {"action": "log_steps", "count": 8500}

  User: weight 74.5 kg this morning
  → {"action": "log_weight", "kg": 74.5}

  User: slept 6.5 hours last night
  → {"action": "log_sleep", "hours": 6.5}

  User: fasting sugar 105
  → {"action": "log_sugar", "mg_dl": 105, "meal_state": "fasting"}

  User: show my BP history last 2 weeks
  → {"action": "history", "type": "bp", "days": 14}

  User: how are my steps trending
  → {"action": "trend", "type": "steps", "days": 14}

  User: today's health summary
  → {"action": "summary"}

  User: my sugar was high today
  → {"action": "chat", "reply": "What was the reading (mg/dL)? And was it fasting or after a meal?"}"""


def _profile_note(context: dict) -> str:
    health = context.get("profile", {}).get("health", {})
    lines = [f"Today: {date.today().isoformat()}"]
    age = health.get("age")
    if age:
        lines.append(f"Age: {age}")
    conditions = health.get("conditions") or []
    if conditions:
        lines.append(f"Known conditions: {', '.join(conditions)}")
    medications = health.get("medications") or []
    if medications:
        lines.append(f"Medications: {', '.join(medications)}")
    goals = tools.get_goals()
    if goals:
        goal_parts = []
        for t, g in goals.items():
            goal_parts.append(f"{t}={g['target']}{g.get('unit','')}")
        lines.append(f"Health goals: {', '.join(goal_parts)}")
    return "\n" + "\n".join(lines)


def _recent_history(limit: int = 4) -> list[dict]:
    try:
        evts = recent_events(module="health", limit=limit * 2)
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
    extras = _profile_note(context)
    history = _recent_history(limit=4)
    payload = {
        "model": TEXT_MODEL,
        "messages": (
            [{"role": "system", "content": _SYSTEM + extras}]
            + history
            + [{"role": "user", "content": query}]
        ),
        "stream": False,
        "format": "json",
        "think": False,
    }
    resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=120.0)
    resp.raise_for_status()
    return json.loads(resp.json()["message"]["content"])


# ── Format helpers ────────────────────────────────────────────────────────────

def _fmt_bp_row(r: dict) -> str:
    systolic = int(r["value1"])
    diastolic = int(r["value2"]) if r.get("value2") else "?"
    cat, _ = tools.interpret_bp(systolic, float(r.get("value2") or 80))
    return f"  {r['date']} {r['time']} — {systolic}/{diastolic} mmHg [{cat}]"


def _fmt_steps_row(r: dict) -> str:
    cat, note = tools.interpret_steps(int(r["value1"]))
    return f"  {r['date']} — {int(r['value1']):,} steps [{cat}]"


def _fmt_generic_row(r: dict, unit: str = "") -> str:
    v = r["value1"]
    u = r.get("unit") or unit
    return f"  {r['date']} {r['time']} — {v} {u}"


# ── Execute actions ───────────────────────────────────────────────────────────

def _execute(action: dict) -> tuple[str, dict | None]:
    a = action.get("action")

    if a == "log_bp":
        s = int(action.get("systolic", 0))
        d = int(action.get("diastolic", 0))
        if not s or not d:
            return "BP reading incomplete — need both systolic and diastolic values.", None
        r = tools.log_reading("bp", s, d, "mmHg", notes=action.get("notes"))
        cat, advice = tools.interpret_bp(s, d)
        text = f"BP logged: {s}/{d} mmHg [{cat}]\n  {advice}"
        return text, r

    if a == "log_steps":
        count = int(action.get("count", 0))
        if count <= 0:
            return "How many steps? Please provide the count.", None
        r = tools.log_reading("steps", count, unit="steps")
        goals = tools.get_goals()
        goal = int(goals.get("steps", {}).get("target", 10000))
        cat, note = tools.interpret_steps(count, goal)
        text = f"Steps logged: {count:,} — {cat}"
        if count < goal:
            text += f"\n  {goal - count:,} more to reach your {goal:,} goal."
        return text, r

    if a == "log_weight":
        kg = float(action.get("kg", 0))
        if kg <= 0:
            return "Please provide your weight in kg.", None
        r = tools.log_reading("weight", kg, unit="kg")
        prev = tools.get_history("weight", days=7)
        if len(prev) >= 2:
            diff = round(kg - prev[1]["value1"], 1)
            direction = "▲" if diff > 0 else "▼"
            return f"Weight logged: {kg} kg ({direction} {abs(diff)} kg from last reading)", r
        return f"Weight logged: {kg} kg", r

    if a == "log_sleep":
        hours = float(action.get("hours", 0))
        if hours <= 0:
            return "How many hours did you sleep? Please provide a number.", None
        r = tools.log_reading("sleep", hours, unit="hours")
        cat, advice = tools.interpret_sleep(hours)
        return f"Sleep logged: {hours}h — {cat}\n  {advice}", r

    if a == "log_sugar":
        mg_dl = float(action.get("mg_dl", 0))
        if mg_dl <= 0:
            return "Please provide the glucose reading in mg/dL.", None
        meal_state = action.get("meal_state") or "random"
        r = tools.log_reading("sugar", mg_dl, unit="mg/dL", meal_state=meal_state)
        cat, advice = tools.interpret_sugar(mg_dl, meal_state)
        label = {"fasting": "fasting", "post_meal": "post-meal", "random": "random"}.get(meal_state, "")
        return f"Blood sugar logged: {mg_dl} mg/dL ({label}) — {cat}\n  {advice}", r

    if a == "history":
        type_ = action.get("type", "bp")
        days = int(action.get("days", 7))
        rows = tools.get_history(type_, days)
        if not rows:
            return f"No {type_} readings in the last {days} days.", None
        label_map = {"bp": "Blood Pressure", "steps": "Steps", "weight": "Weight",
                     "sleep": "Sleep", "sugar": "Blood Sugar"}
        label = label_map.get(type_, type_.upper())
        lines = [f"{label} — last {days} days:"]
        for r in rows[:20]:
            if type_ == "bp":
                lines.append(_fmt_bp_row(r))
            elif type_ == "steps":
                lines.append(_fmt_steps_row(r))
            else:
                lines.append(_fmt_generic_row(r, r.get("unit", "")))
        return "\n".join(lines), rows

    if a == "summary":
        s = tools.today_summary()
        if not s["readings"]:
            return f"No health readings logged yet today ({s['date']}).\nLog your BP, steps, weight, sleep, or sugar to get started.", None
        lines = [f"Health summary — {s['date']}:"]
        for rd in s["readings"]:
            t = rd["type"]
            v1 = rd["value1"]
            v2 = rd.get("value2")
            u  = rd.get("unit", "")
            if t == "bp" and v2:
                cat, _ = tools.interpret_bp(v1, v2)
                lines.append(f"  BP: {int(v1)}/{int(v2)} mmHg [{cat}] at {rd['time']}")
            elif t == "steps":
                lines.append(f"  Steps: {int(v1):,} at {rd['time']}")
            elif t == "weight":
                lines.append(f"  Weight: {v1} kg at {rd['time']}")
            elif t == "sleep":
                lines.append(f"  Sleep: {v1}h")
            elif t == "sugar":
                ms = rd.get("meal_state") or "random"
                lines.append(f"  Sugar: {v1} mg/dL ({ms}) at {rd['time']}")
            else:
                lines.append(f"  {t}: {v1} {u}")
        return "\n".join(lines), s

    if a == "trend":
        type_ = action.get("type", "steps")
        days  = int(action.get("days", 14))
        rows  = tools.daily_trend(type_, days)
        if not rows:
            return f"No {type_} data in the last {days} days to show trend.", None
        label_map = {"bp": "BP", "steps": "Steps", "weight": "Weight", "sleep": "Sleep", "sugar": "Sugar"}
        label = label_map.get(type_, type_)
        lines = [f"{label} trend — last {days} days:"]
        for r in rows:
            v1 = r["avg_v1"]
            v2 = r.get("avg_v2")
            if type_ == "bp" and v2:
                lines.append(f"  {r['date']}: {int(v1)}/{int(v2)} mmHg (avg, {r['readings']} readings)")
            elif type_ == "steps":
                lines.append(f"  {r['date']}: {int(v1):,} steps")
            else:
                lines.append(f"  {r['date']}: {v1}")
        return "\n".join(lines), rows

    if a == "set_goal":
        type_ = action.get("type")
        target = float(action.get("target", 0))
        if not type_ or target <= 0:
            return "Please specify goal type (steps/weight/sleep) and target value.", None
        unit_map = {"steps": "steps/day", "weight": "kg", "sleep": "hours/night"}
        unit = unit_map.get(type_, "")
        r = tools.set_goal(type_, target, unit)
        return f"Goal set: {type_} = {target} {unit}", r

    if a == "chat":
        return action.get("reply", ""), None

    return f"Unknown action: {a}", None


_FOLLOW_UPS = {
    "log_bp":     "Want to see your BP trend over the past 2 weeks?",
    "log_steps":  "Want to see your steps trend this week?",
    "log_weight": "Want to see your weight trend?",
    "log_sleep":  "Want to see your sleep pattern this week?",
    "log_sugar":  "Want to see your blood sugar history?",
    "summary":    "Want to see trends for any specific metric?",
}


class HealthModule(BaseModule):
    name = "health"
    description = (
        "Tracks personal health data: blood pressure (BP), daily steps, weight, "
        "sleep hours, blood glucose (sugar). Logs readings, shows history, trends, "
        "and gives safe observations — never diagnoses or prescribes."
    )

    def __init__(self):
        db.init()

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        action = _call_llm(query, context)
        v = validate_action("health", action)
        if not v.valid:
            return ModuleResponse(
                text=f"Action blocked: {'; '.join(v.errors)}",
                module=self.name,
            )
        if v.warnings:
            for w in v.warnings:
                print(f"  [health validator] {w}")
        text, data = _execute(v.action)
        follow_up = _FOLLOW_UPS.get(v.action.get("action"))
        return ModuleResponse(text=text, module=self.name, data=data, follow_up=follow_up)
