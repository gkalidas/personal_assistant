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

from dotenv import load_dotenv
load_dotenv()  # must run before any module reads os.getenv() at import time

import asyncio
import hashlib
import logging
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.responses import Response, StreamingResponse
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
    """Return the module registry used to answer assistant queries (lazy-built)."""
    global _query_modules
    if _query_modules is None:
        with _query_modules_lck:
            if _query_modules is None:
                from core.registry import build_modules
                _query_modules = build_modules()
    return _query_modules

# ── API request tracking ───────────────────────────────────────────────────────
# Inbound endpoint hits + outbound external-API calls live in core.api_stats
# (in-memory live counters + periodic count persistence to the DB).


# ── Whisper model singleton ────────────────────────────────────────────────────
# Loaded once in background at startup; avoids 2-3s delay on first voice query
_whisper_model = None
_whisper_lock  = threading.Lock()

def _load_whisper():
    """Pre-load the Whisper model in the background so first voice query is fast."""
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
    """At startup: report unprocessed photos. Captioning itself is NOT run here —
    it is a HEAVY job (moondream ~1 min/photo) owned by the guardian's idle-aware
    scheduler (task 'diary_caption'), so it runs in an idle window instead of
    competing with the user. The manual /api/diary/write-photos endpoint remains
    for on-demand processing.
    """
    try:
        from core.memory import get_processed_photo_paths
        from pathlib import Path
        photo_exts = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".heif"}
        already = get_processed_photo_paths()
        pending = 0
        for folder in ("Uploads", "Pictures"):
            d = Path.home() / folder
            if not d.exists():
                continue
            # Top-level only — matches scan_photos/caption_pending_photos so the
            # count reflects what will actually be captioned (no phantom pending
            # from subfolders like ~/Pictures/Screenshots/ that scan never reads).
            pending += sum(1 for p in d.iterdir()
                           if p.is_file() and p.suffix.lower() in photo_exts and str(p) not in already)
        if pending:
            log.info("startup: %d unprocessed photo(s) pending — guardian will caption "
                     "them in the next idle window (or use /api/diary/write-photos now)", pending)
    except Exception as e:
        log.error("bg photo check failed: %s", e)


def _bg_todo_verifier():
    """Every 10 minutes, re-run all todo verifiers and reopen stale 'done' tasks."""
    time.sleep(30)  # let server fully start before first check
    while True:
        try:
            from dashboard.todo_verifier import verify_all_todos
            results = verify_all_todos()
            reopened = [r for r in results if not r["passed"]]
            if reopened:
                log.info("todo-verify: %d task(s) failed verification: %s",
                         len(reopened), [r["id"] for r in reopened])
        except Exception as e:
            log.debug("todo-verify sweep error: %s", e)
        time.sleep(600)  # 10 minutes


def _bg_digest_scheduler():
    """Sleep-loop that fires the weekly digest every Sunday at ~19:00 local time."""
    import datetime as _dt
    while True:
        now   = _dt.datetime.now()
        # Sunday = weekday 6; target 19:00
        days_until_sunday = (6 - now.weekday()) % 7
        target = now.replace(hour=19, minute=0, second=0, microsecond=0)
        if days_until_sunday == 0 and now >= target:
            days_until_sunday = 7  # already past target today, wait for next week
        target += _dt.timedelta(days=days_until_sunday)
        sleep_s = (target - now).total_seconds()
        log.info("digest scheduler: next send in %.0fh (Sunday 19:00)", sleep_s / 3600)
        time.sleep(sleep_s)
        try:
            from modules.digest.sender import send_digest
            result = send_digest()
            log.info("digest scheduler: send result=%s", result)
        except Exception as e:
            log.error("digest scheduler: send failed: %s", e)


def _bg_warmup():
    """Pre-build the module registry and warm the router/text models at startup.

    Without this, the first chatbox/voice query pays the full cold cost — lazy
    module import + cold Ollama model load — which is what made an early query
    take minutes. Runs once in the background so it never blocks startup.
    """
    try:
        _get_query_modules()  # import + instantiate all modules once
    except Exception as e:
        log.warning("warmup: module pre-build failed: %s", e)
    try:
        from core.embedding_router import warmup as _embed_warmup
        if _embed_warmup(timeout=180.0):
            log.info("warmup: embedding router ready")
        else:
            log.warning("warmup: embedding router not ready (will use LLM router)")
    except Exception as e:
        log.warning("warmup: embedding router failed: %s", e)
    from core.config import OLLAMA_URL, ROUTER_MODEL, TEXT_MODEL, CHAT_KEEP_ALIVE, NUM_CTX
    import httpx

    def _resident() -> set[str]:
        try:
            r = httpx.get(f"{OLLAMA_URL}/api/ps", timeout=10.0)
            return {m.get("name", "") for m in r.json().get("models", [])}
        except Exception:
            return set()

    # Warm the text model LAST so it's the one left resident. Ollama reloads a model
    # whenever num_ctx changes, so warm the text model at the SAME num_ctx chat calls
    # use — warming at the default 4096 would force a slow reload on the first chat.
    for model in dict.fromkeys([ROUTER_MODEL, TEXT_MODEL]):
        try:
            body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
                    "stream": False, "keep_alive": CHAT_KEEP_ALIVE}
            if model == TEXT_MODEL:
                body["options"] = {"num_ctx": NUM_CTX}
            httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=180.0)
            log.info("warmup: %s ready", model)
        except Exception as e:
            log.warning("warmup: %s failed: %s", model, e)
    # Detect a single-model Ollama (OLLAMA_MAX_LOADED_MODELS=1): if warming the text
    # model evicted the router, only one model can be resident at a time. Reading the
    # env var here is unreliable (it's set on the ollama process, not ours), so probe.
    single_slot = ROUTER_MODEL not in _resident()
    # Pre-load the vision model so background photo captioning isn't a cold ~100s load.
    # Skip it on a single-slot Ollama: it would immediately EVICT the chat model we
    # just warmed, making the next chat pay a reload. Diary captioning is idle-scheduled
    # and loads vision on demand (evicting chat only while it actually runs).
    if single_slot:
        log.info("warmup: single-model Ollama detected — skipping vision pre-warm to keep %s resident", TEXT_MODEL)
    else:
        try:
            from modules.diary.vision import prewarm_vision
            prewarm_vision()
        except Exception as e:
            log.warning("warmup: vision pre-warm failed: %s", e)


