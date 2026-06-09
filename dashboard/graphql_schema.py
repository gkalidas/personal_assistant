"""
Strawberry GraphQL schema for the dashboard.

Clients request only the fields they need — the mind map uses this to
fetch per-module live summaries on demand (no over-fetching).

Endpoint: POST /graphql
Playground: GET  /graphql  (browser, dev only)
"""

from __future__ import annotations

import strawberry
from typing import Optional

from dashboard.sysmon import get_system_stats
from dashboard.weather_widget import WeatherCache
from dashboard.guardian import get_guardian_status
import modules.farming.db as farming_db
import modules.health.db as health_db
import modules.finance.db as finance_db

# Module-level singletons — reuse the TTL cache across GraphQL requests
_wx = WeatherCache()


# ── Types ──────────────────────────────────────────────────────────────────────

@strawberry.type
class SystemStats:
    cpu_pct:       float
    ram_pct:       float
    swap_pct:      float
    swap_used_gb:  float
    swap_total_gb: float
    disk_pct:      float
    disk_used_gb:  float
    disk_total_gb: float
    ram_used_gb:   float
    ram_total_gb:  float
    cpu_cores:     int
    uptime:        str
    load_avg:      str


@strawberry.type
class WeatherSummary:
    temp_c:        str
    feels_like_c:  str
    description:   str
    humidity:      str
    wind_kmh:      str
    wind_dir:      str
    visibility_km: str
    location:      str
    age_min:       int


@strawberry.type
class GuardianSummary:
    overall:       str
    alert_count:   int
    vuln_count:    int
    threat_vulns:  int
    audit_issues:  int
    audit_critical:int


@strawberry.type
class FarmingSummary:
    plots_count:        int
    crops_count:        int
    spray_logs_count:   int
    observations_count: int


@strawberry.type
class HealthSummary:
    readings_count: int
    goals_count:    int
    last_bp:        Optional[str]
    last_steps:     Optional[int]
    last_weight:    Optional[float]


@strawberry.type
class FinanceSummary:
    transactions_count:    int
    budgets_count:         int
    goals_count:           int
    current_month_income:  float
    current_month_spend:   float


# ── Resolvers ──────────────────────────────────────────────────────────────────

def _resolve_system() -> SystemStats:
    s = get_system_stats()
    return SystemStats(
        cpu_pct       = s["cpu_pct"],
        ram_pct       = s["ram_pct"],
        swap_pct      = s["swap_pct"],
        swap_used_gb  = s["swap_used_gb"],
        swap_total_gb = s["swap_total_gb"],
        disk_pct      = s["disk_pct"],
        disk_used_gb  = s["disk_used_gb"],
        disk_total_gb = s["disk_total_gb"],
        ram_used_gb   = s["ram_used_gb"],
        ram_total_gb  = s["ram_total_gb"],
        cpu_cores     = s["cpu_cores"],
        uptime        = s["uptime"],
        load_avg      = s["load_avg"],
    )


def _resolve_weather() -> WeatherSummary:
    w = _wx.get()
    return WeatherSummary(
        temp_c        = str(w.get("temp_c", "--")),
        feels_like_c  = str(w.get("feels_like_c", "--")),
        description   = w.get("description", ""),
        humidity      = str(w.get("humidity", "--")),
        wind_kmh      = str(w.get("wind_kmh", "--")),
        wind_dir      = w.get("wind_dir", ""),
        visibility_km = str(w.get("visibility_km", "--")),
        location      = w.get("location", ""),
        age_min       = w.get("age_min", 0),
    )


def _resolve_guardian() -> GuardianSummary:
    g = get_guardian_status()
    return GuardianSummary(
        overall        = g["overall"],
        alert_count    = g["alert_count"],
        vuln_count     = g["vuln_count"],
        threat_vulns   = g["threat_vulns"],
        audit_issues   = g["audit_issues"],
        audit_critical = g["audit_critical"],
    )


def _resolve_farming() -> FarmingSummary:
    farming_db.init()
    c = farming_db.conn()
    try:
        plots  = c.execute("SELECT COUNT(*) FROM plots").fetchone()[0]
        crops  = c.execute("SELECT COUNT(*) FROM crops").fetchone()[0]
        sprays = c.execute("SELECT COUNT(*) FROM spray_logs").fetchone()[0]
        obs    = c.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    finally:
        c.close()
    return FarmingSummary(
        plots_count        = plots,
        crops_count        = crops,
        spray_logs_count   = sprays,
        observations_count = obs,
    )


def _resolve_health() -> HealthSummary:
    health_db.init()
    c = health_db.conn()
    try:
        readings = c.execute("SELECT COUNT(*) FROM health_readings").fetchone()[0]
        goals    = c.execute("SELECT COUNT(*) FROM health_goals").fetchone()[0]
        # Latest BP (type='bp', value1=systolic, value2=diastolic)
        bp_row = c.execute(
            "SELECT value1, value2 FROM health_readings WHERE type='bp' "
            "ORDER BY date DESC, time DESC LIMIT 1"
        ).fetchone()
        # Latest steps
        steps_row = c.execute(
            "SELECT value1 FROM health_readings WHERE type='steps' "
            "ORDER BY date DESC, time DESC LIMIT 1"
        ).fetchone()
        # Latest weight
        weight_row = c.execute(
            "SELECT value1 FROM health_readings WHERE type='weight' "
            "ORDER BY date DESC, time DESC LIMIT 1"
        ).fetchone()
    finally:
        c.close()
    return HealthSummary(
        readings_count = readings,
        goals_count    = goals,
        last_bp        = f"{int(bp_row[0])}/{int(bp_row[1])}" if bp_row else None,
        last_steps     = int(steps_row[0]) if steps_row else None,
        last_weight    = float(weight_row[0]) if weight_row else None,
    )


def _resolve_finance() -> FinanceSummary:
    import time
    finance_db.init()
    c = finance_db.conn()
    try:
        tx_count = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        bud      = c.execute("SELECT COUNT(*) FROM budgets").fetchone()[0]
        goals    = c.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
        # Current month totals (ts column is ISO timestamp)
        now = time.localtime()
        month_start = f"{now.tm_year}-{now.tm_mon:02d}-01"
        income = c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions "
            "WHERE type='income' AND ts >= ?", (month_start,)
        ).fetchone()[0]
        spend = c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions "
            "WHERE type='expense' AND ts >= ?", (month_start,)
        ).fetchone()[0]
    finally:
        c.close()
    return FinanceSummary(
        transactions_count   = tx_count,
        budgets_count        = bud,
        goals_count          = goals,
        current_month_income = round(float(income), 2),
        current_month_spend  = round(float(spend), 2),
    )


# ── Schema ─────────────────────────────────────────────────────────────────────

@strawberry.type
class Query:
    system:   SystemStats   = strawberry.field(resolver=_resolve_system)
    weather:  WeatherSummary= strawberry.field(resolver=_resolve_weather)
    guardian: GuardianSummary = strawberry.field(resolver=_resolve_guardian)
    farming:  FarmingSummary  = strawberry.field(resolver=_resolve_farming)
    health:   HealthSummary   = strawberry.field(resolver=_resolve_health)
    finance:  FinanceSummary  = strawberry.field(resolver=_resolve_finance)


schema = strawberry.Schema(query=Query)
