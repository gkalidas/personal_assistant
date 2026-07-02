import json
import logging
import time
from datetime import date
from typing import Any

import httpx

from core.config import OLLAMA_URL, TEXT_MODEL

from core.base_module import BaseModule, ModuleResponse
from core.memory import recent_events
from core.sanitizer import validate_action, sanitize_external_text
from modules.health import db, tools

log = logging.getLogger(__name__)


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
  {"action": "nutrition", "topic": "<general topic, e.g. protein intake, diabetes diet, iron deficiency>"}
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

  User: what foods are good for a diabetic?
  → {"action": "nutrition", "topic": "diabetes diet"}

  User: how much protein do I need daily?
  → {"action": "nutrition", "topic": "protein intake"}

  User: my sugar was high today
  → {"action": "chat", "reply": "What was the reading (mg/dL)? And was it fasting or after a meal?"}"""


def _profile_note(context: dict) -> str:
    """Render a short health-profile context block for the LLM system prompt."""
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
    """Return the last few health query/response pairs for conversation context (sanitized)."""
    try:
        evts = recent_events(module="health", limit=limit * 2)
        pairs = []
        for e in reversed(evts):
            q = e.get("query", "").strip()
            r = e.get("response", "").strip()
            if q and r and len(pairs) < limit:
                pairs.append({"role": "user",      "content": q})
                pairs.append({"role": "assistant",  "content": sanitize_external_text(r, label="mem:health")})
        return pairs
    except Exception:
        return []


def _call_llm(query: str, context: dict) -> dict:
    """Call the health LLM with profile + history and return the parsed action dict."""
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
    t0 = time.monotonic()
    resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=120.0)
    resp.raise_for_status()
    result = json.loads(resp.json()["message"]["content"])
    ms = int((time.monotonic() - t0) * 1000)
    log.debug("LLM %dms → action=%s", ms, result.get("action"))
    if ms > 30_000:
        log.warning("slow LLM response: %dms for query=%r", ms, query[:60])
    return result


# ── Format helpers ────────────────────────────────────────────────────────────

def _fmt_bp_row(r: dict) -> str:
    """Format a BP reading row as a display line."""
    systolic = int(r["value1"])
    diastolic = int(r["value2"]) if r.get("value2") else "?"
    cat, _ = tools.interpret_bp(systolic, float(r.get("value2") or 80))
    return f"  {r['date']} {r['time']} — {systolic}/{diastolic} mmHg [{cat}]"


def _fmt_steps_row(r: dict) -> str:
    """Format a steps reading row as a display line."""
    cat, note = tools.interpret_steps(int(r["value1"]))
    return f"  {r['date']} — {int(r['value1']):,} steps [{cat}]"


def _fmt_generic_row(r: dict, unit: str = "") -> str:
    """Format a generic reading row as a display line."""
    v = r["value1"]
    u = r.get("unit") or unit
    return f"  {r['date']} {r['time']} — {v} {u}"


# ── Per-action handlers ───────────────────────────────────────────────────────

def _handle_log_bp(action: dict) -> tuple[str, dict | None]:
    """Handler: log a blood-pressure reading with interpretation."""
    s = int(action.get("systolic", 0))
    d = int(action.get("diastolic", 0))
    if not s or not d:
        return "BP reading incomplete — need both systolic and diastolic values.", None
    r = tools.log_reading("bp", s, d, "mmHg", notes=action.get("notes"))
    cat, advice = tools.interpret_bp(s, d)
    return f"BP logged: {s}/{d} mmHg [{cat}]\n  {advice}", r


def _handle_log_steps(action: dict) -> tuple[str, dict | None]:
    """Handler: log a step count with goal progress."""
    count = int(action.get("count", 0))
    if count <= 0:
        return "How many steps? Please provide the count.", None
    r = tools.log_reading("steps", count, unit="steps")
    goals = tools.get_goals()
    goal  = int(goals.get("steps", {}).get("target", 10000))
    cat, _ = tools.interpret_steps(count, goal)
    text = f"Steps logged: {count:,} — {cat}"
    if count < goal:
        text += f"\n  {goal - count:,} more to reach your {goal:,} goal."
    return text, r


def _handle_log_weight(action: dict) -> tuple[str, dict | None]:
    """Handler: log a weight reading and compare to the last."""
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


def _handle_log_sleep(action: dict) -> tuple[str, dict | None]:
    """Handler: log sleep hours with interpretation."""
    hours = float(action.get("hours", 0))
    if hours <= 0:
        return "How many hours did you sleep? Please provide a number.", None
    r = tools.log_reading("sleep", hours, unit="hours")
    cat, advice = tools.interpret_sleep(hours)
    return f"Sleep logged: {hours}h — {cat}\n  {advice}", r


def _handle_log_sugar(action: dict) -> tuple[str, dict | None]:
    """Handler: log a blood-sugar reading with interpretation."""
    mg_dl = float(action.get("mg_dl", 0))
    if mg_dl <= 0:
        return "Please provide the glucose reading in mg/dL.", None
    meal_state = action.get("meal_state") or "random"
    r = tools.log_reading("sugar", mg_dl, unit="mg/dL", meal_state=meal_state)
    cat, advice = tools.interpret_sugar(mg_dl, meal_state)
    label = {"fasting": "fasting", "post_meal": "post-meal", "random": "random"}.get(meal_state, "")
    return f"Blood sugar logged: {mg_dl} mg/dL ({label}) — {cat}\n  {advice}", r


def _handle_history(action: dict) -> tuple[str, dict | None]:
    """Handler: show recent history for a reading type."""
    type_ = action.get("type", "bp")
    days  = int(action.get("days", 7))
    rows  = tools.get_history(type_, days)
    if not rows:
        return f"No {type_} readings in the last {days} days.", None
    label = {"bp": "Blood Pressure", "steps": "Steps", "weight": "Weight",
             "sleep": "Sleep", "sugar": "Blood Sugar"}.get(type_, type_.upper())
    lines = [f"{label} — last {days} days:"]
    for r in rows[:20]:
        if type_ == "bp":
            lines.append(_fmt_bp_row(r))
        elif type_ == "steps":
            lines.append(_fmt_steps_row(r))
        else:
            lines.append(_fmt_generic_row(r, r.get("unit", "")))
    return "\n".join(lines), rows


def _handle_summary(action: dict) -> tuple[str, dict | None]:
    """Handler: summarise today's readings."""
    s = tools.today_summary()
    if not s["readings"]:
        return (f"No health readings logged yet today ({s['date']}).\n"
                "Log your BP, steps, weight, sleep, or sugar to get started."), None
    lines = [f"Health summary — {s['date']}:"]
    for rd in s["readings"]:
        t  = rd["type"]
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


