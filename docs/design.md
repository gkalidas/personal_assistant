# GK Personal Assistant — Design Document

> Extracted from session `1b45e6d1-97dd-4040-8221-36d727c41274` (2026-05-29)  
> Use this to resume the planning conversation on any machine.

---

## What is GK?

A local, private, multi-module personal assistant — named after you (Ganesh Kalidas).  
Think: Jarvis for your actual life.

- Boots with: **"Welcome to the future, GK"**
- Runs 100% on your machine — no cloud, no telemetry, no sync
- Every response is filtered through one question: *does this move you toward health or wealth?*
- Makes you smarter by not just answering — explains the *why*, asks what you'd do, builds reasoning not just knowledge
- Weekly: "here's what you learned this week" summary

---

## Architecture

```
You
 └─► Router (small fast model — classifies intent)
       ├─► farming module  (llama3.2-vision, farming.db, weather/soil tools)
       ├─► finance module  (some model, finance.db)
       ├─► health module   (some model, health.db)  ← deferred
       ├─► fashion module                           ← deferred, complex
       └─► [add more by dropping a new file in modules/]
```

- Router reads your message, routes to the right module(s) — no manual switching
- Each module: own model (swappable), own memory/history, own tools
- Blended answers supported: "adjust farming budget because monsoon hit yield" pulls finance + farming together
- Farming's existing `BaseModule` contract is already the right shape — router layer goes on top

**Confirmed router candidates:** `qwen2.5:3b` vs `llama3.2:3b` — test both on 20 sample queries, pick the winner.

**UI:** Terminal chat first (faster to build, easier to debug routing), then minimal web UI.

---

## Memory System (confirmed: Option C — both)

| Layer | What it does |
|---|---|
| `user_profile.json` | Fast access — goals, health context, financial situation, preferences. Updated as you share. |
| `events.db` | Ground truth — every query + answer stored. Profile is corrected from this. |

---

## Input Layer

Everything normalized locally before hitting the router:

| Input | Processing |
|---|---|
| Text | Direct |
| Document (PDF, Word, Excel) | Extract text locally |
| Photo | Vision model (already in farming) |
| Audio | **Whisper** — local, offline, supports Hindi/Marathi |
| Video | Audio track (Whisper) + sampled frames (vision model) |

**Nothing goes to cloud transcription. All processing on your machine.**

### Timestamp extraction (always stored, layered):

| Source | What we extract |
|---|---|
| Photo | EXIF `DateTimeOriginal`, GPS coordinates, device model |
| Document | File creation date, last modified, author metadata |
| Audio | File creation time, recording duration |
| Video | Creation timestamp, duration, GPS if embedded |
| Everything | Ingestion timestamp (when GK received it) — fallback if no metadata |

*Both "taken at" and "shared at" timestamps stored. Old photo sent today: GK knows both.*

**Libraries:** `Pillow` (EXIF, already in farming stack) + `python-docx`/`pypdf2` for docs — no new heavy dependencies.

---

## Diary Layer

GK writes your diary, you don't:

- Every meaningful interaction → auto-drafted diary entry
- You can also say "GK, note this down" with a voice memo while commuting
- Entries tagged: farming day, health, finance, general
- Stored as structured (queryable) + markdown (readable)

**Trigger:** Both — auto draft after every session, you review/approve at day end.  
*Auto draft = nothing is lost. Explicit review = you stay in the loop and builds reflection habit.*

---

## Analysis Layer ("make me smarter")

Runs on the diary + event log:

- **Weekly:** what decisions did you make, what were the outcomes
- **Pattern detection:** "you always overspend in the last week of the month"
- **Goal alignment score:** are this week's actions moving toward health/wealth?
- **Causal links:** "yield dropped in June → traced to skipping the May spray schedule"

After each answer, GK adds one line: a follow-up question, a connection to something asked before, or "next time you see this, look for Y." One sentence, not a lecture.

---

## Background Intelligence Loop

When idle (CPU low), GK runs a low-priority worker:

- Scans `~/gk/inbox/` for unprocessed photos/videos/documents
- Clusters unknown faces across photos using **InsightFace** (local, runs on GPU/CPU)
- Builds a queue of things it's uncertain about

When you're free, GK surfaces one at a time:
> *"I found this face in 5 photos — March 2023, July 2023, December 2024. Who is this?"*

