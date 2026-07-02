"""Service health checks — the external dependencies the assistant relies on.

Probed in a background thread (see server._bg_services) and cached, so the
2-second dashboard payload build never blocks on a network round-trip.

Status values:
  up    — reachable and healthy            (green)
  off   — optional service intentionally not running (amber)
  down  — should be reachable but isn't    (red)
"""

import logging
import threading
import time

import httpx

from core.config import OLLAMA_URL, SEARXNG_URL
from core.registry import MODULE_NAMES

log = logging.getLogger(__name__)

_cache: dict = {"services": [], "modules": {}, "at": None}
_lock = threading.Lock()


def _check_ollama() -> dict:
    """Ping Ollama's /api/tags — the LLM backend every text/router call depends on."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        n = len(r.json().get("models", []))
        return {"name": "Ollama", "kind": "core", "status": "up",
                "detail": f"{n} model{'' if n == 1 else 's'}"}
    except Exception:
        return {"name": "Ollama", "kind": "core", "status": "down", "detail": "unreachable"}


def _check_farming() -> dict:
    """Check the optional external farming LLM server (falls back to local LLM if off)."""
    try:
        from modules.farming.farming_client import is_running, FARMING_SERVER
        host = FARMING_SERVER.replace("http://", "").replace("https://", "")
        if is_running():
            return {"name": "Farming server", "kind": "optional", "status": "up", "detail": host}
        return {"name": "Farming server", "kind": "optional", "status": "off",
                "detail": "offline · using local LLM"}
    except Exception:
        return {"name": "Farming server", "kind": "optional", "status": "off", "detail": "offline"}


def _check_searxng() -> dict | None:
    """Check the optional self-hosted SearXNG instance, if one is configured."""
    if not SEARXNG_URL:
        return None
    host = SEARXNG_URL.replace("http://", "").replace("https://", "")
    try:
        r = httpx.get(SEARXNG_URL, timeout=2.0)
        ok = r.status_code < 500
        return {"name": "SearXNG", "kind": "optional",
                "status": "up" if ok else "down", "detail": host}
    except Exception:
        return {"name": "SearXNG", "kind": "optional", "status": "down", "detail": "unreachable"}


def refresh() -> dict:
    """Probe all services and update the cache. Called from a background thread."""
    services = [_check_ollama(), _check_farming()]
    sx = _check_searxng()
    if sx:
        services.append(sx)
    data = {
        "services": services,
        # Modules run in-process: they're "loaded" whenever the server is up.
        "modules": {"count": len(MODULE_NAMES), "names": list(MODULE_NAMES)},
        "at": time.strftime("%H:%M:%S"),
    }
    with _lock:
        _cache.update(data)
    return data


def get_services_status() -> dict:
    """Return the last cached service status (probes once if it has never run)."""
    with _lock:
        cold = _cache["at"] is None
        snapshot = dict(_cache)
    return refresh() if cold else snapshot
