#!/usr/bin/env python3
"""
Record the /showcase3d Three.js model into docs/showcase3d.mp4 + .gif for the README.

Unlike the 2D recorder (which steps a deterministic hook), the 3D scene animates
continuously, so this captures real-time frames while it auto-rotates:
  1. serve dashboard/static over a local http.server (/static/* resolves)
  2. launch headless Chrome with WebGL enabled (SwiftShader) + DevTools
  3. wait for Three.js (CDN) to load and the scene to appear, then grab N frames
  4. ffmpeg assembles an mp4 + palette gif

Requires google-chrome + ffmpeg on PATH, and internet (Three.js loads from a CDN).
Run from a normal terminal (it starts a local server + browser).
"""

import asyncio
import base64
import json
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
import websockets

ROOT      = Path(__file__).resolve().parent.parent
SERVE_DIR = ROOT / "dashboard"
PAGE_URL  = "http://127.0.0.1:{port}/static/showcase3d.html"
FRAMES    = ROOT / "logs" / "showcase3d_frames"
DOCS      = ROOT / "docs"
HTTP_PORT = 8801
CDP_PORT  = 9224
W, H      = 1100, 720
N_FRAMES  = 150
FRAME_DT  = 0.07     # seconds between captures (~14 fps source)
FPS       = 14


def _start_http() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "http.server", str(HTTP_PORT), "--bind", "127.0.0.1"],
        cwd=str(SERVE_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _start_chrome() -> subprocess.Popen:
    return subprocess.Popen(
        ["google-chrome", "--headless=new", "--no-sandbox", "--hide-scrollbars",
         "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
         "--ignore-gpu-blocklist", "--enable-webgl",
         f"--remote-debugging-port={CDP_PORT}", f"--window-size={W},{H}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _ws_url() -> str:
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
    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    async def call(self, method: str, params: dict | None = None) -> dict:
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
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        cdp = CDP(ws)
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False})
        await cdp.call("Page.navigate", {"url": PAGE_URL.format(port=HTTP_PORT)})

        # wait until the loading overlay is gone (scene built) or time out
        for _ in range(60):
            await asyncio.sleep(0.25)
            r = await cdp.call("Runtime.evaluate", {"returnByValue": True,
                "expression": "(document.getElementById('loading')||{}).style?.display==='none'"})
            if r["result"].get("value"):
                break
        await asyncio.sleep(1.0)

        for n in range(N_FRAMES):
            shot = await cdp.call("Page.captureScreenshot", {"format": "png"})
            (FRAMES / f"f{n:04d}.png").write_bytes(base64.b64decode(shot["data"]))
            await asyncio.sleep(FRAME_DT)
        return N_FRAMES


def _assemble() -> None:
    mp4, gif, pal = DOCS / "showcase3d.mp4", DOCS / "showcase3d.gif", FRAMES / "pal.png"
    subprocess.run(["ffmpeg", "-y", "-framerate", str(FPS), "-i", str(FRAMES / "f%04d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ffmpeg", "-y", "-i", str(mp4), "-vf",
                    f"fps={FPS},scale=820:-1:flags=lanczos,palettegen", str(pal)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ffmpeg", "-y", "-i", str(mp4), "-i", str(pal), "-lavfi",
                    f"fps={FPS},scale=820:-1:flags=lanczos[x];[x][1:v]paletteuse", str(gif)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  mp4 → {mp4}  ({mp4.stat().st_size // 1024} KB)")
    print(f"  gif → {gif}  ({gif.stat().st_size // 1024} KB)")


def main() -> int:
    if FRAMES.exists():
        shutil.rmtree(FRAMES)
    FRAMES.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    http, chrome = _start_http(), _start_chrome()
    try:
        n = asyncio.run(_capture(_ws_url()))
        print(f"captured {n} frames")
        _assemble()
        return 0
    finally:
        for p in (chrome, http):
            try:
                p.send_signal(signal.SIGTERM); p.wait(timeout=5)
            except Exception:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
