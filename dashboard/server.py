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
import tempfile
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.responses import Response
from strawberry.fastapi import GraphQLRouter

from dashboard.network import NetworkMonitor
from dashboard.news_widget import NewsCache
from dashboard.sysmon import get_system_stats
from dashboard.weather_widget import WeatherCache
from dashboard.guardian import get_guardian_status
from dashboard.graphql_schema import schema

log = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"

_net  = NetworkMonitor()
_wx   = WeatherCache()
_news = NewsCache()

# ── Lazy module pipeline (only loaded on first voice query) ────────────────────
_query_modules     = None
_query_modules_lck = threading.Lock()

def _get_query_modules() -> dict:
    global _query_modules
    if _query_modules is None:
        with _query_modules_lck:
            if _query_modules is None:
                from modules.finance.module import FinanceModule
                from modules.farming.module import FarmingModule
                from modules.health.module import HealthModule
                from modules.system.module import SystemModule
                from modules.diary.module import DiaryModule
                from modules.search.module import SearchModule
                _query_modules = {
                    "finance": FinanceModule(),
                    "farming": FarmingModule(),
                    "health":  HealthModule(),
                    "system":  SystemModule(),
                    "diary":   DiaryModule(),
                    "search":  SearchModule(),
                }
    return _query_modules

# ── API request tracking ───────────────────────────────────────────────────────
# Keyed by "METHOD /path". WebSocket upgrades (101) are excluded.
_api_stats: dict[str, dict] = defaultdict(lambda: {
    "calls": 0, "total_ms": 0.0, "max_ms": 0.0,
    "errors": 0, "recent": deque(maxlen=20),
})


@asynccontextmanager
async def _lifespan(app: FastAPI):
    asyncio.get_event_loop().run_in_executor(None, _wx.get)
    yield


app = FastAPI(title="GK Dashboard", docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.middleware("http")
async def _request_logger(request: Request, call_next):
    t0 = time.monotonic()
    response = await call_next(request)
    # Skip WebSocket upgrade (101) — duration would be entire connection lifetime
    if response.status_code == 101:
        return response
    elapsed = (time.monotonic() - t0) * 1000
    key = f"{request.method} {request.url.path}"
    s = _api_stats[key]
    s["calls"] += 1
    s["total_ms"] += elapsed
    if elapsed > s["max_ms"]:
        s["max_ms"] = elapsed
    if response.status_code >= 400:
        s["errors"] += 1
    s["recent"].append({
        "ts": datetime.now().strftime("%H:%M:%S"),
        "ms": round(elapsed, 1),
        "status": response.status_code,
    })
    log.info("API %-6s %-30s → %d  %.1f ms", request.method, request.url.path,
             response.status_code, elapsed)
    return response


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
                     "weekly summary", "what did I do this week",
                     "show this week's diary", "approve diary"],
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


@app.get("/todo")
async def todo_page():
    return HTMLResponse((_STATIC / "todo.html").read_text(encoding="utf-8"))


@app.get("/guide")
async def guide_page():
    return HTMLResponse((_STATIC / "guide.html").read_text(encoding="utf-8"))


@app.get("/api/data")
async def api_data():
    return JSONResponse(_build_payload())


@app.get("/api/mindmap")
async def api_mindmap():
    return JSONResponse({"branches": _MINDMAP_BRANCHES})


@app.post("/api/guardian/scan")
async def guardian_scan():
    """Trigger an on-demand anomaly scan in background."""
    def _run():
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from security.guardian import task_anomaly_detect
            task_anomaly_detect()
        except Exception as e:
            log.error("on-demand scan failed: %s", e)
    import threading
    threading.Thread(target=_run, daemon=True, name="on-demand-scan").start()
    return JSONResponse({"status": "scanning"})


@app.post("/api/guardian/patch")
async def guardian_patch():
    """Trigger on-demand CVE scan with auto-patching (HIGH+ severity, same major version)."""
    def _run():
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from security.guardian import task_vuln_scan
            result = task_vuln_scan(auto_patch=True)
            p = result.get("patch_result", {}).get("summary", {}).get("patched", 0)
            log.info("on-demand patch: %d package(s) patched", p)
        except Exception as e:
            log.error("on-demand patch failed: %s", e)
    import threading
    threading.Thread(target=_run, daemon=True, name="on-demand-patch").start()
    return JSONResponse({"status": "patching"})