def _bg_services():
    """Probe external service health (Ollama, farming server, …) every 15s into a cache."""
    from dashboard.services import refresh
    while True:
        try:
            refresh()
        except Exception as e:
            log.debug("services probe error: %s", e)
        time.sleep(15)


def _bg_ensure_farming():
    """Launch the external farming server when PA starts, if it isn't already up.

    Ties the farming project's lifecycle to PA startup: on boot, if the farming
    server (localhost:5002) isn't responding, launch its run.sh detached so it
    keeps running alongside PA. Already-running servers are left untouched, so a
    PA restart never double-starts it. Opt out with FARMING_AUTOSTART=0; point
    elsewhere with FARMING_DIR.
    """
    import os
    import subprocess
    if os.getenv("FARMING_AUTOSTART", "1").lower() in ("0", "false", "no", ""):
        return
    try:
        from modules.farming.farming_client import is_running, FARMING_SERVER
    except Exception as e:
        log.warning("farming-autostart: client import failed: %s", e)
        return

    if is_running():
        log.info("farming-autostart: server already running at %s — skipping", FARMING_SERVER)
        return

    farming_dir = Path(os.getenv("FARMING_DIR", str(Path.home() / "projects" / "farming")))
    run_sh = farming_dir / "run.sh"
    if not run_sh.exists():
        log.warning("farming-autostart: %s not found — skipping (set FARMING_DIR)", run_sh)
        return

    log_path = Path(__file__).resolve().parent.parent / "logs" / "farming.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "ab") as lf:
            subprocess.Popen(["bash", str(run_sh)], stdout=lf, stderr=lf,
                             cwd=str(farming_dir), start_new_session=True)
        log.info("farming-autostart: launched %s (logs → %s)", run_sh, log_path)
    except Exception as e:
        log.error("farming-autostart: launch failed: %s", e)
        return

    # Poll briefly so the dashboard's services panel flips to 'up' and we can log it.
    for _ in range(20):
        time.sleep(2)
        try:
            if is_running():
                log.info("farming-autostart: server is up at %s", FARMING_SERVER)
                return
        except Exception:
            pass
    log.warning("farming-autostart: launched but not responding yet — check %s", log_path)


def _bg_farming_sync():
    """Every 30 minutes, sync farming project's crop_plots → PA plots/crops tables."""
    time.sleep(5)  # let db.init() complete before first sync
    while True:
        try:
            from modules.farming.sync import sync_crop_plots_to_pa
            result = sync_crop_plots_to_pa()
            if result.get("synced_plots") or result.get("synced_crops"):
                log.info("farming-sync: %s", result)
        except Exception as e:
            log.debug("farming-sync error: %s", e)
        time.sleep(1800)  # 30 minutes


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """FastAPI lifespan: start background services on boot, clean up on shutdown."""
    # API call tracking: log every inbound request + outbound external-API call
    # (httpx) to the DB (api_call_log) for later analysis. Events are buffered
    # and batch-flushed every 15s so per-call DB writes don't add request latency.
    from core import api_stats
    api_stats.install_httpx_tracking()
    api_stats.start_persistence(interval=15.0)
    # Initialise persistent stores
    _ensure_todos()
    _ensure_graph()
    # Init farming DB tables (adds PA tables to shared farming.db without touching farming project's tables)
    try:
        from modules.farming.db import init as _farming_db_init
        _farming_db_init()
    except Exception as e:
        log.warning("farming db init failed: %s", e)
    # Warm up weather + whisper in background
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _wx.get)
    loop.run_in_executor(None, _load_whisper)
    # Auto-process new photos in ~/Pictures (non-blocking)
    threading.Thread(target=_bg_check_new_photos, daemon=True, name="photo-check").start()
    # Encrypted backup of sensitive DBs at startup (non-blocking)
    threading.Thread(target=_backup_dbs, daemon=True, name="db-backup").start()
    # Weekly digest emailer — fires every Sunday ~19:00
    threading.Thread(target=_bg_digest_scheduler, daemon=True, name="digest-sched").start()
    # Periodic todo verifier sweep — re-checks all verifiable tasks every 10 minutes
    threading.Thread(target=_bg_todo_verifier, daemon=True, name="todo-verify").start()
    # Auto-start the external farming server alongside PA (if not already up)
    threading.Thread(target=_bg_ensure_farming, daemon=True, name="farming-autostart").start()
    # Farming sync: mirror crop_plots → PA plots/crops every 30 min
    threading.Thread(target=_bg_farming_sync, daemon=True, name="farming-sync").start()
    # Service health probes (Ollama, farming server, …) — cached, refreshed every 15s
    threading.Thread(target=_bg_services, daemon=True, name="services").start()
    # Pre-build modules + warm router/text models so the first chat isn't cold
    threading.Thread(target=_bg_warmup, daemon=True, name="warmup").start()
    yield
    # Shutdown: flush any pending API call counts to the DB
    api_stats.stop_persistence()


