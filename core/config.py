"""Central configuration — single source of truth for model names and URLs.

All modules import from here instead of re-reading env vars individually.
Override any value by setting the corresponding environment variable.
"""

import os

OLLAMA_URL     = os.getenv("OLLAMA_URL",      "http://localhost:11434")
TEXT_MODEL     = os.getenv("TEXT_MODEL",      "qwen3:1.7b")
ROUTER_MODEL   = os.getenv("ROUTER_MODEL",    "qwen2.5:0.5b")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL",  "qwen2.5:3b")   # backup when TEXT_MODEL fails
VISION_MODEL   = os.getenv("VISION_MODEL",    "moondream")

# Weekly digest email (Gmail SMTP with App Password)
DIGEST_FROM        = os.getenv("DIGEST_FROM",        "")
DIGEST_TO          = os.getenv("DIGEST_TO",          "")
DIGEST_EMAIL_PASS  = os.getenv("DIGEST_EMAIL_PASS",  "")

# SearXNG self-hosted search (optional — empty = use DDGS)
SEARXNG_URL        = os.getenv("SEARXNG_URL",        "").rstrip("/")