def _handle_trend(action: dict) -> tuple[str, dict | None]:
    """Handler: show a multi-day trend for a reading type."""
    type_ = action.get("type", "steps")
    days  = int(action.get("days", 14))
    rows  = tools.daily_trend(type_, days)
    if not rows:
        return f"No {type_} data in the last {days} days to show trend.", None
    label = {"bp": "BP", "steps": "Steps", "weight": "Weight",
             "sleep": "Sleep", "sugar": "Sugar"}.get(type_, type_)
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


def _handle_set_goal(action: dict) -> tuple[str, dict | None]:
    """Handler: set a goal target for a reading type."""
    type_  = action.get("type")
    target = float(action.get("target", 0))
    if not type_ or target <= 0:
        return "Please specify goal type (steps/weight/sleep) and target value.", None
    unit = {"steps": "steps/day", "weight": "kg", "sleep": "hours/night"}.get(type_, "")
    r = tools.set_goal(type_, target, unit)
    return f"Goal set: {type_} = {target} {unit}", r


_NUTRITION_TIPS: list[tuple[tuple[str, ...], list[str]]] = [
    (
        ("diabet", "sugar", "glucose"),
        [
            "  Diabetes-friendly eating principles:",
            "  • Choose low-glycaemic-index foods: lentils, legumes, oats, brown rice, vegetables",
            "  • Limit refined carbs: white rice, maida, sugary drinks, sweets",
            "  • Eat regular small meals — avoid long gaps to keep blood sugar stable",
            "  • Fibre helps: include 4–5 servings of vegetables, whole grains daily",
            "  • Protein at every meal (dal, curd, eggs, fish) slows glucose absorption",
            "  • Check blood sugar before and 2h after meals to understand your body's response",
        ],
    ),
    (
        ("protein", "muscle", "strength"),
        [
            "  Protein guidance (general):",
            "  • Typical intake: 0.8–1.2 g per kg body weight for sedentary to active adults",
            "  • Good Indian sources: dal, rajma, chole, paneer, curd (dahi), eggs, fish, chicken",
            "  • Spread protein across 3–4 meals for better absorption",
            "  • Whey or plant protein supplements: not necessary if dal+curd+eggs are in daily diet",
        ],
    ),
    (
        ("iron", "anaemia", "anemia", "hemoglobin"),
        [
            "  Iron and anaemia (general):",
            "  • Iron-rich foods: green leafy vegetables (palak, methi), jaggery, sesame, lentils,",
            "    liver, red meat, fortified cereals",
            "  • Pair iron-rich foods with Vitamin C (lemon juice, amla) to improve absorption",
            "  • Avoid tea/coffee immediately after meals — tannins reduce iron absorption",
            "  • If haemoglobin is low, see a doctor to confirm iron-deficiency (vs other causes)",
        ],
    ),
    (
        ("weight loss", "lose weight", "obesity", "fat"),
        [
            "  Weight management (general principles):",
            "  • Sustainable deficit: reduce portion size, not eliminate food groups",
            "  • Prioritise vegetables, dal, salad — high volume, low calories",
            "  • Limit oil, ghee, fried foods, processed snacks, sugary drinks",
            "  • Physical activity: 30–45 min brisk walking most days is evidence-backed",
            "  • Avoid extreme diets (crash dieting slows metabolism over time)",
        ],
    ),
    (
        ("bp", "blood pressure", "hypertension", "heart"),
        [
            "  Heart-healthy / low-sodium eating (general):",
            "  • Reduce salt: cook without added salt where possible, avoid papad, pickles, chips",
            "  • DASH-pattern: more vegetables, fruits, low-fat dairy, whole grains; less red meat",
            "  • Potassium helps (banana, sweet potato, spinach) — but ask doctor if on BP meds",
            "  • Limit saturated fats: choose olive oil or small amounts of groundnut oil over ghee",
            "  • Alcohol and smoking significantly worsen blood pressure — avoid/minimise",
        ],
    ),
]