app = FastAPI(title="GK Dashboard", docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.middleware("http")
async def _request_logger(request: Request, call_next):
    """Middleware: log each HTTP request path and latency."""
    t0 = time.monotonic()
    response = await call_next(request)
    # Skip WebSocket upgrade (101) — duration would be entire connection lifetime
    if response.status_code == 101:
        return response
    elapsed = (time.monotonic() - t0) * 1000
    from core.api_stats import record_inbound
    record_inbound(request.method, request.url.path, elapsed, response.status_code)
    log.info("API %-6s %-30s → %d  %.1f ms", request.method, request.url.path,
             response.status_code, elapsed)
    return response


# Mount GraphQL at /graphql (GET = playground, POST = query)
graphql_app = GraphQLRouter(schema, graphql_ide="graphiql")
app.include_router(graphql_app, prefix="/graphql")

# Serve split frontend assets (css/, js/) from the static dir.
# no-cache so the browser revalidates every load (via ETag) — edits show up on a
# normal refresh instead of being stuck on a heuristically-cached old copy.
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope


class _NoCacheStatic(StaticFiles):
    def is_not_modified(self, response_headers, request_headers) -> bool:
        return super().is_not_modified(response_headers, request_headers)

    async def get_response(self, path: str, scope: Scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _NoCacheStatic(directory=str(_STATIC)), name="static")


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
            "NDVI crop health (NASA MODIS)",
            "Soil trend charts",
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
            "Guided photo-question prompts",
            "Draft → review → approve workflow",
            "Stored in SQLite (queryable)",
        ],
    },
    {
        "id": "todo", "label": "Todo Module", "color": "#e3b341",
        "graphql": None,
        "examples": ["show todo list", "add todo buy seeds",
                     "remind me to call vet", "mark buy seeds done"],
        "children": [
            "Natural-language CRUD (no LLM)",
            "Eisenhower quadrant board (/todo)",
            "Completion verifier on done",
            "SQLite-backed, served via /api/todos",
        ],
    },
    {
        "id": "search", "label": "Search Module", "color": "#a5d6ff",
        "graphql": None,
        "examples": ["search drone subsidy 2026", "look up pomegranate export price",
                     "what is integrated pest management"],
        "children": [
            "SearXNG self-hosted backend",
            "DuckDuckGo (DDGS) fallback",
            "Web content injection guardrails",
            "Result summarisation via LLM",
        ],
    },
    {
        "id": "code", "label": "Code Module", "color": "#ffab70",
        "graphql": None,
        "examples": ["analyze this project", "show complex functions",
                     "how many lines of code", "security scan /path"],
        "children": [
            "Directory / project analysis",
            "LLM codebase explanation",
            "Complexity report",
            "Bandit security scan",
            "LOC summary",
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
            "Text chat with the assistant",
            "Voice query (mic → router)",
            "Service-health monitor (/api/services)",
            "System-load / idle analytics",
            "Live agri-news + YouTube feed",
            "Knowledge graph (nodes · edges · graphify)",
            "Face clustering (InsightFace + DBSCAN)",
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
            "Sanitizer — injection patterns + PII redact",
            "Memory — SQLite events log",
            "Action schema validation",
            "Follow-up suggestion engine",
            "General fallback module (small talk)",
            "API call profiling (api_call_log)",
            "GraphQL API (Strawberry, /graphql)",
            "Weekly email digest (Gmail SMTP)",
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
            "NASA MODIS (NDVI crop health)",
            "data.gov.in APMC (mandi prices)",
            "SearXNG · DuckDuckGo (web search)",
            "YouTube / RSS (agri-news feed)",
            "Gmail SMTP (weekly digest)",
            "OSV.dev (CVE databases)",
            "NVD / NIST (CVE + LLM attacks)",
            "CISA KEV (exploited CVEs)",
            "Arxiv cs.CR RSS (AI security)",
        ],
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────

# Payload cache — shared between WS and /api/data so both see the same build
# without doubling the work. Refreshed in a thread every WS tick.
_payload_cache: dict[str, Any] = {}
_payload_lock  = threading.Lock()


def _build_payload() -> dict[str, Any]:
    """Build the dashboard data payload (system, weather, guardian, etc.)."""
    net = _net.status
    sys = get_system_stats()
    wx  = _wx.get()

    from dashboard.services import get_services_status

    return {
        "guardian": get_guardian_status(),
        "services": get_services_status(),
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
    """Serve the dashboard index page."""
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/mindmap")
async def mindmap():
    """Serve the mind-map page."""
    return HTMLResponse((_STATIC / "mindmap.html").read_text(encoding="utf-8"))


@app.get("/todo")
async def todo_page():
    """Serve the todo/quadrant page."""
    return HTMLResponse((_STATIC / "todo.html").read_text(encoding="utf-8"))


@app.get("/guide")
async def guide_page():
    """Serve the user-guide page."""
    return HTMLResponse((_STATIC / "guide.html").read_text(encoding="utf-8"))


_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/showcase")
async def showcase_page():
    """Serve the animated architecture-showcase page."""
    return HTMLResponse((_STATIC / "showcase.html").read_text(encoding="utf-8"), headers=_NO_CACHE)


@app.get("/showcase3d")
async def showcase3d_page():
    """Serve the Three.js 3D system-model page."""
    return HTMLResponse((_STATIC / "showcase3d.html").read_text(encoding="utf-8"), headers=_NO_CACHE)


@app.get("/api/data")
async def api_data():
    """API: return the full dashboard data payload as JSON."""
    with _payload_lock:
        cached = dict(_payload_cache)
    if cached:
        return JSONResponse(cached)
    # Cache cold — build in executor so we don't block the loop
    data = await asyncio.get_event_loop().run_in_executor(None, _build_payload)
    with _payload_lock:
        _payload_cache.clear()
        _payload_cache.update(data)
    return JSONResponse(data)


@app.get("/api/services")
async def api_services():
    """API: current service-health snapshot (Ollama, farming server, modules…)."""
    from dashboard.services import get_services_status
    return JSONResponse(get_services_status())


@app.get("/api/system/load")
async def api_system_load(start: str | None = None, end: str | None = None):
    """System-load analytics for the idle-detection dashboard.

    Optional `start`/`end` (YYYY-MM-DD) filter the historical window.
    """
    from security import load_monitor
    return JSONResponse(load_monitor.analytics(start, end))


@app.get("/api/mindmap")
async def api_mindmap():
    """API: return the mind-map graph data."""
    return JSONResponse({"branches": _MINDMAP_BRANCHES})


@app.post("/api/guardian/scan")
async def guardian_scan():
    """Trigger an on-demand anomaly scan in background."""
    def _run():
        """Background worker thread for this request."""
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
    """On-demand CVE scan + auto-patch of every available safe (same-major) fix.

    Runs synchronously and returns the real patch summary so the dashboard can
    report what actually happened. Unlike the scheduled scan (HIGH+ only), the
    manual button applies fixes at any severity that has a safe upgrade.
    """
    def _run():
        """Background worker thread for this request."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from security.guardian import task_vuln_scan
        return task_vuln_scan(auto_patch=True, severity_threshold="LOW")

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _run)
    except Exception as e:
        log.error("on-demand patch failed: %s", e)
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    pr = result.get("patch_result", {}) or {}
    summary = pr.get("summary", {}) or {}
    patched = [p.get("package") for p in pr.get("patched", []) if p.get("package")]
    manual = [m.get("package") for m in pr.get("manual_review", []) if m.get("package")]
    log.info("on-demand patch: %d package(s) patched", summary.get("patched", 0))
    return JSONResponse({
        "status": "ok",
        "patched": patched,
        "manual": manual,
        "summary": summary,
        "vuln_total": result.get("total_vulns", 0),
    })


@app.get("/api/code/scan")
async def api_code_scan(path: str | None = None):
    """Run the code directory analyzer. Defaults to the project root."""
    import asyncio
    loop = asyncio.get_event_loop()
    def _run():
        """Background worker thread for this request."""
        from modules.code.analyzer import scan_directory
        target = path or str(Path(__file__).parent.parent)
        return scan_directory(target, llm_context=False)
    result = await loop.run_in_executor(None, _run)
    return JSONResponse(result)


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
    """API call analytics — inbound endpoint hits + outbound external-API calls."""
    from core.api_stats import inbound_snapshot, outbound_snapshot
    result = {}
    inbound = inbound_snapshot()
    for key, s in sorted(inbound.items(), key=lambda x: -x[1]["calls"]):
        calls = s["calls"]
        avg_ms = s["avg_ms"]
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
            "avg_ms": avg_ms,
            "max_ms": s["max_ms"],
            "errors": s["errors"],
            "recent": s["recent"],
            "recommendation": rec,
        }
    return JSONResponse({
        "endpoints": result,
        "external": outbound_snapshot(),
        "snapshot_at": datetime.now().isoformat(),
    })


@app.get("/api/stats/log")
async def api_stats_log(before: str | None = None, after: str | None = None,
                       name: str | None = None, direction: str | None = None,
                       source: str | None = None, group: bool = False, limit: int = 1000):
    """Read logged API calls from the DB (filter by date range / api / source)."""
    from core.api_stats import flush_to_db, query_calls
    flush_to_db()  # include buffered events before reading
    rows = query_calls(before=before, after=after, name=name, direction=direction,
                       source=source, group=group, limit=limit)
    return JSONResponse({"rows": rows, "count": len(rows)})


@app.delete("/api/stats/log")
async def api_stats_log_delete(before: str | None = None, after: str | None = None,
                              name: str | None = None, direction: str | None = None,
                              source: str | None = None):
    """Delete logged API calls by date range / api / source (filters ANDed)."""
    from core.api_stats import delete_calls
    if before is None and after is None and name is None and direction is None and source is None:
        return JSONResponse(
            {"error": "refusing to delete all rows; pass a filter (before/after/name/direction/source)"},
            status_code=400)
    deleted = delete_calls(before=before, after=after, name=name,
                          direction=direction, source=source)
    return JSONResponse({"deleted": deleted})


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


@app.get("/api/diary/photo/{filename}")
async def diary_photo_serve(filename: str):
    """Serve a photo from the configured photo directory (path-traversal safe)."""
    from core.memory import load_profile
    from modules.diary.photo_reader import default_photo_dir
    if ".." in filename or filename.startswith("/"):
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    profile = load_profile()
    photo_dir = default_photo_dir(profile)
    target = (photo_dir / filename).resolve()
    if not str(target).startswith(str(photo_dir.resolve())):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not target.exists():
        # Try recursively searching subdirectories one level deep
        matches = list(photo_dir.glob(f"*/{filename}")) + list(photo_dir.glob(filename))
        if not matches:
            return JSONResponse({"error": "not found"}, status_code=404)
        target = matches[0].resolve()
        if not str(target).startswith(str(photo_dir.resolve())):
            return JSONResponse({"error": "invalid path"}, status_code=400)
    suffix = target.suffix.lower()
    media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic"}
    media_type = media_map.get(suffix, "image/jpeg")
    return FileResponse(str(target), media_type=media_type)


@app.post("/api/diary/write-photos")
async def diary_write_photos():
    """Trigger diary write from ~/Pictures in background; return immediately."""
    def _run():
        """Background worker thread for this request."""
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


def _failed_diary_dates(raw: str) -> list[str]:
    """Extract the YYYY-MM-DD dates that failed generation from a draft's error text."""
    import re as _re
    from datetime import datetime
    out = []
    for d in _re.findall(r'Could not generate diary entry for \w+, (\d{2} \w+ \d{4})', raw):
        try:
            out.append(datetime.strptime(d, "%d %B %Y").strftime("%Y-%m-%d"))
        except Exception:
            pass
    return out


def _retry_diary_week(week: str) -> dict:
    """Strip failed entries from a week's draft, un-mark their photos, and regenerate.

    Runs in a worker thread. Returns {ok, cleaned, retried_dates}.
    """
    import re as _re, sqlite3
    from core.memory import get_diary_draft, save_diary_draft
    draft = get_diary_draft(week)
    if not draft:
        return {"ok": False, "error": "week not found"}

    raw = draft.get("draft", "")
    cleaned = _re.sub(r'──[^\n]*\n+\[Could not generate[^\]]*\]\n*', '',
                      raw, flags=_re.MULTILINE).strip()
    failed_date_strs = _failed_diary_dates(raw)

    # Un-mark failed-date photos so they retry; save cleaned draft (or delete if empty).
    conn = sqlite3.connect("personal_assistant.db")
    for ds in failed_date_strs:
        conn.execute("DELETE FROM processed_photos WHERE path LIKE ? OR path LIKE ?",
                     (f"%{ds}%", f"%{ds.replace('-', '')}%"))
        log.info("diary retry: un-marked photos for %s", ds)
    if cleaned:
        save_diary_draft(week, cleaned)
    else:
        conn.execute("DELETE FROM diary_drafts WHERE week=?", (week,))
    conn.commit()
    conn.close()

    try:
        diary = _get_query_modules().get("diary")
        if diary:
            diary.handle("write diary from my photos", {})
    except Exception as e:
        log.error("diary retry write error: %s", e)
    return {"ok": True, "cleaned": bool(cleaned), "retried_dates": failed_date_strs}


@app.post("/api/diary/retry/{week}")
async def diary_retry_week(week: str):
    """Clear a week's failed diary draft, un-mark its photos, and re-run generation."""
    result = await asyncio.get_event_loop().run_in_executor(None, _retry_diary_week, week)
    return JSONResponse(result)


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


@app.get("/api/graph/search")
async def api_graph_search(q: str = "", type: str = ""):
    """Full-text search across graph node labels, aliases, and properties."""
    from core.knowledge_graph import search_nodes
    if not q.strip():
        return JSONResponse({"results": []})
    results = search_nodes(q.strip(), type_=type or None, limit=20)
    return JSONResponse({"results": results})


@app.get("/api/graph/node/{node_id}")
async def api_graph_node(node_id: int):
    """Return a single node by ID."""
    from core.knowledge_graph import get_node
    node = get_node(node_id)
    if not node:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(node)


@app.get("/api/graph/node/{node_id}/neighborhood")
async def api_graph_neighborhood(node_id: int, depth: int = 1):
    """Return the subgraph around a node (vis-network format)."""
    from core.knowledge_graph import node_neighborhood
    return JSONResponse(node_neighborhood(node_id, depth=min(depth, 3)))


@app.delete("/api/graph/node/{node_id}")
async def api_graph_delete_node(node_id: int):
    """Delete a node and all its edges."""
    from core.knowledge_graph import delete_node
    delete_node(node_id)
    return JSONResponse({"ok": True})


@app.post("/api/graph/import-graphify")
async def api_graph_import_graphify(payload: dict = Body(default={})):
    """
    Import a Graphify graph.json into our knowledge graph.
    Pass {"path": "/absolute/path/to/graph.json"} or defaults to project root/graph.json.
    Graphify (github.com/safishamsi/graphify) produces this via: /graphify .
    """
    import json as _json
    from core.knowledge_graph import upsert_node, add_edge

    graph_path = Path(payload.get("path") or
                      Path(__file__).parent.parent / "graph.json")
    if not Path(graph_path).exists():
        return JSONResponse(
            {"error": f"graph.json not found at {graph_path}. "
                      "Run: /graphify . in this project first."},
            status_code=404,
        )

    try:
        data = _json.loads(Path(graph_path).read_text())
    except Exception as e:
        return JSONResponse({"error": f"parse error: {e}"}, status_code=400)

    imported_nodes = _import_graphify_nodes(data.get("nodes") or [])
    edge_count     = _import_graphify_edges(data.get("edges") or [], imported_nodes)
    log.info("graphify import: %d nodes, %d edges", len(imported_nodes), edge_count)
    return JSONResponse({"ok": True, "nodes": len(imported_nodes), "edges": edge_count})


# Graphify node types → our knowledge-graph node types.
_GRAPHIFY_TYPE_MAP = {
    "function": "topic", "class": "topic", "module": "topic",
    "file": "media", "concept": "topic", "person": "person",
    "place": "place", "event": "event",
}


def _import_graphify_nodes(nodes_in: list[dict]) -> dict[str, int]:
    """Upsert Graphify nodes into the knowledge graph. Returns {graphify_id: kg_id}."""
    from core.knowledge_graph import upsert_node
    imported: dict[str, int] = {}
    for n in nodes_in:
        gid   = str(n.get("id") or n.get("name") or "")
        label = (n.get("label") or n.get("name") or gid)[:160]
        if not label:
            continue
        ktype = _GRAPHIFY_TYPE_MAP.get((n.get("type") or "topic").lower(), "topic")
        props = {k: v for k, v in n.items()
                 if k not in ("id", "label", "name", "type") and isinstance(v, (str, int, float))}
        imported[gid] = upsert_node(ktype, label, properties=props)
    return imported


def _import_graphify_edges(edges_in: list[dict], imported_nodes: dict[str, int]) -> int:
    """Add Graphify edges between already-imported nodes. Returns the edge count."""
    from core.knowledge_graph import add_edge
    count = 0
    for e in edges_in:
        src = imported_nodes.get(str(e.get("source") or e.get("from") or ""))
        dst = imported_nodes.get(str(e.get("target") or e.get("to") or ""))
        rel = (e.get("label") or e.get("rel") or e.get("type") or "related_to")[:80]
        if src and dst:
            add_edge(src, dst, rel, weight=float(e.get("weight", 1.0)))
            count += 1
    return count


@app.get("/api/farming/ndvi")
async def api_ndvi(lat: float = 18.1617, lon: float = 75.4218, weeks: int = 6):
    """NDVI crop health from NASA MODIS for given coordinates (defaults to Barloni)."""
    loop = asyncio.get_event_loop()
    def _fetch():
        """Fetch helper for this handler."""
        from modules.farming.ndvi import get_ndvi
        return get_ndvi(lat=lat, lon=lon, weeks_back=weeks)
    data = await loop.run_in_executor(None, _fetch)
    return JSONResponse(data)


@app.get("/api/farming/soil-trend")
async def api_soil_trend(lat: float = 18.1617, lon: float = 75.4218, days: int = 14):
    """Soil moisture + temperature trend from Open-Meteo ERA5 for given coordinates."""
    loop = asyncio.get_event_loop()
    def _fetch():
        """Fetch helper for this handler."""
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


@app.post("/api/news/dismiss")
async def api_news_dismiss(url: str = Body(..., embed=True)):
    """Remove one article from the cache by URL; persists until next server restart."""
    _news.dismiss(url)
    return JSONResponse({"ok": True})


# ── Music: local collection player + discovery (trending / new-for-you) ────────

@app.get("/api/music/library")
async def api_music_library():
    """Return the user's local music collection (from config.MUSIC_DIR)."""
    from modules.personal.library import scan_library
    from core.config import MUSIC_DIR
    loop = asyncio.get_event_loop()
    tracks = await loop.run_in_executor(None, scan_library)
    return JSONResponse({"tracks": tracks, "count": len(tracks), "dir": MUSIC_DIR})


@app.get("/api/music/file/{track_id}")
async def api_music_file(track_id: str):
    """Stream one local audio file by id (sandboxed to MUSIC_DIR; Range/seek OK)."""
    from modules.personal.library import resolve_track
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, resolve_track, track_id)
    if path is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    media_map = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
                 ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
                 ".opus": "audio/ogg"}
    media_type = media_map.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(path), media_type=media_type)


