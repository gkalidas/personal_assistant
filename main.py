"""GK Personal Assistant — terminal chat entry point."""

import logging
import os
import time
from dotenv import load_dotenv

load_dotenv()

# Voice-input commands that trigger mic recording
_VOICE_TRIGGERS = {"/voice", "/mic", "/speak", "/v"}

from core.log import setup_logging
setup_logging()

log = logging.getLogger("gk.main")

import httpx
from core import memory
from core.config import OLLAMA_URL, TEXT_MODEL
from core.router import dispatch, ROUTER_MODEL
from core.sanitizer import sanitize_input, redact_pii
from core.doc_reader import read_document, describe as doc_describe
from modules.finance.module import FinanceModule
from modules.farming.module import FarmingModule
from modules.health.module import HealthModule
from modules.system.module import SystemModule
from modules.diary.module import DiaryModule


MODULES = {
    "finance": FinanceModule(),
    "farming": FarmingModule(),
    "health":  HealthModule(),
    "system":  SystemModule(),
    "diary":   DiaryModule(),
}


def _warmup_models() -> None:
    """Load each model into Ollama memory sequentially at startup."""
    models = list(dict.fromkeys([ROUTER_MODEL, TEXT_MODEL]))
    for model in models:
        print(f"  Loading {model}...", end=" ", flush=True)
        t0 = time.monotonic()
        try:
            httpx.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "keep_alive": "10m",
                },
                timeout=120.0,
            )
            ms = int((time.monotonic() - t0) * 1000)
            print(f"ready ({ms}ms)")
            log.info("warmup %s ready in %dms", model, ms)
        except Exception as e:
            print(f"failed ({e})")
            log.error("warmup %s failed: %s", model, e)


def _build_context(profile: dict) -> dict:
    ctx = {"profile": profile}
    farms = profile.get("farms", [])
    default_label = profile.get("preferences", {}).get("default_farm")
    farm = next((f for f in farms if f.get("label") == default_label), None) or (farms[0] if farms else None)
    if farm:
        ctx["default_farm"] = farm

    homes = profile.get("homes", [])
    default_home_label = profile.get("preferences", {}).get("default_home")
    home = next((h for h in homes if h.get("label") == default_home_label), None) or (homes[0] if homes else None)
    if home:
        ctx["default_home"] = home

    return ctx

GOODBYE = {"exit", "quit", "bye", "/exit", "/quit"}

# Words that mean "yes, do what you just offered"
_CONFIRMATIONS = {"sure", "yes", "yeah", "yep", "ok", "okay", "go ahead", "do it", "haan", "ha"}

# Maps each follow-up prompt to the query GK should run when user confirms
_FOLLOW_UP_ACTIONS = {
    "Want the 7-day forecast or spray safety check?":        "7 day weather forecast",
    "Want me to check if it's safe to spray tomorrow?":      "is it safe to spray tomorrow",
    "Want to check how this affects your monthly budget?":   "show my monthly budget status",
    "Would you like to see where you can cut back this month?": "show spending breakdown this month",
    "Want me to suggest which categories to trim?":          "suggest which budget categories to cut",
    "Want to set a monthly savings amount toward this goal?":"how should I save for my goal",
    "Want to log what treatment you applied?":               "log a spray treatment",
    "Noticed any missed sprays in the schedule?":            "show spray history",
    "Want to see crop history or rainfall since planting?":  "show crop weather history",
    "Want to pull weather history since planting date?":     "show weather history since planting",
    "Want disease risk advice based on this soil type?":     "what disease risk does this soil have",
    "Want to log a treatment or observation?":               "log a field observation",
    "Want to compare prices across more markets or check a different district?": "show pomegranate prices in all Maharashtra markets",
    "Say \"approve diary\" to mark this week's draft as final.":                "approve diary",
}


