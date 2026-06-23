#!/usr/bin/env python3
"""
Record the /showcase architecture animation into docs/showcase.mp4 + .gif.

Headless pipeline (no playwright/puppeteer needed — just google-chrome + ffmpeg):
  1. serve dashboard/static over a local http.server so /static/* assets resolve
  2. drive Chrome via the DevTools Protocol (websocket): for every scenario/step,
     call the page's deterministic window.__renderFrame(si, sti, p) hook and grab
     a screenshot — fully reproducible, no animation-timing flakiness
  3. ffmpeg assembles the PNG frames into an mp4, then a palette-based gif
"""

import asyncio
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
import websockets

ROOT      = Path(__file__).resolve().parent.parent
STATIC    = ROOT / "dashboard"                      # serve from here so /static/* works
PAGE_URL  = "http://127.0.0.1:{port}/static/showcase.html"
FRAMES    = ROOT / "logs" / "showcase_frames"
DOCS      = ROOT / "docs"
HTTP_PORT = 8799
CDP_PORT  = 9223
W, H      = 960, 900
MOTION_P  = [0.34, 0.67, 1.0]   # packet progress samples while moving into a step
HOLD      = 3                   # extra frames held at the step (p=1.0)
FPS       = 10


def _start_http() -> subprocess.Popen:
    """Serve dashboard/ on localhost so the page's absolute /static/* paths resolve."""
    return subprocess.Popen(
        [sys.executable, "-m", "http.server", str(HTTP_PORT), "--bind", "127.0.0.1"],
        cwd=str(STATIC), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _start_chrome() -> subprocess.Popen:
    """Launch headless Chrome with the DevTools endpoint open."""
    return subprocess.Popen(
        ["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
         "--hide-scrollbars", f"--remote-debugging-port={CDP_PORT}",
         f"--window-size={W},{H}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _ws_url() -> str:
    """Poll the CDP HTTP endpoint until the page target's websocket URL is ready."""
    for _ in range(50):
        try:
            for t in requests.get(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2).json():
                if t.get("type") == "page":
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("Chrome DevTools endpoint never came up")


class CDP:
    """Minimal DevTools-Protocol client over a single websocket."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    async def call(self, method: str, params: dict | None = None) -> dict:
        """Send one CDP command and wait for its matching reply."""
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})


async def _capture(ws_url: str) -> int:
    """Render every scenario/step frame and write PNGs to FRAMES. Returns frame count."""
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        cdp = CDP(ws)
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False})
        await cdp.call("Page.navigate", {"url": PAGE_URL.format(port=HTTP_PORT)})
        await asyncio.sleep(2.0)  # let DOMContentLoaded + init() + fonts settle

        counts = (await cdp.call("Runtime.evaluate",
                  {"expression": "window.__renderFrame ? __renderFrame(0,0,1) : null",
                   "returnByValue": True}))["result"].get("value")
        if not counts:
            raise RuntimeError("__renderFrame hook not found on page")

        n = 0
        for si, n_steps in enumerate(counts):
            for sti in range(n_steps):
                samples = list(MOTION_P) + [1.0] * HOLD
                for p in samples:
                    await cdp.call("Runtime.evaluate",
                                   {"expression": f"__renderFrame({si},{sti},{p})"})
                    shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
                    (FRAMES / f"f{n:04d}.png").write_bytes(base64.b64decode(shot["data"]))
                    n += 1
        return n


def _assemble(frame_count: int) -> None:
    """ffmpeg: frames -> mp4 (h264) -> palette gif."""
    mp4 = DOCS / "showcase.mp4"
    gif = DOCS / "showcase.gif"
    pal = FRAMES / "palette.png"
    subprocess.run(
        ["ffmpeg", "-y", "-framerate", str(FPS), "-i", str(FRAMES / "f%04d.png"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp4), "-vf",
         f"fps={FPS},scale=760:-1:flags=lanczos,palettegen", str(pal)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp4), "-i", str(pal), "-lavfi",
         f"fps={FPS},scale=760:-1:flags=lanczos[x];[x][1:v]paletteuse", str(gif)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"  mp4 → {mp4}  ({mp4.stat().st_size // 1024} KB)")
    print(f"  gif → {gif}  ({gif.stat().st_size // 1024} KB)")


def main() -> int:
    """Orchestrate http server + chrome + capture + ffmpeg, cleaning up processes."""
    if FRAMES.exists():
        shutil.rmtree(FRAMES)
    FRAMES.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(exist_ok=True)

    http = _start_http()
    chrome = _start_chrome()
    try:
        ws_url = _ws_url()
        n = asyncio.run(_capture(ws_url))
        print(f"captured {n} frames")
        _assemble(n)
        return 0
    finally:
        for p in (chrome, http):
            try:
                p.send_signal(signal.SIGTERM)
                p.wait(timeout=5)
            except Exception:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
