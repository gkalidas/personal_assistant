"""
Assembles the weekly digest: diary + finance + health.
Returns both a plain-text and HTML version of the email body.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)

# ── Data collectors ───────────────────────────────────────────────────────────

def _diary_section() -> tuple[str, str]:
    """Return (text, html) for the most recent approved diary entry."""
    try:
        from core.memory import list_diary_drafts, get_diary_draft
        approved = list_diary_drafts(approved=True)
        if not approved:
            return "No approved diary entries yet.\n", "<p><em>No approved diary entries yet.</em></p>"
        latest = approved[0]
        week   = latest["week"]
        draft  = get_diary_draft(week)
        body   = (draft.get("draft") or "").strip() if draft else ""
        if not body:
            return "Diary entry empty.\n", "<p><em>Diary entry is empty.</em></p>"
        text = f"Week of {week}:\n{body}\n"
        html = f"<h3>Week of {week}</h3><p>{body.replace(chr(10), '<br>')}</p>"
        return text, html
    except Exception as e:
        log.warning("diary section failed: %s", e)
        return "Diary unavailable.\n", "<p><em>Diary unavailable.</em></p>"


def _finance_section() -> tuple[str, str]:
    """Return (text, html) for this month's finance summary + budget status."""
    try:
        from modules.finance.tools import monthly_summary, budget_status
        summary = monthly_summary()
        budgets = budget_status()

        month     = summary.get("month", str(date.today())[:7])
        income    = summary.get("total_income", 0.0)
        expenses  = summary.get("total_expenses", 0.0)
        net       = summary.get("net", income - expenses)
        by_cat    = summary.get("by_category", {})

        lines = [f"Month: {month}", f"Income: ₹{income:,.0f}", f"Expenses: ₹{expenses:,.0f}",
                 f"Net: ₹{net:,.0f}"]
        rows_html = (f"<tr><td>Income</td><td>₹{income:,.0f}</td></tr>"
                     f"<tr><td>Expenses</td><td>₹{expenses:,.0f}</td></tr>"
                     f"<tr><td><strong>Net</strong></td><td><strong>₹{net:,.0f}</strong></td></tr>")

        if by_cat:
            lines.append("By category:")
            rows_html += "<tr><td colspan=2><em>By category</em></td></tr>"
            for cat, amt in sorted(by_cat.items(), key=lambda x: -abs(x[1])):
                lines.append(f"  {cat}: ₹{amt:,.0f}")
                rows_html += f"<tr><td>&nbsp;&nbsp;{cat}</td><td>₹{amt:,.0f}</td></tr>"

        over_budget = [b for b in budgets if b.get("status") == "over"]
        if over_budget:
            lines.append("Over budget:")
            rows_html += "<tr><td colspan=2><strong>Over budget</strong></td></tr>"
            for b in over_budget:
                lines.append(f"  {b['category']}: spent ₹{b['spent']:,.0f} / cap ₹{b['cap']:,.0f}")
                rows_html += (f"<tr style='color:#c0392b'><td>&nbsp;&nbsp;{b['category']}</td>"
                              f"<td>₹{b['spent']:,.0f} / ₹{b['cap']:,.0f}</td></tr>")

        text = "\n".join(lines) + "\n"
        html = (f"<table border=0 cellpadding=4 style='border-collapse:collapse'>"
                f"{rows_html}</table>")
        return text, html
    except Exception as e:
        log.warning("finance section failed: %s", e)
        return "Finance unavailable.\n", "<p><em>Finance unavailable.</em></p>"


_HEALTH_METRICS = [
    ("bp",     "Blood Pressure", "mmHg"),
    ("steps",  "Steps",          "steps/day"),
    ("weight", "Weight",         "kg"),
    ("sleep",  "Sleep",          "h/night"),
    ("sugar",  "Blood Sugar",    "mg/dL"),
]


