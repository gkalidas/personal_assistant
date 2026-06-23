# GK Personal Assistant

> "Welcome to the future, GK."

A local, private, router-based personal assistant for farming, finance, and health.
Runs **100% on your machine** — no cloud, no telemetry, no data leaves the house.

---

## Live Architecture Showcase

A single glance at how the whole system fits together — the **Guardian** security
shell wrapping the **Router** core and its modules, reaching the internet through
the guardian and deflecting constant attacks at the perimeter.

<p align="center">
  <img src="docs/showcase3d.gif" alt="GK Personal Assistant — 3D system model" width="82%">
</p>

These are **live, interactive** pages served by the dashboard (drag to rotate the 3D one):

| | URL |
|---|---|
| 2D animated request flow | `http://<host>:8765/showcase` |
| 3D system model | `http://<host>:8765/showcase3d` |

> The GIF above is generated from the live `/showcase3d` page. To (re)create it —
> needs `google-chrome` + `ffmpeg` on PATH:
> ```bash
> python3 scripts/record_showcase3d.py   # -> docs/showcase3d.gif (+ .mp4)
> python3 scripts/record_showcase.py     # -> docs/showcase.gif   (2D flow)
> ```
> Then commit the generated files so they render here on GitHub.

---

## Mind Map

![Architecture Mind Map](docs/mindmap.png)

---

## Architecture Flow

```mermaid
flowchart TD
    You([You]) -->|natural language| SAN

    subgraph Input Layer
        SAN["Sanitizer\n45 injection patterns\nPII redactor"]
    end

    SAN --> RTR

    subgraph Routing
        RTR["Router\nqwen2.5:0.5b\nfast intent classifier"]
    end

    RTR -->|farming| FM
    RTR -->|finance| FN
    RTR -->|health| HL
    RTR -->|system| SY
    RTR -->|multi-module| FM & FN

    subgraph Modules
        FM["Farming Module\nqwen3:1.7b"]
        FN["Finance Module\nqwen3:1.7b"]
        HL["Health Module\nqwen3:1.7b"]
        SY["System Module\nno LLM - structured data"]
    end

    FM --> FT & FDB & WAPI & SAPI & KB
    FN --> FNDB
    HL --> HDB
    SY --> LM & GR

    subgraph Tools and Data
        FT["Farming Tools\nplot/crop/spray/obs CRUD"]
        FDB[(farming.db)]
        FNDB[(finance.db)]
        HDB[(health.db)]
        KB["Disease KB\nPomegranate - Sugarcane - Banana"]
    end

    subgraph ExternalAPIs["External APIs — all free, no auth"]
        WAPI["Open-Meteo\nweather - forecast - ERA5"]
        SAPI["SoilGrids\npH - N - clay - sand"]
        GEO["Nominatim\ngeocoding"]
    end

    subgraph Memory
        MEM[(personal_assistant.db\nevents log)]
        PRF[user_profile.json]
    end

    FM & FN & HL --> MEM
    RTR --> MEM

    subgraph SecurityGuardian["Security Guardian — runs on boot, idle-aware"]
        GR["Guardian Daemon\nsystemd user service"]
        LM["Load Monitor\nCPU - RAM - IO - queries"]
        GR -->|idle?| LM
        LM -->|score less than 25| SCH
        SCH["Idle Scheduler\none task at a time\n3 min cooldown"]
        SCH --> CVE & AUD & TI & PAT & ANO
        CVE["CVE Scanner\nOSV.dev batch API\ndaily"]
        AUD["Code Auditor\nbandit + 15 patterns\nweekly"]
        TI["Threat Intel\nNVD - GitHub - CISA - Arxiv\nevery 12h"]
        PAT["Pattern Watcher\nNVD to patterns.json\nevery 6h"]
        ANO["Anomaly Detector\nevents log scan\nhourly"]
        TI -->|replicate attack| REP
        REP["Attack Replication\nsanitizer test\npackage test\ncode scan"]
        REP -->|vulnerable| ALT["Alert + Fix\nno auto-fix"]
        CVE -->|safe upgrade| PTC["Auto-Patcher\nsame major version only"]
    end
```

---

## What GK Can Do

### Farming
| What you say | What GK does |
|---|---|
| "weather today at my farm" | Live conditions from Open-Meteo at your plot coordinates |
| "7-day forecast Solapur" | ECMWF-backed daily forecast + rain probability |
| "safe to spray tomorrow?" | Wind, humidity, rain check - YES / NO / WAIT |
| "rainfall last 30 days" | ERA5 historical archive from Open-Meteo |
| "soil data for gk_north" | pH, nitrogen, organic carbon, clay % via SoilGrids API |
| "pomegranate has yellow spots" | Disease KB lookup - Cercospora / Alternaria / deficiency |
| "log copper spray 250g/15L on gk_north" | Spray log stored to farming.db |
| "add plot barloni, 3 acres, red soil" | Plot management |
| "plant G9 banana on barloni" | Crop tracking (plant to harvest to yield) |
| "show open field observations" | All logged observations (disease, pest, growth) |

**Crops with full disease + pest + fertilizer KB:**
- Pomegranate (Telya, Bacterial Blight, Cercospora, Alternaria, Fruit Borer, Thrips)
- Sugarcane (Red Rot, Wilt, Grassy Shoot, Pokkah Boeng, Top Borer, Pyrilla)
- Banana (Panama Wilt TR4, Sigatoka, BBTV, Rhizome Rot, Stem Weevil)

### Finance
| What you say | What GK does |
|---|---|
| "spent 800 on drip repair" | Logs expense, category: Farm Maintenance |
| "earned 25000 from pomegranate" | Logs income, category: Crop Sale |
| "this month's summary" | Income vs expenses vs net for the month |
| "budget status" | Each category vs monthly cap, over-budget flags |
| "set seeds budget 10000 per month" | Budget cap stored |
| "add goal: buy pump, target 50000" | Goal tracking with deadline |