@app.get("/api/music/trending")
async def api_music_trending():
    """Return {trending, new_for_you, age_min} for the Music panel's discovery rows.

    Serves whatever is cached instantly (never blocks on a slow web+LLM fetch);
    any missing row is filled by a background thread and appears on the next poll.
    """
    from modules.personal.music import music_feed, ensure_fetched_async
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, music_feed, False)  # cache-only, no blocking
    ensure_fetched_async()                                      # fill gaps in background
    return JSONResponse(data)


@app.post("/api/guardian/fix-audit")
async def api_guardian_fix_audit(payload: dict = Body(...)):
    """Mute a bandit finding by adding # nosec to the flagged line, then re-audit.

    This only *suppresses* the linter warning — it does not remediate the code.
    CRITICAL/HIGH findings are therefore refused: a real vulnerability must be
    fixed in code, not hidden.
    """
    issue     = payload.get("issue", {})
    file_path = issue.get("file", "")
    line_no   = issue.get("line", 0)
    severity  = (issue.get("severity") or "").upper()

    if severity in ("CRITICAL", "HIGH"):
        return JSONResponse({
            "ok": False, "blocked": True,
            "error": f"{severity} findings must be fixed in code, not muted",
        }, status_code=403)

    if not file_path or not line_no:
        return JSONResponse({"ok": False, "error": "missing file or line"}, status_code=400)

    repo_root = Path(__file__).parent.parent
    target    = (repo_root / file_path).resolve()
    if not str(target).startswith(str(repo_root.resolve())):
        return JSONResponse({"ok": False, "error": "invalid path"}, status_code=400)
    if not target.exists():
        return JSONResponse({"ok": False, "error": "file not found"}, status_code=404)

    lines = target.read_text().splitlines(keepends=True)
    if line_no < 1 or line_no > len(lines):
        return JSONResponse({"ok": False, "error": "invalid line"}, status_code=400)

    # Append # nosec to the flagged line so bandit ignores it in future runs
    line = lines[line_no - 1]
    if "# nosec" not in line:
        lines[line_no - 1] = line.rstrip("\n\r") + "  # nosec\n"
        target.write_text("".join(lines))

    # Re-run the full audit and persist updated results
    try:
        from security.auditor import run_audit
        from security.guardian import _save_report
        result = run_audit(auto_fix_permissions=False)
        _save_report("code_audit", result)
        log.info(f"fix-audit: applied nosec to {file_path}:{line_no}, re-audit found {result['total_issues']} issue(s)")
    except Exception as exc:
        log.warning(f"fix-audit re-audit failed: {exc}")

    return JSONResponse({"ok": True, "fixed": file_path, "line": line_no})


