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

from fastapi import Body
from dashboard.network import NetworkMonitor
from dashboard.news_widget import NewsCache
from dashboard.sysmon import get_system_stats
from dashboard.weather_widget import WeatherCache
from dashboard.guardian import get_guardian_status
from dashboard.graphql_schema import schema
from dashboard.todo_store import ensure_table as _ensure_todos, list_todos, add_todo, update_todo, delete_todo
from core.knowledge_graph import init_graph_tables as _ensure_graph
from core.db_encryption import backup_sensitive_dbs as _backup_dbs, key_info as _enc_key_info

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
                from modules.code.module import CodeModule
                _query_modules = {
                    "finance": FinanceModule(),
                    "farming": FarmingModule(),
                    "health":  HealthModule(),
                    "system":  SystemModule(),
                    "diary":   DiaryModule(),
                    "search":  SearchModule(),
                    "code":    CodeModule(),
                }
    return _query_modules

# ── API request tracking ───────────────────────────────────────────────────────
# Keyed by "METHOD /path". WebSocket upgrades (101) are excluded.
_api_stats: dict[str, dict] = defaultdict(lambda: {
    "calls": 0, "total_ms": 0.0, "max_ms": 0.0,
    "errors": 0, "recent": deque(maxlen=20),
})


# ── Whisper model singleton ────────────────────────────────────────────────────
# Loaded once in background at startup; avoids 2-3s delay on first voice query
_whisper_model = None
_whisper_lock  = threading.Lock()

def _load_whisper():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            try:
                from faster_whisper import WhisperModel
                log.info("loading faster-whisper 'tiny' model…")
                _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
                log.info("faster-whisper ready")
            except Exception as e:
                log.error("faster-whisper load failed: %s", e)
    return _whisper_model


def _bg_check_new_photos():
    """At startup: scan ~/Uploads and ~/Pictures for unprocessed photos; auto-write diary."""
    try:
        from core.memory import get_processed_photo_paths
        from pathlib import Path
        photo_exts = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".heif"}
        already = get_processed_photo_paths()
        for folder in ("Uploads", "Pictures"):
            d = Path.home() / folder
            if not d.exists():
                continue
            new = [str(p) for p in d.rglob("*")
                   if p.suffix.lower() in photo_exts and str(p) not in already]
            if new:
                log.info("startup: %d new photo(s) in ~/%s — writing diary", len(new), folder)
                mods = _get_query_modules()
                diary = mods.get("diary")
                if diary:
                    diary.handle(f"write diary from my photos {d}", {})
    except Exception as e:
        log.error("bg photo check failed: %s", e)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Initialise persistent stores
    _ensure_todos()
    _ensure_graph()
    # Warm up weather + whisper in background
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _wx.get)
    loop.run_in_executor(None, _load_whisper)
    # Auto-process new photos in ~/Pictures (non-blocking)
    threading.Thread(target=_bg_check_new_photos, daemon=True, name="photo-check").start()
    # Encrypted backup of sensitive DBs at startup (non-blocking)
    threading.Thread(target=_backup_dbs, daemon=True, name="db-backup").start()
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


@app.get("/api/security/db-key")
async def api_db_key_info():
    """Return encryption key metadata (never the key itself)."""
    return JSONResponse(_enc_key_info())


@app.post("/api/security/backup-dbs")
async def api_backup_dbs():
    """Encrypt sensitive DBs to .enc snapshot files (non-blocking)."""
    loop = asyncio.get_event_loop()
    backed = await loop.run_in_executor(None, _backup_dbs)
    return JSONResponse({"status": "ok", "files": backed})


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


@app.get("/api/diary/questions")
async def diary_questions_list(week: str | None = None):
    """Return pending (unanswered) diary review questions."""
    try:
        from core.memory import get_pending_questions
        return JSONResponse(get_pending_questions(week=week))
    except Exception as e:
        log.error("diary questions error: %s", e)
        return JSONResponse([])


@app.post("/api/diary/questions/{qid}/answer")
async def diary_question_answer(qid: int, payload: dict = Body(...)):
    """Record user's answer to a diary review question."""
    try:
        from core.memory import answer_diary_question
        answer = (payload.get("answer") or "").strip()
        if not answer:
            return JSONResponse({"error": "answer required"}, status_code=400)
        answer_diary_question(qid, answer)
        return JSONResponse({"ok": True})
    except Exception as e:
        log.error("diary question answer error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/diary/photo-stats")