_DOC_EXTS = {".pdf", ".docx", ".xlsx", ".txt", ".md", ".csv"}
_DOC_PATH_RE = __import__("re").compile(r"(?:^|(?<=\s))(/[\w/._~-]+\.(?:pdf|docx|xlsx|txt|md|csv)|~/[\w/._-]+\.(?:pdf|docx|xlsx|txt|md|csv))", __import__("re").IGNORECASE)


def _inject_doc_context(query: str) -> str:
    """
    Detect file paths in query, extract document text, inject as context.
    e.g. "summarize /home/ganesh/report.pdf" → "summarize [report.pdf]: <text>"
    """
    match = _DOC_PATH_RE.search(query)
    if not match:
        return query
    path = match.group(1)
    result = read_document(path)
    if result["error"]:
        print(f"\n  [doc] {result['error']}")
        return query
    print(f"\n  [doc] {doc_describe(result)}")
    snippet = result["text"][:8000]  # cap context passed to LLM
    return query.replace(path, f"[document: {__import__('pathlib').Path(path).name}]\n\n{snippet}\n\n")


def print_response(responses):
    for r in responses:
        print(f"\nGK [{r.module}]: {r.text}")
        if r.follow_up:
            print(f"\n  → {r.follow_up}")


def main():
    memory.init_db()
    profile = memory.load_profile()

    print("Welcome to the future, GK.")
    print("Warming up models...")
    _warmup_models()
    print('Ready. Type your query. "exit" to quit.\n')

    pending_follow_up: str | None = None   # last follow-up prompt shown

    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGK: Goodbye.")
            break

        if not query:
            continue
        if query.lower() in GOODBYE:
            print("GK: Goodbye.")
            break

        # ── Voice input ───────────────────────────────────────────────────────
        parts = query.split()
        if parts[0].lower() in _VOICE_TRIGGERS:
            try:
                secs = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
            except (IndexError, ValueError):
                secs = 5
            from core.audio import record_and_transcribe, is_available as audio_ok
            if not audio_ok():
                print("  [voice] whisper not installed — run: pip install openai-whisper")
                continue
            print(f"  [voice] recording {secs}s … speak now")
            transcribed = record_and_transcribe(seconds=secs)
            if not transcribed:
                print("  [voice] nothing transcribed")
                continue
            print(f"  [voice] → {transcribed}")
            query = transcribed

        # ── Follow-up confirmation ─────────────────────────────────────────────
        if query.lower() in _CONFIRMATIONS and pending_follow_up:
            mapped = _FOLLOW_UP_ACTIONS.get(pending_follow_up)
            if mapped:
                print(f"  (following up: {mapped})")
                query = mapped
            pending_follow_up = None

        # ── Sanitize input ────────────────────────────────────────────────────
        san = sanitize_input(query)
        if san.warnings:
            for w in san.warnings:
                print(f"  [input] {w}")
                log.warning("sanitizer: %s", w)
        query = san.query   # use cleaned version for LLM

        # Auto-inject document content if query contains a file path
        query = _inject_doc_context(query)

        context = _build_context(profile)
        event_id = memory.log_query_start(redact_pii(query))
        t0 = time.monotonic()
        log.info("query id=%d q=%r", event_id, query[:120])

        try:
            responses = dispatch(query, MODULES, context)
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            memory.log_query_done(event_id, "router", str(e), latency_ms, status="error")
            log.error("dispatch failed id=%d latency=%dms: %s", event_id, latency_ms, e, exc_info=True)
            print(f"\nGK: Something went wrong — {e}")
            continue

        latency_ms = int((time.monotonic() - t0) * 1000)
        log.info("response id=%d modules=%s latency=%dms",
                 event_id, [r.module for r in responses], latency_ms)
        print_response(responses)

        # Track last follow-up so user can confirm with "sure/yes"
        pending_follow_up = None
        for r in responses:
            if r.follow_up:
                pending_follow_up = r.follow_up
            memory.log_query_done(
                event_id=event_id,
                module=r.module,
                response=redact_pii(r.text),
                latency_ms=latency_ms,
                metadata=r.data,
            )

        print()


if __name__ == "__main__":
    main()