You give it a name, a story. GK:
- Maps all 5 photos to that person
- Backdates diary entries to actual EXIF timestamps
- Links them into your social graph

**File inbox:** `~/gk/inbox/` — you drop anything there, GK picks it up. (Not watching full `~/Photos` — you stay in control of what GK sees. Expand scope later.)

---

## Knowledge Graph

Every entity GK discovers = a node. Every connection = an edge.

```
[Person: Rahul]
  ├── personal info: name, relationship, birthday, profession
  ├── appears in: 12 photos
  ├── shared events: "Pune trip Jan 2024", "Farm visit May 2025"
  ├── co-appears with: [Person: Amit], [Person: Sister]
  └── timeline: first seen Mar 2023 → last seen Dec 2025

[Photo: IMG_20240315.jpg]
  ├── taken: 15 Mar 2024, 7:42pm
  ├── location: Koregaon Park, Pune
  ├── contains: [Rahul], [Amit], [You]
  └── linked diary entry: "Birthday dinner, Rahul's 30th"
```

**Node types:** People, Places (GPS clusters), Events, Media, Topics (health incidents, financial decisions, farming seasons)

**Edge types:** Person↔Person, Person↔Event, Event↔Place↔Date, Media↔all

**Storage:** SQLite with graph schema — no heavy graph DB needed at personal scale.

**Visualization:** `vis-network` (JavaScript, runs in browser, fully offline) — interactive, zoomable, clickable.

**Link suggestions:** GK suggests links (e.g., name mentioned in voice note → linked to their node), you confirm with one tap. Never auto-commits without approval.

---

## OPSEC Layer — Personal Threat Model

GK acts as your security analyst on your own life.

**1. Information inventory**  
For every piece of data GK holds — who else knows it, how it was shared, when.

**2. Who-knows-what map**  
Each person in your knowledge graph gets a `knows[]` list:
> Rahul → knows: your farm location, financial stress in 2024, health issue (present at hospital visit)

**3. Vulnerability scoring**  
GK flags information that could be misused:
- **Leverage**: sensitive moments, financial pressure, health issues
- **Pattern exposure**: routines, locations, regular contacts
- **Association risk**: being linked to certain people in certain contexts
- **Digital footprint**: metadata in photos/docs that reveals more than intended

Risk levels: low / medium / high. GK suggests:
> "This story + this photo together reveal X. Consider: who else has access to this?"

**What-if scenarios:**
> "If this person became adversarial, what do they already know that could be used against you?"

*This is about being intentional, not fearful. You decide what to share, knowing the full picture.*

---

## Privacy & Security

| Threat | Protection |
|---|---|
| Someone on LAN accessing GK | UI binds to `127.0.0.1` only, never `0.0.0.0` |
| Malicious file (PDF/image) | Process in sandbox, validate type before reading |
| Laptop stolen with data | Sensitive DBs (diary, health, finance) encrypted at rest |
| GK leaking data when searching web | Privacy proxy — anonymized queries only, SearXNG self-hosted |
| Idle background process misbehaving | Audit log — every action GK takes logged with timestamp |
| Unauthorized access to GK | Local passphrase to unlock on startup |

**Web search:** SearXNG (self-hosted on your Linux machine) — fully private, no third party ever sees queries.  
PII never leaves the machine. Generic anonymized queries go out: "what medication treats X" — not "32yo male in Pune with X injury."

---

## Confirmed Decisions (do not re-derive these)

| Decision | Choice |
|---|---|
| Router blending | Yes — cross-module blended answers supported |
| Module structure | GK umbrella repo at `/home/ganesh/projects/personal_assistant`; farming was a prior standalone project |
| Memory system | `user_profile.json` (fast cache) + `events.db` (ground truth log of every query) |
| Diary trigger | Both auto-draft (silent, after every session) + explicit review at day end |
| File inbox | Dedicated `~/personal_assistant/inbox/` only, not full filesystem watch |
| Link suggestion | GK suggests, you confirm one tap — never auto-commit |
| Web search | SearXNG (self-hosted) + LLM guardrails layer for prompt injection defense |
| Router model | `qwen2.5:0.5b` — small, fast, sufficient for intent classification |
| Text model | `qwen3:1.7b` — newer architecture, matches qwen2.5:3b quality at half the RAM |
| UI first | Terminal chat (built), then web UI |
| v1 modules | finance (built), farming+weather (next), health (later), fashion (deferred) |
| Weather source | Open-Meteo — free, no API key, ECMWF-backed, covers India, returns soil/rain/humidity |
| Query logging | Log before + after processing: raw query, module, response, latency → `events.db` |
| Web response security | Strip HTML → prompt injection scan → source trust score → LLM summarises with guardrail instruction |

