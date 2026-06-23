# Architecture Showcase

An animated, presentable explainer of how the GK Personal Assistant works —
the **GUARDIAN** security shield (outer ring) protecting the **CORE/ROUTER**,
which dispatches to the **modules**, with a packet animating through real
(scripted) request flows.

## View it live

Start the dashboard, then open **`/showcase`** (e.g. `http://<tailscale-ip>:8765/showcase`).

Controls: scenario tabs, ▶ play / ⏸ pause, ◀ prev / next ▶, ↻ replay.

Scenarios (scripted, always demo-safe):
1. **Weather query** — user → guardian (clean) → router → farming → Open-Meteo → reply
2. **Blocked jailbreak** — sanitizer flags injection; packet stops red at the guardian ring
3. **Crop forecast** — PA's weather model hands its forecast to the external farming app (llama3)
4. **Voice query** — local Whisper transcription → guardian → router → farming → SQLite

## Make a GIF / MP4 for slides

A deterministic headless recorder turns the page into video files:

```bash
python3 scripts/record_showcase.py
```

Outputs:
- `docs/showcase.mp4` (H.264, for slides / sharing)
- `docs/showcase.gif` (palette-optimised, for chat / README embeds)

How it works: it serves `dashboard/` on `127.0.0.1`, drives headless Chrome over
the DevTools Protocol, calls the page's deterministic `window.__renderFrame(si,
sti, p)` hook for every scenario/step (no animation-timing flakiness), screenshots
each frame, then `ffmpeg` assembles the mp4 + gif. Requires `google-chrome` and
`ffmpeg` on PATH (both already installed on this host).

> Note: must be run from a normal terminal, not inside a restricted sandbox —
> the recorder launches a local web server and a headless browser, which
> sandboxed shells block.