@app.get("/api/stats")
async def api_stats():
    """API call analytics — hit counts, latency, caching recommendations."""
    result = {}
    for key, s in sorted(_api_stats.items(), key=lambda x: -x[1]["calls"]):
        calls = s["calls"]
        avg_ms = s["total_ms"] / calls if calls else 0.0
        path = key.split(" ", 1)[1] if " " in key else key

        # Caching recommendation heuristics
        if path in ("/", "/mindmap"):
            rec = "cache with ETag — static HTML, never changes at runtime"
        elif path == "/api/data":
            rec = "use WebSocket /ws instead of polling; data is 2 s stale by design"
        elif path.startswith("/api/diary"):
            rec = "cache 60 s TTL — diary drafts change only on write"
        elif path == "/graphql":
            rec = "cacheable per query hash; selective fields keep payload small"
        elif path in ("/api/guardian/scan", "/api/weather/refresh"):
            rec = "write/trigger endpoint — do not cache"
        elif calls > 20 and avg_ms > 80:
            rec = "cache recommended — high traffic AND slow (> 80 ms avg)"
        elif avg_ms > 200:
            rec = "cache recommended — slow endpoint (> 200 ms avg)"
        else:
            rec = "ok"

        result[key] = {
            "calls": calls,
            "avg_ms": round(avg_ms, 1),
            "max_ms": round(s["max_ms"], 1),
            "errors": s["errors"],
            "recent": list(s["recent"])[-5:],
            "recommendation": rec,
        }
    return JSONResponse({"endpoints": result, "snapshot_at": datetime.now().isoformat()})


@app.get("/api/diary/drafts")
async def diary_drafts_list():
    """List all diary draft metadata (no full text — avoids large payloads)."""
    try:
        from core.memory import list_diary_drafts
        drafts = list_diary_drafts()
        return JSONResponse([{
            "week":        d["week"],
            "approved":    bool(d.get("approved")),
            "created_at":  (d.get("created_at") or "")[:10],
            "approved_at": (d.get("approved_at") or "")[:10],
            "chars":       len(d.get("draft") or ""),
        } for d in drafts])
    except Exception as e:
        log.error("diary drafts list error: %s", e)
        return JSONResponse([], status_code=200)


@app.get("/api/diary/{week}")
async def diary_draft_get(week: str):
    """Return one diary draft (full text) by ISO week key e.g. 2025-W31."""
    try:
        from core.memory import get_diary_draft
        draft = get_diary_draft(week)
        if not draft:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(draft)
    except Exception as e:
        log.error("diary draft get error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/weather/refresh")
async def weather_refresh():
    """Force a fresh weather fetch by clearing the cache TTL."""
    _wx._fetched_at = 0.0
    data = await asyncio.get_event_loop().run_in_executor(None, _wx.get)
    return JSONResponse(data)


@app.get("/api/mistakes")
async def api_mistakes(limit: int = 50, error_type: str | None = None):
    """Return Jarvis mistake log for analysis."""
    from core.mistake_log import get_mistakes, count_by_type
    return JSONResponse({
        "mistakes": get_mistakes(limit=limit, error_type=error_type or None),
        "summary":  count_by_type(),
        "hint": "Run `python -m core.mistake_log` for suggested fixes",
    })


@app.get("/api/news")
async def api_news():
    """Return cached agriculture/India news (15-min TTL)."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _news.get)
    return JSONResponse(data)


@app.post("/api/news/refresh")
async def api_news_refresh():
    """Force a fresh news fetch."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _news.refresh)
    return JSONResponse(data)


@app.post("/api/voice/query")
async def api_voice_query(audio: UploadFile = File(...)):
    """
    Accept webm/opus audio from the browser, transcribe with faster-whisper,
    route through the module pipeline, and return the response.
    """
    def _process(tmp_path: str) -> dict:
        # Transcribe
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segs, _ = model.transcribe(tmp_path, beam_size=5)
            transcript = " ".join(s.text.strip() for s in segs).strip()
        except Exception as e:
            return {"error": f"transcription failed: {e}"}

        if not transcript:
            return {"error": "no speech detected"}

        # Route + dispatch
        try:
            from core.router import route, dispatch
            mods = _get_query_modules()
            ctx  = {}
            chosen = route(transcript, mods)
            responses = []
            for name in chosen:
                if name in mods:
                    r = mods[name].handle(transcript, ctx)
                    responses.append(r.text if hasattr(r, "text") else str(r))
            response_text = "\n\n".join(responses) if responses else "No response"
            return {
                "transcript": transcript,
                "module":     chosen[0] if chosen else "unknown",
                "response":   response_text,
            }
        except Exception as e:
            return {"transcript": transcript, "error": f"dispatch failed: {e}"}

    suffix = ".webm"
    loop = asyncio.get_event_loop()
    try:
        content = await audio.read()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(content)
            tmp_path = f.name
        result = await loop.run_in_executor(None, _process, tmp_path)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass
    return JSONResponse(result)


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