@app.get("/api/news/channels")
async def api_news_channels():
    """Return the live TV news channel lineup with ready-to-embed YouTube URLs."""
    from dashboard.news_channels import channels_payload
    return JSONResponse(channels_payload())


@app.get("/api/news/video")
async def api_news_video(q: str = ""):
    """Return a YouTube video ID for a news article query via DDGS video search."""
    import re as _re

    def _find(query: str) -> str | None:
        """Lookup helper for this handler."""
        try:
            from ddgs import DDGS
            for r in DDGS().videos(query, max_results=5):
                url = r.get("content") or r.get("url") or ""
                m = _re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
                if m:
                    return m.group(1)
        except Exception as e:
            log.debug("news video search failed: %s", e)
        return None

    if not q.strip():
        return JSONResponse({"video_id": None})

    loop = asyncio.get_event_loop()
    video_id = await loop.run_in_executor(None, _find, q.strip()[:120])
    return JSONResponse({"video_id": video_id})


# ── Todo CRUD ──────────────────────────────────────────────────────────────────

@app.get("/api/todos")
async def api_todos_list(include_done: bool = False):
    """API: list todos."""
    return JSONResponse(list_todos(include_done=include_done))


@app.post("/api/todos")
async def api_todos_add(payload: dict = Body(...)):
    """API: add a todo."""
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    quad = payload.get("quad", "q2")
    tid = add_todo(text, quad)
    return JSONResponse({"id": tid})


