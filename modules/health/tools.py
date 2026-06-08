from datetime import date, datetime
from modules.health.db import conn


# ── Logging ───────────────────────────────────────────────────────────────────

def log_reading(
    type_: str,
    value1: float,
    value2: float | None = None,
    unit: str | None = None,
    meal_state: str | None = None,
    notes: str | None = None,
) -> dict:
    now = datetime.now()
    with conn() as c:
        cur = c.execute(
            """INSERT INTO health_readings
               (date, time, type, value1, value2, unit, meal_state, notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (now.date().isoformat(), now.strftime("%H:%M"),
             type_, value1, value2, unit, meal_state, notes),
        )
        return {
            "id": cur.lastrowid,
            "type": type_,
            "value1": value1,
            "value2": value2,
            "date": now.date().isoformat(),
            "time": now.strftime("%H:%M"),
        }


# ── Queries ───────────────────────────────────────────────────────────────────

def get_latest(type_: str) -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM health_readings WHERE type=? ORDER BY date DESC, time DESC LIMIT 1",
            (type_,),
        ).fetchone()
        return dict(row) if row else None


def get_history(type_: str, days: int = 7) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """SELECT * FROM health_readings
               WHERE type=? AND date >= date('now', ?)
               ORDER BY date DESC, time DESC""",
            (type_, f"-{days} days"),
        ).fetchall()
        return [dict(r) for r in rows]


def today_summary() -> dict:
    today = date.today().isoformat()
    with conn() as c:
        rows = c.execute(
            """SELECT type, value1, value2, unit, time, meal_state
               FROM health_readings WHERE date=? ORDER BY time""",
            (today,),
        ).fetchall()
        return {"date": today, "readings": [dict(r) for r in rows]}


def daily_trend(type_: str, days: int = 14) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """SELECT date,
                      ROUND(AVG(value1), 1) AS avg_v1,
                      ROUND(AVG(value2), 1) AS avg_v2,
                      COUNT(*) AS readings
               FROM health_readings
               WHERE type=? AND date >= date('now', ?)
               GROUP BY date ORDER BY date""",
            (type_, f"-{days} days"),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Goals ─────────────────────────────────────────────────────────────────────

def set_goal(type_: str, target: float, unit: str | None = None, notes: str | None = None) -> dict:
    with conn() as c:
        c.execute(
            """INSERT INTO health_goals (type, target, unit, notes, updated_at)
               VALUES (?,?,?,?,datetime('now'))
               ON CONFLICT(type) DO UPDATE SET
                   target=excluded.target,
                   unit=excluded.unit,
                   notes=excluded.notes,
                   updated_at=excluded.updated_at""",
            (type_, target, unit, notes),
        )
        return {"type": type_, "target": target, "unit": unit}


def get_goals() -> dict[str, dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM health_goals").fetchall()
        return {r["type"]: dict(r) for r in rows}


# ── Interpretation helpers ────────────────────────────────────────────────────

def interpret_bp(systolic: float, diastolic: float) -> tuple[str, str]:
    """Return (category, advice) for a BP reading."""
    if systolic >= 180 or diastolic >= 120:
        return "Hypertensive Crisis", "Seek immediate medical attention — call 108 or go to a hospital now."
    if systolic >= 160 or diastolic >= 100:
        return "Stage 2 Hypertension", "Consult a doctor today. Do not ignore this."
    if systolic >= 140 or diastolic >= 90:
        return "Stage 1 Hypertension", "Monitor closely and consult a doctor soon."
    if systolic >= 130 or diastolic >= 80:
        return "Elevated", "Lifestyle changes recommended: reduce salt, walk more, manage stress."
    if systolic >= 120 and diastolic < 80:
        return "Elevated (prehypertension)", "Monitor regularly. Walk daily, reduce processed food."
    if systolic < 90 or diastolic < 60:
        return "Low BP (Hypotension)", "Stay hydrated. Consult a doctor if dizzy or fainting occurs."
    return "Normal", "Good. Keep logging regularly."


def interpret_sugar(mg_dl: float, meal_state: str | None) -> tuple[str, str]:
    """Return (category, advice) for blood glucose."""
    if meal_state == "fasting":
        if mg_dl < 70:
            return "Low (Hypoglycemia)", "Eat something now. Consult a doctor."
        if mg_dl < 100:
            return "Normal fasting", "Good. Maintain diet and activity."
        if mg_dl < 126:
            return "Pre-diabetic range", "Consult a doctor. Diet and exercise changes are critical."
        return "Diabetic range", "Consult a doctor immediately. Do not ignore."
    if meal_state == "post_meal":
        if mg_dl < 140:
            return "Normal post-meal", "Good control."
        if mg_dl < 200:
            return "Elevated post-meal", "Review diet — reduce simple carbs. Consult doctor."
        return "High post-meal", "Consult a doctor."
    # random
    if mg_dl >= 200:
        return "High (possible diabetes)", "Consult a doctor soon."
    return "Normal random", "Continue monitoring."


def interpret_steps(count: int, goal: int = 10000) -> tuple[str, str]:
    pct = count / goal * 100
    if pct >= 100:
        return "Goal achieved!", f"{count:,} steps — excellent."
    if pct >= 75:
        return "Active", f"{count:,} steps — on track."
    if pct >= 50:
        return "Moderate", f"{count:,} steps — try to reach {goal:,}."
    return "Low activity", f"Only {count:,} steps today — aim for {goal:,}."


def interpret_sleep(hours: float) -> tuple[str, str]:
    if hours >= 9.5:
        return "Oversleeping", "More than 9.5 hours may indicate health issues. Check with a doctor."
    if hours >= 7:
        return "Good", f"{hours}h sleep — ideal range."
    if hours >= 6:
        return "Slightly low", f"{hours}h — try to get 7–9 hours."
    return "Poor sleep", f"Only {hours}h — chronic sleep deprivation harms health. Prioritise sleep."