_NUTRITION_DEFAULT = [
    "  General balanced diet principles (Indian context):",
    "  • Half plate: vegetables and salad at every main meal",
    "  • Quarter plate: complex carbs (jowar, bajra, brown rice, whole wheat roti)",
    "  • Quarter plate: protein (dal, legumes, curd, eggs, fish, lean meat)",
    "  • Healthy fats: a small amount of cold-pressed oil, nuts, or seeds daily",
    "  • Hydration: 2–3 litres water; limit packaged juices and sweetened drinks",
    "  • Local seasonal produce is cheaper, fresher, and culturally appropriate",
]

_NUTRITION_FOOTER = [
    "",
    "  For a personalised plan (diabetes, kidney disease, heart conditions etc.),",
    "  please consult a registered dietitian or your doctor.",
]


def _handle_nutrition(action: dict) -> tuple[str, dict | None]:
    """Handler: return nutrition guidance for a topic."""
    topic    = action.get("topic", "general nutrition")
    topic_lc = topic.lower()
    tips = next(
        (lines for keywords, lines in _NUTRITION_TIPS if any(k in topic_lc for k in keywords)),
        _NUTRITION_DEFAULT,
    )
    lines = [
        f"General nutrition guidance — {topic}:",
        "  This is general information, not medical advice. Consult a registered dietitian",
        "  or your doctor for personalised nutrition plans, especially with any health conditions.",
        "",
        *tips,
        *_NUTRITION_FOOTER,
    ]
    return "\n".join(lines), None


def _handle_chat(action: dict) -> tuple[str, dict | None]:
    """Handler: return the LLM chat reply verbatim."""
    return action.get("reply", ""), None


# ── Dispatch table ────────────────────────────────────────────────────────────

_HANDLERS: dict[str, Any] = {
    "log_bp":     _handle_log_bp,
    "log_steps":  _handle_log_steps,
    "log_weight": _handle_log_weight,
    "log_sleep":  _handle_log_sleep,
    "log_sugar":  _handle_log_sugar,
    "history":    _handle_history,
    "summary":    _handle_summary,
    "trend":      _handle_trend,
    "set_goal":   _handle_set_goal,
    "nutrition":  _handle_nutrition,
    "chat":       _handle_chat,
}


def _execute(action: dict) -> tuple[str, dict | None]:
    """Dispatch a validated health action to its handler and return (text, data)."""
    handler = _HANDLERS.get(action.get("action", ""))
    if handler:
        return handler(action)
    return f"Unknown action: {action.get('action')}", None


_FOLLOW_UPS = {
    "log_bp":     "Want to see your BP trend over the past 2 weeks?",
    "log_steps":  "Want to see your steps trend this week?",
    "log_weight": "Want to see your weight trend?",
    "log_sleep":  "Want to see your sleep pattern this week?",
    "log_sugar":  "Want to see your blood sugar history?",
    "summary":    "Want to see trends for any specific metric?",
    "nutrition":  "Want nutrition guidance for another topic (e.g. diabetes diet, iron, protein)?",
}


class HealthModule(BaseModule):
    name = "health"
    description = (
        "Tracks personal health data: blood pressure (BP), daily steps, weight, "
        "sleep hours, blood glucose (sugar). Logs readings, shows history, trends, "
        "and gives safe observations — never diagnoses or prescribes."
    )

    def __init__(self):
        """Initialize the health module (ensures the DB schema exists)."""
        db.init()

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        """Route a health query: LLM → validate_action → dispatch."""
        t0 = time.monotonic()
        try:
            action = _call_llm(query, context)
        except Exception as e:
            log.error("LLM call failed: %s", e, exc_info=True)
            from core.mistake_log import log_service_failure
            log_service_failure("ollama", f"health LLM call failed: {e}")
            return ModuleResponse(
                text="I couldn't process that — please try again.",
                module=self.name,
            )

        v = validate_action("health", action)
        action_name = v.action.get("action", "unknown")

        if not v.valid:
            log.warning("action blocked action=%s errors=%s", action_name, v.errors)
            return ModuleResponse(
                text=f"Action blocked: {'; '.join(v.errors)}",
                module=self.name,
            )
        for w in v.warnings:
            log.warning("validator: %s", w)

        log.info("action=%s latency=%dms", action_name, int((time.monotonic() - t0) * 1000))
        text, data = _execute(v.action)
        follow_up = _FOLLOW_UPS.get(action_name)
        return ModuleResponse(text=text, module=self.name, data=data, follow_up=follow_up)