@app.put("/api/todos/{tid}")
async def api_todos_update(tid: str, payload: dict = Body(...)):
    """API: update a todo."""
    from dashboard.todo_store import get_todo
    from dashboard.todo_verifier import run_verifier

    # If the user is marking a task done, run its verifier first
    if payload.get("done") == 1 or payload.get("done") is True:
        todo = get_todo(tid)
        if todo and todo.get("verifier"):
            loop = asyncio.get_event_loop()
            passed, note = await loop.run_in_executor(
                None, run_verifier, todo["verifier"]
            )
            if not passed:
                # Block the done mark — return failure with reason
                update_todo(tid, verified=-1, verify_note=note)
                return JSONResponse({
                    "ok": False,
                    "blocked": True,
                    "reason": note,
                }, status_code=200)
            # Verifier passed — mark verified and done
            update_todo(tid, verified=1, verify_note=note)

    update_todo(tid, **{k: v for k, v in payload.items()
                        if k not in ("verifier",)})
    return JSONResponse({"ok": True})


@app.post("/api/todos/verify-all")
async def api_todos_verify_all():
    """Re-run all verifiers; reopen any task that claims done but fails verification."""
    from dashboard.todo_verifier import verify_all_todos
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, verify_all_todos)
    return JSONResponse({"results": results})


@app.delete("/api/todos/{tid}")
async def api_todos_delete(tid: str):
    """API: delete a todo."""
    delete_todo(tid)
    return JSONResponse({"ok": True})


# ── Threat pattern auto-fix ────────────────────────────────────────────────────

