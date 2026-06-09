"""
Dashboard WebSocket server.

GET /          → serve index.html
WS  /ws        → JSON stream: {network, system, weather} every 2 seconds
GET /api/data  → single JSON snapshot (for debugging)
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard.network import NetworkMonitor
from dashboard.sysmon import get_system_stats
from dashboard.weather_widget import WeatherCache
from dashboard.guardian import get_guardian_status

log = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"

_net = NetworkMonitor()
_wx  = WeatherCache()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Fire-and-forget weather pre-fetch — don't block startup on slow network
    asyncio.get_event_loop().run_in_executor(None, _wx.get)
    yield


app = FastAPI(title="GK Dashboard", docs_url=None, redoc_url=None, lifespan=_lifespan)


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/api/data")
async def api_data():
    return JSONResponse(_build_payload())


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
