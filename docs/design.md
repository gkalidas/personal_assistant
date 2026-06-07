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
| Module structure now | Keep farming as standalone; GK umbrella built later |
| Memory system | Option C — `user_profile.json` (fast) + `events.db` (ground truth) |
| Diary trigger | Both auto (silent) + explicit review at day end |
| File inbox | Dedicated `~/gk/inbox/` only, not full filesystem watch |
| Link suggestion | GK suggests, you confirm one tap — never auto-commit |
| Web search | SearXNG (self-hosted), not DDG API |
| Router model | Test `qwen2.5:3b` vs `llama3.2:3b` on 20 queries, pick winner |
| UI first | Terminal chat, then web UI |
| v1 modules | farming (done), finance (next), health (later), fashion (deferred) |

---

## What's Built vs What's Planned

| Component | Status |
|---|---|
| Farming module (crop advisor, disease detection, weather, soil, plots) | **Built and shipped** |
| Linux deployment (setup.sh, start.sh, systemd service) | **Built** |
| GK router + multi-module shell | **Planned, not started** |
| Finance module | **Planned, not started** |
| Diary layer | **Planned, not started** |
| Knowledge graph + people ID (InsightFace) | **Planned, not started** |
| OPSEC layer | **Planned, not started** |
| Background intelligence loop | **Planned, not started** |
| Privacy proxy (SearXNG) | **Planned, not started** |
| Web UI (vis-network mind map) | **Planned, not started** |

---

## How to Resume This Conversation

Paste this document into a new Claude session and say:

> "This is the GK personal assistant design we planned. I want to start building it. Let's begin with [component]."

Suggested starting point: **finance module** — simplest, no vision needed, text-only, directly tied to wealth goal, and best test case for cross-module blending with farming.