def _format_metric_value(ht, mtype: str, latest: dict, unit: str) -> str:
    """Format one metric's latest 7-day average, with an interpretation tag where useful."""
    v1 = latest.get("avg_v1", 0)
    v2 = latest.get("avg_v2")
    if mtype == "bp" and v2:
        return f"{int(v1)}/{int(v2)} {unit} [{ht.interpret_bp(v1, v2)[0]}]"
    if mtype == "steps":
        return f"{int(v1):,} {unit} [{ht.interpret_steps(int(v1))[0]}]"
    if mtype == "sleep":
        return f"{v1:.1f} {unit} [{ht.interpret_sleep(v1)[0]}]"
    return f"{v1} {unit}"


def _health_section() -> tuple[str, str]:
    """Return (text, html) for the last 7 days of health metrics."""
    try:
        from modules.health import tools as ht
        lines: list[str] = []
        rows_html = ""
        for mtype, label, unit in _HEALTH_METRICS:
            trend = ht.daily_trend(mtype, days=7)
            if not trend:
                continue
            val = _format_metric_value(ht, mtype, trend[-1], unit)
            lines.append(f"  {label}: {val}")
            rows_html += f"<tr><td>{label}</td><td>{val}</td></tr>"

        if not lines:
            return "No health data recorded this week.\n", "<p><em>No health data this week.</em></p>"

        text = "7-day averages:\n" + "\n".join(lines) + "\n"
        html = ("<p><strong>7-day averages</strong></p>"
                "<table border=0 cellpadding=4 style='border-collapse:collapse'>"
                f"{rows_html}</table>")
        return text, html
    except Exception as e:
        log.warning("health section failed: %s", e)
        return "Health unavailable.\n", "<p><em>Health unavailable.</em></p>"


# ── Assembly ──────────────────────────────────────────────────────────────────

_STYLE = """
body{font-family:Arial,sans-serif;font-size:14px;color:#222;background:#f9f9f9;margin:0;padding:0}
.wrap{max-width:640px;margin:24px auto;background:#fff;border-radius:8px;
      box-shadow:0 2px 8px rgba(0,0,0,.1);overflow:hidden}
.header{background:#1a3a5c;color:#fff;padding:20px 28px}
.header h1{margin:0;font-size:20px;letter-spacing:.5px}
.header p{margin:4px 0 0;opacity:.75;font-size:12px}
.section{padding:20px 28px;border-bottom:1px solid #eee}
.section h2{margin:0 0 12px;font-size:15px;color:#1a3a5c;text-transform:uppercase;
            letter-spacing:.8px;border-left:3px solid #1a3a5c;padding-left:8px}
table{width:100%;font-size:13px}
td{padding:4px 6px;vertical-align:top}
td:last-child{text-align:right}
.footer{padding:14px 28px;font-size:11px;color:#888;text-align:center}
"""


def build_digest() -> tuple[str, str, str]:
    """
    Returns (subject, text_body, html_body).
    Calls all three section builders and wraps them in a styled email.
    """
    today   = date.today()
    week_of = (today - timedelta(days=today.weekday())).isoformat()
    subject = f"GK Weekly Digest — week of {week_of}"

    diary_t,   diary_h   = _diary_section()
    finance_t, finance_h = _finance_section()
    health_t,  health_h  = _health_section()

    text_body = "\n".join([
        f"GK Weekly Digest — week of {week_of}",
        "=" * 48,
        "",
        "── DIARY ──────────────────────────────────────",
        diary_t,
        "── FINANCE ─────────────────────────────────────",
        finance_t,
        "── HEALTH ──────────────────────────────────────",
        health_t,
        "── ─────────────────────────────────────────────",
        "Generated by GK Personal Assistant",
    ])

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_STYLE}</style></head>
<body><div class="wrap">
  <div class="header">
    <h1>GK Weekly Digest</h1>
    <p>Week of {week_of} &nbsp;·&nbsp; auto-generated</p>
  </div>
  <div class="section"><h2>Diary</h2>{diary_h}</div>
  <div class="section"><h2>Finance</h2>{finance_h}</div>
  <div class="section"><h2>Health</h2>{health_h}</div>
  <div class="footer">Generated by GK Personal Assistant · {today.isoformat()}</div>
</div></body></html>"""

    return subject, text_body, html_body