async def diary_photo_stats():
    """Return processed-photos summary: total count, by-week breakdown, last run."""
    try:
        from core.memory import get_photo_stats
        return JSONResponse(get_photo_stats())
    except Exception as e:
        log.error("photo stats error: %s", e)
        return JSONResponse({"total_processed": 0, "by_week": [], "last_processed_at": None})


@app.post("/api/diary/write-photos")
async def diary_write_photos():
    """Trigger diary write from ~/Pictures in background; return immediately."""
    def _run():
        try:
            mods = _get_query_modules()
            diary = mods.get("diary")
            if diary:
                diary.handle("write diary from my photos", {})
        except Exception as e:
            log.error("diary write-photos error: %s", e)
    threading.Thread(target=_run, daemon=True, name="diary-write-photos").start()
    return JSONResponse({"status": "started", "message": "Writing diary from ~/Pictures — check voice panel for results"})


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


@app.get("/api/graph")
async def api_graph():
    """Return knowledge graph nodes + edges for vis-network."""
    from core.knowledge_graph import get_graph_json
    return JSONResponse(get_graph_json())


@app.get("/api/graph/stats")
async def api_graph_stats():
    """Return knowledge graph summary stats."""
    from core.knowledge_graph import graph_stats
    return JSONResponse(graph_stats())


@app.post("/api/graph/node")
async def api_graph_add_node(payload: dict = Body(...)):
    """Add or update a knowledge graph node."""
    from core.knowledge_graph import upsert_node
    type_ = payload.get("type", "person")
    label = (payload.get("label") or "").strip()
    if not label:
        return JSONResponse({"error": "label required"}, status_code=400)
    nid = upsert_node(type_, label, payload.get("properties"), payload.get("aliases"))
    return JSONResponse({"id": nid})


@app.post("/api/graph/edge")
async def api_graph_add_edge(payload: dict = Body(...)):
    """Add an edge between two nodes."""
    from core.knowledge_graph import add_edge
    src = payload.get("src_id")
    dst = payload.get("dst_id")
    rel = (payload.get("rel") or "related_to").strip()
    if not src or not dst:
        return JSONResponse({"error": "src_id and dst_id required"}, status_code=400)
    eid = add_edge(int(src), int(dst), rel, payload.get("weight", 1.0))
    return JSONResponse({"id": eid})


@app.get("/api/farming/ndvi")
async def api_ndvi(lat: float = 18.1617, lon: float = 75.4218, weeks: int = 6):
    """NDVI crop health from NASA MODIS for given coordinates (defaults to Barloni)."""
    loop = asyncio.get_event_loop()
    def _fetch():
        from modules.farming.ndvi import get_ndvi
        return get_ndvi(lat=lat, lon=lon, weeks_back=weeks)
    data = await loop.run_in_executor(None, _fetch)
    return JSONResponse(data)


@app.get("/api/farming/soil-trend")
async def api_soil_trend(lat: float = 18.1617, lon: float = 75.4218, days: int = 14):
    """Soil moisture + temperature trend from Open-Meteo ERA5 for given coordinates."""
    loop = asyncio.get_event_loop()
    def _fetch():
        try:
            import httpx
            from datetime import datetime, timedelta
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            resp = httpx.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params={
                    "latitude": lat, "longitude": lon,
                    "start_date": start, "end_date": end,
                    "daily": "soil_moisture_0_to_7cm_mean,soil_temperature_0_to_7cm_mean,precipitation_sum",
                    "timezone": "Asia/Kolkata",
                },
                timeout=15,
            )
            resp.raise_for_status()
            d = resp.json()
            daily = d.get("daily", {})
            dates = daily.get("time", [])
            sm    = daily.get("soil_moisture_0_to_7cm_mean", [])
            st    = daily.get("soil_temperature_0_to_7cm_mean", [])
            rain  = daily.get("precipitation_sum", [])
            return {
                "history": [
                    {"date": dates[i], "soil_moisture": sm[i], "soil_temp_c": st[i], "rain_mm": rain[i]}
                    for i in range(len(dates))
                    if sm[i] is not None
                ],
                "lat": lat, "lon": lon,
            }
        except Exception as e:
            return {"error": str(e)}
    data = await loop.run_in_executor(None, _fetch)
    return JSONResponse(data)


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


# ── Todo CRUD ──────────────────────────────────────────────────────────────────