@app.post("/api/guardian/fix-threat")
async def fix_threat(request: Request):
    """
    Add one or more sanitizer patterns to security/patterns.json and hot-reload.
    Body: {"patterns": [{id, pattern, severity, category, ...}, ...]}
    """
    import json as _json
    body = await request.json()
    patterns_to_add: list[dict] = body.get("patterns", [])
    if not patterns_to_add:
        return JSONResponse({"ok": False, "error": "no patterns provided"}, status_code=400)

    patterns_file = Path("security/patterns.json")
    try:
        data = _json.loads(patterns_file.read_text())
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"cannot read patterns.json: {e}"}, status_code=500)

    existing_ids = {p.get("id") for p in data.get("injection_patterns", [])}
    added = []
    for pat in patterns_to_add:
        if pat.get("id") and pat["id"] not in existing_ids:
            data["injection_patterns"].append(pat)
            data["_meta"]["total_patterns"] = len(data["injection_patterns"])
            existing_ids.add(pat["id"])
            added.append(pat["id"])

    try:
        patterns_file.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"cannot write patterns.json: {e}"}, status_code=500)

    from core.sanitizer import reload_patterns
    active = reload_patterns()
    log.info("threat fix: added %d pattern(s) %s, %d active", len(added), added, active)

    # Patch threat_intel_latest.json so the dashboard reflects the fix on reload.
    fixed_sources = {p.get("source") for p in patterns_to_add if p.get("source")}
    _clear_fixed_threats(fixed_sources)

    return JSONResponse({"ok": True, "added": added, "active": active})


def _clear_fixed_threats(fixed_sources: set) -> None:
    """Drop now-fixed threats (and their alerts) from threat_intel_latest.json.

    Matches a pattern's ``source`` against each vulnerability's threat id so the
    dashboard stops showing threats the user just patched.
    """
    import json as _json
    if not fixed_sources:
        return
    threat_file = Path("logs/security/threat_intel_latest.json")
    if not threat_file.exists():
        return
    try:
        ti = _json.loads(threat_file.read_text())
        remaining = [v for v in ti.get("vulnerabilities", [])
                     if v.get("threat", {}).get("id") not in fixed_sources]
        ti["vulnerabilities"]       = remaining
        ti["vulnerabilities_found"] = len(remaining)
        ti["alerts"] = [a for a in ti.get("alerts", [])
                        if not any(src in a for src in fixed_sources)]
        threat_file.write_text(_json.dumps(ti, indent=2, ensure_ascii=False))
        log.info("threat fix: patched threat_intel_latest.json → %d remaining", len(remaining))
    except Exception as e:
        log.warning("threat fix: could not patch threat_intel_latest.json: %s", e)


# ── System upgrade (pip packages) ──────────────────────────────────────────────

@app.post("/api/guardian/upgrade")
async def guardian_upgrade():
    """Upgrade all outdated pip packages in the project venv + report apt upgradable."""
    import subprocess, shutil

    def _run():
        """Background worker thread for this request."""
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


@app.post("/api/digest/send")
async def api_digest_send():
    """Manually trigger the weekly email digest."""
    from modules.digest.sender import send_digest
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, send_digest)
    if result.get("ok"):
        return JSONResponse(result)
    return JSONResponse(result, status_code=500)


@app.get("/api/digest/preview")
async def api_digest_preview():
    """Return the digest HTML body for browser preview (no email sent)."""
    from modules.digest.builder import build_digest
    loop = asyncio.get_event_loop()
    subject, _, html = await loop.run_in_executor(None, build_digest)
    return JSONResponse({"subject": subject, "html": html})


@app.post("/api/faces/scan")
async def api_faces_scan(payload: dict = Body(default={})):
    """Scan for faces. If 'directory' given, scan that. Otherwise scan ~/Pictures + ~/Uploads."""
    from modules.faces.clusterer import scan_directory
    loop = asyncio.get_event_loop()
    if "directory" in payload:
        result = await loop.run_in_executor(None, scan_directory, payload["directory"])
        return JSONResponse(result)
    # Default: scan all standard photo directories
    totals = {"total_photos": 0, "newly_scanned": 0, "faces_found": 0}
    for folder in ("Pictures", "Uploads"):
        d = Path.home() / folder
        if d.exists():
            r = await loop.run_in_executor(None, scan_directory, str(d))
            totals["total_photos"] += r.get("total_photos", 0)
            totals["newly_scanned"] += r.get("newly_scanned", 0)
            totals["faces_found"]   += r.get("faces_found", 0)
    return JSONResponse(totals)


@app.post("/api/faces/cluster")
async def api_faces_cluster():
    """Run DBSCAN clustering over all stored face embeddings."""
    from modules.faces.clusterer import cluster_all
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, cluster_all)
    return JSONResponse(result)


@app.get("/api/faces/clusters")
async def api_faces_clusters():
    """List all face clusters with name + photo count."""
    from modules.faces import db as fdb
    fdb.init()
    return JSONResponse({"clusters": fdb.list_clusters(), "stats": fdb.stats()})


@app.put("/api/faces/clusters/{cluster_id}")
async def api_faces_rename(cluster_id: int, payload: dict = Body(...)):
    """Rename a face cluster (assign a person's name)."""
    from modules.faces import db as fdb
    fdb.init()
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    fdb.rename_cluster(cluster_id, name)
    return JSONResponse({"ok": True, "cluster_id": cluster_id, "name": name})


@app.get("/api/faces/photo")
async def api_faces_photo(path: str = ""):
    """Return face detections for a specific photo path."""
    from modules.faces import db as fdb
    fdb.init()
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)
    return JSONResponse({"faces": fdb.faces_for_photo(path)})


def _transcribe_and_route(tmp_path: str) -> dict:
    """Transcribe an audio file with Whisper and route the text through the modules.

    Runs in a worker thread. Returns {transcript, module, response} or {error}.
    """
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
        ctx = {"stream_to_stdout": False}
        responses = []
        for name in chosen:
            if name in mods:
                r = mods[name].handle(transcript, ctx)
                responses.append(r.text if hasattr(r, "text") else str(r))
        return {
            "transcript": transcript,
            "module":     chosen[0] if chosen else "unknown",
            "response":   "\n\n".join(responses) if responses else "No response",
        }
    except Exception as e:
        log.error("voice dispatch failed: %s", e)
        return {"transcript": transcript, "error": f"dispatch failed: {e}"}


