# GK — Personal Life OS

A local, private, multi-module personal assistant. Runs 100% on your machine.

**"Welcome to the future, GK."**

## What is GK?

GK is a router-based personal assistant where each domain (farming, finance, health) is a self-contained module with its own model, memory, and tools. You ask in natural language — GK routes to the right module, blends answers across modules when needed, and never sends your personal data outside your machine.

## Design

See [docs/design.md](docs/design.md) for the full system design, confirmed decisions, and build roadmap.

## Setup

```bash
source ~/envs/evn_gov_schemes/bin/activate
pip install -r requirements.txt
```

## Project Structure

```
gk/
├── docs/           # Design documents
├── modules/        # Individual assistant modules (farming, finance, health, ...)
├── core/           # Router, memory, input layer, diary, knowledge graph
├── main.py         # Entry point
└── requirements.txt
```

## Status

- [ ] Router (qwen2.5:3b vs llama3.2:3b — to be tested)
- [ ] Finance module (v1 — text only)
- [ ] Diary layer
- [ ] Knowledge graph + people ID (InsightFace)
- [ ] Privacy proxy (SearXNG)
- [ ] OPSEC layer

> Farming module is built and lives in the sibling `farming/` project. It will plug in as a module here.
