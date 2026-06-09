"""
Dashboard WebSocket server.

GET  /           → serve index.html
GET  /mindmap    → interactive mind map page
WS   /ws         → JSON stream every 2 s
GET  /api/data   → single JSON snapshot (debugging)
GET  /api/mindmap→ mind map branch data (JSON)
POST /graphql    → GraphQL API (selective field queries)
GET  /graphql    → GraphQL playground (browser)
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from strawberry.fastapi import GraphQLRouter

from dashboard.network import NetworkMonitor
from dashboard.sysmon import get_system_stats
from dashboard.weather_widget import WeatherCache
from dashboard.guardian import get_guardian_status
from dashboard.graphql_schema import schema

log = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"

_net = NetworkMonitor()
_wx  = WeatherCache()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    asyncio.get_event_loop().run_in_executor(None, _wx.get)
    yield


app = FastAPI(title="GK Dashboard", docs_url=None, redoc_url=None, lifespan=_lifespan)

# Mount GraphQL at /graphql (GET = playground, POST = query)
graphql_app = GraphQLRouter(schema, graphql_ide="graphiql")
app.include_router(graphql_app, prefix="/graphql")


# ── Mind-map branch data ───────────────────────────────────────────────────────

_MINDMAP_BRANCHES = [
    {
        "id": "farming", "label": "Farming Module", "color": "#3fb950",
        "graphql": "{ farming { plotsCount cropsCount sprayLogsCount observationsCount } }",
        "examples": ["show my farm weather", "mandi price pomegranate today",
                     "is it safe to spray tomorrow?", "add spray log"],
        "children": [
            "Current weather & 7-day forecast",
            "Spray safety check (wind/rain/humidity)",
            "Rainfall history (ERA5 archive)",
            "Plot & crop management",
            "Mandi prices (APMC · data.gov.in)",
            "Disease KB + fuzzy symptom matching",
            "Photo diagnosis (vision server)",
            "Soil data (pH · N via SoilGrids)",
        ],
    },
    {
        "id": "finance", "label": "Finance Module", "color": "#f78166",
        "graphql": "{ finance { transactionsCount budgetsCount goalsCount currentMonthIncome currentMonthSpend } }",
        "examples": ["monthly summary", "log expense 500 groceries",
                     "show budget status", "add income 20000"],
        "children": [
            "Log income & expenses",
            "Monthly income vs spend summary",
            "Budget tracker (cap per category)",
            "Spending breakdown by category",
            "Financial goals (target + deadline)",
        ],
    },
    {
        "id": "health", "label": "Health Module", "color": "#ff7b72",
        "graphql": "{ health { readingsCount goalsCount lastBp lastSteps lastWeight } }",
        "examples": ["log BP 120/80", "show health trends",
                     "steps today 8000", "log weight 72"],
        "children": [
            "Blood pressure (6 risk levels)",
            "Step count vs daily goal",
            "Weight tracking",
            "Sleep hours & quality",
            "Blood sugar (fasting/post-meal)",
            "7–14 day trends for any metric",
        ],
    },
    {
        "id": "diary", "label": "Diary Module", "color": "#d2a8ff",
        "graphql": None,
        "examples": ["write diary from today's photos",
                     "show this week's diary", "diary summary this month"],
        "children": [
            "Photo EXIF extraction (GPS · device)",
            "Vision captions via moondream (local)",
            "LLM diary entry writer (qwen3:1.7b)",
            "Weekly auto-draft from query history",
            "Draft → review → approve workflow",
            "Stored in SQLite (queryable)",
        ],
    },
    {
        "id": "dashboard", "label": "Dashboard", "color": "#79c0ff",
        "graphql": "{ system { cpuPct ramPct swapPct diskPct uptime } weather { tempC description humidity } }",
        "examples": ["open dashboard", "system status",
                     "http://localhost:8765"],
        "children": [
            "Live CPU · RAM · Swap · Disk",
            "Network interfaces (LAN · WiFi · Tailscale)",
            "Internet speed (download / upload)",
            "Multi-connection failover alerting",
            "Live weather widget (Barloni)",
            "Security Guardian panel",
            "WebSocket push every 2s",
            "Phone access via Tailscale",
        ],
    },
    {
        "id": "security", "label": "Security Guardian", "color": "#ffa657",
        "graphql": "{ guardian { overall alertCount vulnCount threatVulns auditIssues auditCritical } }",
        "examples": ["guardian status", "show security schedule",
                     "show code audit issues"],
        "children": [
            "CVE scan — OSV.dev (daily)",
            "Code audit — bandit (weekly)",
            "Threat intel — NVD · CISA KEV · Arxiv",
            "Log anomaly detection (hourly)",
            "Auto-patch safe upgrades",
            "Idle-aware scheduler",
            "Auto-starts on boot (systemd)",
        ],
    },
    {
        "id": "core", "label": "Core Layer", "color": "#bc8cff",
        "graphql": "{ system { cpuPct uptime loadAvg } }",
        "examples": ["show system load", "show load pattern",
                     "guardian schedule"],
        "children": [
            "Router — qwen2.5:0.5b (intent → module)",
            "Sanitizer — 45 injection patterns",
            "Memory — SQLite events log",
            "Action schema validation",
            "Follow-up suggestion engine",
            "Config — single source of truth",
        ],
    },
    {
        "id": "apis", "label": "External APIs", "color": "#56d364",
        "graphql": "{ weather { tempC description location ageMin } }",
        "examples": ["show current weather", "soil data barloni",
                     "mandi price onion"],
        "children": [
            "Open-Meteo (weather · forecast · ERA5)",
            "Open-Meteo Geocoding (lat/lon)",
            "SoilGrids / ISRIC (soil data)",
            "data.gov.in APMC (mandi prices)",
            "OSV.dev (CVE databases)",
            "NVD / NIST (CVE + LLM attacks)",
            "CISA KEV (exploited CVEs)",
            "Arxiv cs.CR RSS (AI security)",
        ],
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _build_payload() -> dict[str, Any]:
    net = _net.status
    sys = get_system_stats()
    wx  = _wx.get()

    return {
        "guardian": get_guardian_status(),
        "network": {
            "interfaces": [
                {
                    "name":         i.name,
                    "type":         i.type,
                    "is_up":        i.is_up,
                    "ip":           i.ip,
                    "link_mbps":    i.link_mbps,
                    "is_connected": i.is_connected,
                }
                for i in net.interfaces
            ],
            "primary":        net.primary,
            "download_mbps":  round(net.download_mbps, 2),
            "upload_mbps":    round(net.upload_mbps, 2),
            "failover_alert": net.failover_alert,
        },
        "system":  sys,
        "weather": wx,
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/mindmap")
async def mindmap():
    return HTMLResponse((_STATIC / "mindmap.html").read_text(encoding="utf-8"))


@app.get("/api/data")
async def api_data():
    return JSONResponse(_build_payload())


@app.get("/api/mindmap")
async def api_mindmap():
    return JSONResponse({"branches": _MINDMAP_BRANCHES})


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    client = websocket.client
    log.info("dashboard connected: %s", client)
    try:
        while True:
            payload = _build_payload()
            await websocket.send_json(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        log.info("dashboard disconnected: %s", client)
    except Exception as e:
        log.error("ws error %s: %s", client, e)