@app.post("/api/voice/query")
async def api_voice_query(audio: UploadFile = File(...)):
    """Transcribe uploaded WAV/webm audio (faster-whisper) and route it through the modules."""
    fname  = (audio.filename or "voice.wav").lower()
    ctype  = (audio.content_type or "").lower()
    suffix = ".wav" if (fname.endswith(".wav") or "wav" in ctype) else ".webm"

    tmp_path = ""
    try:
        content = await audio.read()
        if not content:
            return JSONResponse({"error": "empty audio received"}, status_code=400)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(content)
            tmp_path = f.name
        result = await asyncio.get_event_loop().run_in_executor(None, _transcribe_and_route, tmp_path)
    except Exception as e:
        log.error("voice query error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
    return JSONResponse(result)


# Conversation-context guards for the dashboard chat: keep the model's prompt
# bounded no matter what the browser sends.
_CTX_RECENT_TURNS = 8        # most recent messages threaded back verbatim
_CTX_MAX_TURNS    = 40       # absolute ceiling we ever look at / summarise
_CTX_MAX_CHARS    = 2000     # per-message content cap

# Cache: hash(older-turns) → compacted summary, so we don't re-summarise the same
# overflow on every request. Bounded to avoid unbounded growth across sessions.
_summary_cache: dict[str, str] = {}
_SUMMARY_CACHE_MAX = 128


def _sanitise_history(history) -> list[dict]:
    """Coerce browser-sent chat history into a clean [{role, content}] list.

    Drops anything that isn't a user/assistant turn, trims over-long messages,
    and keeps only the most recent _CTX_MAX_TURNS so the prompt stays bounded.
    """
    if not isinstance(history, list):
        return []
    clean: list[dict] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            clean.append({"role": role, "content": content[:_CTX_MAX_CHARS]})
    return clean[-_CTX_MAX_TURNS:]


def _compact_history(history) -> list[dict]:
    """Bound the prompt while preserving what the conversation is *about*.

    Keeps the last _CTX_RECENT_TURNS turns verbatim. Anything older is compressed
    into a single leading summary note (cached by content hash) so that even when
    early turns fall out of the window, the model still knows the goal and key
    facts established earlier — instead of silently forgetting them.
    """
    clean = _sanitise_history(history)
    if len(clean) <= _CTX_RECENT_TURNS:
        return clean

    older  = clean[:-_CTX_RECENT_TURNS]
    recent = clean[-_CTX_RECENT_TURNS:]

    key = hashlib.sha1(
        "\n".join(f"{t['role']}:{t['content']}" for t in older).encode()
    ).hexdigest()
    summary = _summary_cache.get(key)
    if summary is None:
        from core.llm import summarize_history
        summary = summarize_history(older)
        if len(_summary_cache) >= _SUMMARY_CACHE_MAX:
            _summary_cache.clear()          # simple bounded cache
        _summary_cache[key] = summary

    if not summary:
        return recent                       # compaction failed — degrade gracefully
    note = {
        "role": "system",
        "content": "Summary of earlier conversation (older turns compacted):\n" + summary,
    }
    return [note, *recent]


def _dispatch_text(text: str, history=None) -> dict:
    """Route a typed message through the same router/modules as voice. Runs in a worker thread."""
    text = (text or "").strip()
    if not text:
        return {"error": "empty message"}
    try:
        from core.router import route
        mods = _get_query_modules()
        chosen = route(text, mods)                 # router now returns a single module
        from core.memory import load_profile
        ctx = {
            "stream_to_stdout": False,             # API path: don't dump replies to stdout
            "history": _compact_history(history),  # recent turns verbatim + summary of older ones
            "profile": load_profile(),             # personal info store (learned prefs, name, …)
        }
        responses = []
        for name in chosen:
            if name in mods:
                r = mods[name].handle(text, ctx)
                responses.append(r.text if hasattr(r, "text") else str(r))
        return {
            "module":   chosen[0] if chosen else "unknown",
            "response": "\n\n".join(responses) if responses else "No response",
        }
    except Exception as e:
        log.error("text query dispatch failed: %s", e)
        return {"error": f"dispatch failed: {e}"}


@app.post("/api/query")
async def api_query(payload: dict = Body(...)):
    """Route a typed chat message through the assistant modules and return the reply."""
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    history = payload.get("history")
    result = await asyncio.get_event_loop().run_in_executor(
        None, _dispatch_text, text, history
    )
    return JSONResponse(result)


def _stream_query_events(text: str, history=None):
    """Yield Server-Sent Events for a chat reply: a module event, then tokens, then done.

    The conversational `general` module streams token-by-token (low perceived
    latency); any other route falls back to a single buffered token event with
    the full reply, so the frontend consumes both the same way.
    """
    import json as _json
    try:
        from core.router import route
        mods = _get_query_modules()
        chosen = route(text, mods)
        name = chosen[0] if chosen else "general"
        mod = mods.get(name)
        if mod is None:
            yield f"data: {_json.dumps({'error': f'no handler for {name}'})}\n\n"
            return
        yield f"data: {_json.dumps({'module': name})}\n\n"
        from core.memory import load_profile
        ctx = {"stream_to_stdout": False, "history": _compact_history(history),
               "profile": load_profile()}
        streamer = getattr(mod, "handle_stream", None)
        if streamer is not None:
            for tok in streamer(text, ctx):
                yield f"data: {_json.dumps({'token': tok})}\n\n"
        else:
            # Non-streaming module: run it normally and emit the whole reply once.
            r = mod.handle(text, ctx)
            full = r.text if hasattr(r, "text") else str(r)
            yield f"data: {_json.dumps({'token': full})}\n\n"
        yield f"data: {_json.dumps({'done': True})}\n\n"
    except Exception as e:
        log.error("stream query dispatch failed: %s", e)
        yield f"data: {_json.dumps({'error': f'dispatch failed: {e}'})}\n\n"


@app.post("/api/query/stream")
async def api_query_stream(payload: dict = Body(...)):
    """Stream a chat reply token-by-token over SSE for lower perceived latency."""
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    return StreamingResponse(
        _stream_query_events(text, payload.get("history")),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """WebSocket: stream live dashboard updates to the browser."""
    await websocket.accept()
    client = websocket.client
    log.info("dashboard connected: %s", client)
    loop = asyncio.get_event_loop()
    try:
        while True:
            # Build payload in thread pool — never blocks the event loop.
            # Other requests (voice, diary, todos) stay responsive during the build.
            payload = await loop.run_in_executor(None, _build_payload)
            with _payload_lock:
                _payload_cache.clear()
                _payload_cache.update(payload)
            await websocket.send_json(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        log.info("dashboard disconnected: %s", client)
    except Exception as e:
        log.error("ws error %s: %s", client, e)