@app.get("/api/todos")
async def api_todos_list(include_done: bool = False):
    return JSONResponse(list_todos(include_done=include_done))


@app.post("/api/todos")
async def api_todos_add(payload: dict = Body(...)):
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    quad = payload.get("quad", "q2")
    tid = add_todo(text, quad)
    return JSONResponse({"id": tid})


@app.put("/api/todos/{tid}")
async def api_todos_update(tid: str, payload: dict = Body(...)):
    update_todo(tid, **payload)
    return JSONResponse({"ok": True})


@app.delete("/api/todos/{tid}")
async def api_todos_delete(tid: str):
    delete_todo(tid)
    return JSONResponse({"ok": True})


# ── System upgrade (pip packages) ──────────────────────────────────────────────

@app.post("/api/guardian/upgrade")
async def guardian_upgrade():
    """Upgrade all outdated pip packages in the project venv + report apt upgradable."""
    import subprocess, shutil

    def _run():
        results = {}
        # Python packages
        pip = shutil.which("pip") or shutil.which("pip3")
        if pip:
            try:
                out = subprocess.run(
                    [pip, "list", "--outdated", "--format=columns"],
                    capture_output=True, text=True, timeout=30
                ).stdout
                pkgs = [l.split()[0] for l in out.strip().splitlines()[2:] if l.strip()]
                results["pip_outdated"] = pkgs
                if pkgs:
                    for p in pkgs[:10]:  # cap at 10 packages
                        subprocess.run([pip, "install", "--upgrade", p],
                                       capture_output=True, text=True, timeout=120)
                    results["pip_upgraded"] = pkgs[:10]
                else:
                    results["pip_upgraded"] = []
            except Exception as e:
                results["pip_error"] = str(e)
        # System packages (read-only — list available, no sudo needed)
        apt = shutil.which("apt")
        if apt:
            try:
                out = subprocess.run(
                    ["apt", "list", "--upgradable"],
                    capture_output=True, text=True, timeout=30
                ).stdout
                sys_pkgs = [l.split("/")[0] for l in out.strip().splitlines()[1:] if "/" in l]
                results["apt_upgradable"] = sys_pkgs[:20]
            except Exception as e:
                results["apt_note"] = str(e)
        log.info("upgrade done: %s", results)
        return results

    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, _run)
    return JSONResponse({"status": "done", "results": res})


@app.post("/api/voice/query")
async def api_voice_query(audio: UploadFile = File(...)):
    """
    Accept WAV (or webm) audio from the browser.
    Browser sends 16kHz mono WAV (PCM) — no ffmpeg required.
    Transcribes with faster-whisper, routes through module pipeline.
    """
    # Detect file format from filename or content-type
    fname    = (audio.filename or "voice.wav").lower()
    ctype    = (audio.content_type or "").lower()
    suffix   = ".wav" if (fname.endswith(".wav") or "wav" in ctype) else ".webm"

    def _process(tmp_path: str) -> dict:
        model = _load_whisper()
        if model is None:
            return {"error": "Whisper model not available — check server logs"}
        try:
            segs, info = model.transcribe(tmp_path, beam_size=3, language=None)
            transcript = " ".join(s.text.strip() for s in segs).strip()
            log.info("voice transcribed: lang=%s  %r", info.language, transcript[:80])
        except Exception as e:
            log.error("transcribe failed: %s", e, exc_info=True)
            return {"error": f"transcription failed: {e}"}

        if not transcript:
            return {"error": "no speech detected — speak closer to the mic"}

        try:
            from core.router import route
            mods = _get_query_modules()
            chosen = route(transcript, mods)
            responses = []
            for name in chosen:
                if name in mods:
                    r = mods[name].handle(transcript, {})
                    responses.append(r.text if hasattr(r, "text") else str(r))
            return {
                "transcript": transcript,
                "module":     chosen[0] if chosen else "unknown",
                "response":   "\n\n".join(responses) if responses else "No response",
            }
        except Exception as e:
            log.error("voice dispatch failed: %s", e)
            return {"transcript": transcript, "error": f"dispatch failed: {e}"}

    tmp_path = ""
    loop = asyncio.get_event_loop()
    try:
        content = await audio.read()
        if not content:
            return JSONResponse({"error": "empty audio received"}, status_code=400)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(content)
            tmp_path = f.name
        result = await loop.run_in_executor(None, _process, tmp_path)
    except Exception as e:
        log.error("voice query error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
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