---

## What's Built vs What's Planned

### Done
| Component | Notes |
|---|---|
| `core/base_module.py` — BaseModule contract | `handle()`, `can_handle()`, `ModuleResponse` |
| `core/router.py` — intent classifier | Routes via Ollama, supports blended multi-module answers |
| `core/memory.py` — memory layer | `user_profile.json` (fast) + `events.db`; pre+post logging with latency |
| `core/analysis.py` — weekly analysis + diary pipeline | Analyses events.db → auto-drafts diary entry via LLM |
| `core/guardrails.py` — web response security | HTML strip → prompt injection scan → source trust scoring → sandboxed LLM summary |
| `modules/finance/` — finance module | Expenses, income, budgets, savings goals, NL → LLM → SQLite |
| `modules/farming/` — farming module | Plots, crops, spray logs, observations, season summary |
| `modules/farming/weather.py` — full Open-Meteo integration | Current conditions (WMO codes) + 7-day forecast + spray-safe check + ERA5 crop history + rainfall history + 6h SQLite cache. **Live and tested.** |
| `modules/farming/soil.py` — SoilGrids integration | pH, nitrogen, organic carbon, clay %, sand %, soil type estimate. Free, no API key. |
| `modules/farming/geocode.py` — location resolver | Location name → lat/lon via Open-Meteo geocoding. In-memory cache. |
| `modules/farming/knowledge.py` — disease knowledge base | Loads `crops/*.json`, provides disease lookup + LLM-ready KB context |
| `modules/farming/farming_client.py` — farming server bridge | Delegates photo disease diagnosis to standalone farming FastAPI server (localhost:5002) |
| `crops/pomegranate.json` — disease KB | Bacterial Blight, Anthracnose, Alternaria, Cercospora, Fruit Borer, Sunburn, Healthy — with exact dosages and spray timing rules |
| `main.py` — terminal chat loop | Routes input, pre+post logs with latency, boots with "Welcome to the future, GK" |
| venv + dependencies | `~/envs/evn_personal_assistant/`, httpx + python-dotenv installed |

### In Progress
| Component | Notes |
|---|---|
| Ollama setup | Installed v0.30.6; `llama-server` binary missing (known 0.30.x bug). Run `curl -fsSL https://ollama.com/install.sh \| sudo sh` in terminal to fix. |

### Build Queue (sequenced — each depends on the previous)

**Step 1 — Unblock inference**
- [ ] Fix Ollama / pull `qwen2.5:0.5b` (router) + `qwen3:1.7b` (text)

**Step 2 — First test run** *(farming + finance + weather all ready, just need Ollama)*
- [ ] Test: `"spent 500 on seeds"`, `"will it rain this week?"`, `"add my north field, 3 acres"`

**Step 3 — Query logging pipeline** *(three stages, built together)*
- [ ] Stage 1: Log every query before + after processing (raw query, module, latency) → `events.db`
- [ ] Stage 2: Weekly analysis job — patterns, category distribution, time-of-day, repeats
- [ ] Stage 3: Diary auto-draft — turn weekly analysis into a readable diary entry for review

**Step 4 — Web search** *(single feature: SearXNG + guardrails)*
- [ ] Self-host SearXNG on local machine
- [ ] Guardrails layer: strip HTML → prompt injection scan → source trust scoring → sandboxed LLM summary

**Step 5 — Input layer**
- [ ] Audio — Whisper (local, offline, Hindi/Marathi)
- [ ] Photos — EXIF extraction (timestamp, GPS, device)
- [ ] Documents — PDF (`pypdf2`), Word (`python-docx`)

