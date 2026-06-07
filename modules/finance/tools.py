from datetime import datetime, date
from typing import Any
from modules.finance.db import conn


# ── Transactions ──────────────────────────────────────────────────────────────

def add_transaction(
    amount: float,
    type_: str,
    category: str,
    description: str = "",
    ts: str | None = None,
) -> dict:
    ts = ts or datetime.now().isoformat()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO transactions (ts, amount, type, category, description) VALUES (?,?,?,?,?)",
            (ts, amount, type_, category, description),
        )
        return {"id": cur.lastrowid, "amount": amount, "type": type_, "category": category}


def get_transactions(
    month: str | None = None,
    category: str | None = None,
    type_: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """month format: YYYY-MM"""
    clauses, params = [], []
    if month:
        clauses.append("strftime('%Y-%m', ts) = ?")
        params.append(month)
    if category:
        clauses.append("lower(category) = lower(?)")
        params.append(category)
    if type_:
        clauses.append("type = ?")
        params.append(type_)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with conn() as c:
        rows = c.execute(
            f"SELECT * FROM transactions {where} ORDER BY ts DESC LIMIT ?", params
        ).fetchall()
    return [dict(r) for r in rows]


# ── Summary ───────────────────────────────────────────────────────────────────

def monthly_summary(month: str | None = None) -> dict[str, Any]:
    """Returns income, expenses, net, and per-category breakdown for a month."""
    month = month or date.today().strftime("%Y-%m")
    with conn() as c:
        totals = c.execute(
            """
            SELECT type, SUM(amount) as total
            FROM transactions
            WHERE strftime('%Y-%m', ts) = ?
            GROUP BY type
            """,
            (month,),
        ).fetchall()

        by_cat = c.execute(
            """
            SELECT category, type, SUM(amount) as total
            FROM transactions
            WHERE strftime('%Y-%m', ts) = ?
            GROUP BY category, type
            ORDER BY total DESC
            """,
            (month,),
        ).fetchall()

    income = next((r["total"] for r in totals if r["type"] == "income"), 0.0)
    expenses = next((r["total"] for r in totals if r["type"] == "expense"), 0.0)

    breakdown: dict[str, dict] = {}
    for row in by_cat:
        cat = row["category"]
        if cat not in breakdown:
            breakdown[cat] = {}
        breakdown[cat][row["type"]] = row["total"]

    return {
        "month": month,
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "breakdown": breakdown,
    }


# ── Budgets ───────────────────────────────────────────────────────────────────

def set_budget(category: str, monthly_cap: float) -> dict:
    now = datetime.now().isoformat()
    with conn() as c:
        c.execute(
            """
            INSERT INTO budgets (category, monthly_cap, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(category) DO UPDATE SET monthly_cap=excluded.monthly_cap, updated_at=excluded.updated_at
            """,
            (category, monthly_cap, now),
        )
    return {"category": category, "monthly_cap": monthly_cap}


def budget_status(month: str | None = None) -> list[dict]:
    """Compare each budget category against actual spending this month."""
    month = month or date.today().strftime("%Y-%m")
    with conn() as c:
        budgets = c.execute("SELECT * FROM budgets").fetchall()
        results = []
        for b in budgets:
            row = c.execute(
                """
                SELECT COALESCE(SUM(amount), 0) as spent
                FROM transactions
                WHERE lower(category) = lower(?) AND type = 'expense'
                  AND strftime('%Y-%m', ts) = ?
                """,
                (b["category"], month),
            ).fetchone()
            spent = row["spent"]
            cap = b["monthly_cap"]
            results.append({
                "category": b["category"],
                "cap": cap,
                "spent": spent,
                "remaining": cap - spent,
                "over_budget": spent > cap,
            })
    return results


# ── Goals ─────────────────────────────────────────────────────────────────────

def add_goal(name: str, target: float, deadline: str | None = None) -> dict:
    now = datetime.now().isoformat()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO goals (name, target, deadline, created_at) VALUES (?,?,?,?)",
            (name, target, deadline, now),
        )
        return {"id": cur.lastrowid, "name": name, "target": target}


def update_goal_savings(goal_id: int, amount: float) -> dict:
    with conn() as c:
        c.execute(
            "UPDATE goals SET saved = saved + ? WHERE id = ?", (amount, goal_id)
        )
        row = c.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    return dict(row)


def list_goals() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM goals ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]