### Health
| What you say | What GK does |
|---|---|
| "BP was 130 over 85" | Logs + interprets (6 levels: Normal to Crisis to call 108) |
| "walked 9000 steps" | Logs vs daily goal, percentage achieved |
| "weight 74.5 kg" | Logs to health.db |
| "slept 6.5 hours" | Quality assessment (7-9h optimal, <5h warning) |
| "fasting sugar 105" | Pre-diabetic range flag, interpretation |
| "show BP trend last 2 weeks" | Day-by-day table with status per reading |
| "set step goal 12000" | Goal stored, future readings measured against it |

### System
| What you say | What GK does |
|---|---|
| "is the system busy?" | Live CPU / RAM / IO / load score (0-100) |
| "show me the load pattern" | 24-hour heatmap of your actual usage |
| "security guardian status" | Latest CVE scan, anomaly, threat intel results |
| "when does the guardian run next?" | Task schedule with last-run times and overdue status |

---

## Security Guardian

Runs automatically on every boot (systemd user service, `loginctl linger`).
Monitors when you are NOT working (load score < 25), then runs one task at a time:

| Task | Interval | What it does |
|---|---|---|
| Anomaly detector | 1 hour | Scans events log for injection attempts, error spikes, module failures |
| Pattern watcher | 6 hours | Fetches NVD AI/LLM CVEs, updates security/patterns.json |
| Threat intelligence | 12 hours | Fetches NVD + GitHub Advisory + CISA KEV + Arxiv, replicates each attack against our system, alerts with fix if vulnerable |
| CVE scanner | 24 hours | Checks all 29 packages against OSV.dev (NVD + GitHub + PyPI + 20 DBs) |
| Code auditor | 7 days | Runs bandit + 15 custom patterns on core/, modules/, scripts/ |

**Rules:**
- One task at a time, 3-minute cool-down between tasks
- If a task is 3x overdue, runs regardless of load (prevents indefinite skipping)
- Auto-patches only safe upgrades (same major version, HIGH+ CVE)
- Threat intel vulnerabilities: alert with manual fix only, never auto-fix
- 45 injection patterns hot-reloaded into sanitizer without restart

---

## Stack

| Component | Choice | Reason |
|---|---|---|
| Text LLM | qwen3:1.7b | Best quality at ~2GB RAM, 4-12s/query |
| Router LLM | qwen2.5:0.5b | <1s routing, good JSON adherence |
| Inference | Ollama (local) | No cloud, no API key |
| Storage | SQLite (3 DBs) | Zero-dependency, fast, per-module isolation |
| Profile | JSON | Human-readable, git-ignored |
| Security | OSV.dev + NVD + GitHub Advisory + CISA KEV | All free, no auth |

---

## Setup

```bash
# 1. Activate virtualenv
source ~/envs/evn_personal_assistant/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and fill your profile
cp user_profile.template.json user_profile.json

# 4. Pull models
ollama pull qwen2.5:0.5b
ollama pull qwen3:1.7b

# 5. Run
python main.py
```

### Auto-start security guardian on boot
```bash
systemctl --user enable gk-guardian.service
loginctl enable-linger $USER
```

### Manual security commands
```bash
python security/guardian.py scan       # full scan now
python security/guardian.py intel      # threat intel only
python security/guardian.py vuln       # CVE scan only
python security/guardian.py audit      # code audit only
python security/guardian.py load       # show load + usage pattern
```

---

## Project Structure

```
personal_assistant/
├── main.py                        # Entry point - MODULES + router dispatch
├── core/
│   ├── router.py                  # LLM-based intent router (qwen2.5:0.5b)
│   ├── sanitizer.py               # Injection filter + PII redactor
│   ├── memory.py                  # Events DB + profile loader
│   ├── guardrails.py              # Web content injection defense
│   └── base_module.py             # BaseModule + ModuleResponse contract
├── modules/
│   ├── farming/                   # Weather, soil, plots, crops, disease KB
│   ├── finance/                   # Transactions, budgets, goals
│   ├── health/                    # BP, steps, weight, sleep, sugar
│   └── system/                    # Load monitor, guardian status
├── security/
│   ├── guardian.py                # Daemon + idle-aware scheduler
│   ├── load_monitor.py            # CPU/RAM/IO observer + pattern DB
│   ├── threat_intel.py            # 4-source intel + attack replication
│   ├── scanner.py                 # OSV.dev CVE scanner
│   ├── auditor.py                 # Code audit (bandit + custom)
│   ├── patcher.py                 # Safe auto-patcher
│   ├── watcher.py                 # Pattern updater + anomaly detector
│   └── patterns.json              # 45 injection patterns (auto-updated)
├── crops/
│   ├── pomegranate.json           # Disease + pest + fertilizer KB
│   ├── sugarcane.json
│   └── banana.json
├── inputs/                        # Benchmark test cases (10K cases)
├── scripts/
│   ├── evaluate_assistant_v2.py   # 10K-case LLM evaluator with judge
│   └── generate_mindmap.py        # Generates docs/mindmap.png
└── docs/
    ├── design.md                  # Full system design
    └── mindmap.png                # Architecture mind map
```

---

## Evaluation

- **4,000** hand-crafted test cases (v1)
- **10,000** benchmark cases (v2) - 80 groups by (category x topic x difficulty)
- LLM judge with first-principles scoring: hallucination rate, refusal accuracy, factual grounding
- Adversarial topics: fake RBI circulars, fake medicine, scam detection - must refuse

---

*Built for Ganesh Kalidas - a farmer, investor, and builder from Maharashtra, India.*