**Step 6 — Knowledge graph** *(strict sequence)*
- [ ] SQLite graph schema — People, Places, Events, Media nodes + edges
- [ ] Face clustering — InsightFace, scans `~/personal_assistant/inbox/`
- [ ] "Who is this?" surfacing — GK surfaces unknown faces one at a time, you name them
- [ ] Graph population — named people backdated to EXIF timestamps, linked to events

**Step 7 — OPSEC layer**
- [ ] Who-knows-what map — per-person `knows[]` list derived from knowledge graph
- [ ] Vulnerability scoring — leverage, pattern exposure, association risk, digital footprint
- [ ] What-if scenario generator — "if this person became adversarial, what do they already know?"

**Step 8 — Security infrastructure** *(do together)*
- [ ] Encrypt sensitive DBs at rest (diary, health, finance)
- [ ] Local passphrase unlock on startup

**Step 9 — Web UI**
- [ ] vis-network interactive mind map — offline, zoomable, clickable nodes (People, Events, Places, Media)

### Deferred
- Health module
- Fashion module

---

## Current Status (updated 2026-06-10)

All core modules are **built and running**. Current focus: dashboard UX polish + new integrations.

### What's Now Live
| Component | Status |
|---|---|
| `core/router.py` — embedding fast-path (FastEmbed + HNSWlib) | ✅ Live — 1ms cosine match before LLM router |
| `core/llm.py` — streaming tokens, fallback model | ✅ qwen3:1.7b primary, qwen2.5:3b fallback |
| `core/mistake_log.py` — dual-sink error journal | ✅ SQLite + JSONL |
| `modules/health/` | ✅ BP, steps, weight, sleep, blood sugar |
| `modules/system/` | ✅ CPU heatmap, load pattern, scheduler |
| `modules/diary/` | ✅ Photo EXIF → vision captions → LLM diary |
| `modules/search/` | ✅ DuckDuckGo + LLM summarise (streaming) |
| `dashboard/server.py` — FastAPI + WebSocket | ✅ Live at :8765 |
| `dashboard/static/index.html` — 4-col grid | ✅ Colored bars, WiFi labels, blink-by-severity |
| `dashboard/news_widget.py` — auto-refresh 2.5min | ✅ Source denylist, background thread |
| `dashboard/todo_store.py` — SQLite todos API | ✅ CRUD at /api/todos |
| `dashboard/guardian.py` — security guardian | ✅ CVE scan, audit, threat intel |
| Voice input (WAV) | ✅ Browser → WAV → faster-whisper tiny |
| ffmpeg | ✅ Installed (for video/audio processing) |

### Photos → Diary
**Put photos in `~/Pictures`** — the diary module auto-detects this as the default photo directory.

To write diary from today's photos:
```
python main.py
> write diary from my photos
```
Or in the voice panel: say *"write diary from my photos"*

The system reads EXIF timestamps, captions each photo with moondream (vision model), then writes a narrative diary entry with qwen3:1.7b.

### Pending Integrations (Priority Order)

| Priority | Task |
|---|---|
| P1 | GodsView AI satellite monitoring (NDVI for farm plots) |
| P1 | Code directory analyzer module (`modules/code/`) |
| P2 | Satellite NDVI crop health widget in dashboard |
| P2 | Real-time plot boundary overlay on farm panel |
| P3 | Weekly email digest (diary + finance summary) |
| P3 | Embedding router phrase expansion (improve routing accuracy) |
| P4 | Face clustering (InsightFace, from design phase) |
| P4 | Knowledge graph (People / Places / Events nodes) |

### GodsView AI
Website: https://godsviewai.com — "Real Time Satellite Intelligence and Global Monitoring Platform"
Use case for GK: NDVI crop health, soil moisture from satellite for Barloni farm plot.
**Action:** Review API docs, check free tier → integrate into `modules/farming/satellite.py`

### Code Directory Analyzer (Planned: `modules/code/module.py`)
Capabilities planned:
- List + count files by language
- LOC count, complexity metrics (radon)
- LLM: "explain this codebase", "what does this directory do"
- Suggest + apply changes (with approval)
- Integration with security guardian (bandit scan on demand)

---

## How to Resume

Open `/home/ganesh/projects/personal_assistant/docs/design.md` in a new session and say:

> "Read the design doc. Resume the build."

**Active top priority:** Integrate GodsView AI satellite API into farming module.
**Next:** Build `modules/code/` directory analyzer.
