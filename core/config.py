"""Central configuration — single source of truth for model names and URLs.

All modules import from here instead of re-reading env vars individually.
Override any value by setting the corresponding environment variable.
"""

import os
from pathlib import Path

OLLAMA_URL     = os.getenv("OLLAMA_URL",      "http://localhost:11434")
TEXT_MODEL     = os.getenv("TEXT_MODEL",      "qwen2.5:1.5b")  # faster + better persona/multilingual than qwen3:1.7b (benchmarked)
# Context window (tokens) requested from Ollama per chat call. Without this,
# Ollama silently defaults to a tiny 4096-token window and truncates the oldest
# turns — the assistant "forgets" mid-conversation and starts confabulating.
# qwen2.5:1.5b supports up to 32768; 8192 gives ample room for a full chat plus
# the compacted-history summary while keeping the KV cache small on this
# RAM-constrained host (larger windows thrash swap and slow every token).
# GUARDRAIL: keep NUM_CTX <= the model's native context (qwen2.5:1.5b = 32768,
# qwen3:1.7b = 40960). Ollama clamps anything larger to the model max, so a value
# above the ceiling silently buys you nothing while still reserving RAM.
NUM_CTX        = int(os.getenv("NUM_CTX",      "8192"))
ROUTER_MODEL   = os.getenv("ROUTER_MODEL",    "qwen2.5:0.5b")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL",  "qwen2.5:3b")   # backup when TEXT_MODEL fails
VISION_MODEL   = os.getenv("VISION_MODEL",    "moondream")
# How long Ollama holds the chat/router models resident after a request. Without
# this, Ollama's 5-minute default unloads them, so any chat after a short idle
# gap pays a full cold reload — the dominant cause of a "slow" first reply. 30m
# keeps the small qwen models warm across typical bursts of conversation.
CHAT_KEEP_ALIVE = os.getenv("CHAT_KEEP_ALIVE", "30m")
# Vision captioning is slow to COLD-LOAD (~100s for moondream on CPU) but fast
# once warm (~6s). The timeout must exceed a cold load or the request is
# abandoned mid-load and the model never warms up (a timeout death-spiral).
# keep_alive holds the model in memory so subsequent captions stay fast.
VISION_TIMEOUT    = float(os.getenv("VISION_TIMEOUT",    "180"))   # seconds
VISION_KEEP_ALIVE = os.getenv("VISION_KEEP_ALIVE", "30m")

# Weekly digest email (Gmail SMTP with App Password)
DIGEST_FROM        = os.getenv("DIGEST_FROM",        "")
DIGEST_TO          = os.getenv("DIGEST_TO",          "")
DIGEST_EMAIL_PASS  = os.getenv("DIGEST_EMAIL_PASS",  "")

# SearXNG self-hosted search (optional — empty = use DDGS)
SEARXNG_URL        = os.getenv("SEARXNG_URL",        "").rstrip("/")

# Local music collection the assistant scans and plays before searching online.
# Drop audio files (optionally in genre subfolders like ghazals/, sufi/) here.
MUSIC_DIR          = os.getenv("MUSIC_DIR", str(Path.home() / "Music" / "gk"))

# Per-session chat transcripts (Claude/ChatGPT-style JSONL files) — one file
# per conversation, for offline behaviour/latency analysis and the
# personalization idle-scan. See core/chat_transcript.py.
CHAT_SESSIONS_DIR  = os.getenv("CHAT_SESSIONS_DIR",
                               str(Path(__file__).resolve().parent.parent / "logs" / "chat_sessions"))

# When a module's reply looks uncertain ("I don't know"), optionally run a web
# search and append its result as a second block. Default OFF so one query →
# one module's output. Set UNCERTAINTY_SEARCH_FALLBACK=1 to re-enable.
UNCERTAINTY_SEARCH_FALLBACK = os.getenv("UNCERTAINTY_SEARCH_FALLBACK", "0") not in ("0", "", "false", "False")
