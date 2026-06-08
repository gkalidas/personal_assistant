"""
Dashboard WebSocket server.

GET /          → serve index.html
WS  /ws        → JSON stream: {network, system, weather} every 2 seconds
GET /api/data  → single JSON snapshot (for debugging)
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard.network import NetworkMonitor
from dashboard.sysmon import get_system_stats
from dashboard.weather_widget import WeatherCache

log = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"

app = FastAPI(title="GK Dashboard", docs_url=None, redoc_url=None)

# Singletons — created once, live for the server lifetime
_net = NetworkMonitor()
_wx  = WeatherCache()   # uses default Barloni coords


# ── Startup: pre-fetch weather so first WS frame has data ────────────────────

@app.on_event("startup")
async def _prefetch_weather():
    # Fire-and-forget — don't block server startup if network is slow
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _wx.get)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_payload() -> dict[str, Any]:
    net = _net.status
    sys = get_system_stats()
    wx  = _wx.get()

    return {
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
