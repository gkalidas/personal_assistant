"""GK Personal Assistant — terminal chat entry point."""

import os
import time
from dotenv import load_dotenv

load_dotenv()

import httpx
from core import memory
from core.router import dispatch, ROUTER_MODEL, OLLAMA_URL
from core.sanitizer import sanitize_input, redact_pii
from modules.finance.module import FinanceModule, TEXT_MODEL as FINANCE_MODEL
from modules.farming.module import FarmingModule, TEXT_MODEL as FARMING_MODEL


MODULES = {
    "finance": FinanceModule(),
    "farming": FarmingModule(),
}


def _warmup_models() -> None:
    """Load each model into Ollama memory sequentially at startup."""
    # Deduplicate — router and text model may be the same
    models = list(dict.fromkeys([ROUTER_MODEL, FINANCE_MODEL, FARMING_MODEL]))
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
            print(f"ready ({int((time.monotonic()-t0)*1000)}ms)")
        except Exception as e:
            print(f"failed ({e})")


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
}


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
        query = san.query   # use cleaned version for LLM

        context = _build_context(profile)
        event_id = memory.log_query_start(redact_pii(query))
        t0 = time.monotonic()

        try:
            responses = dispatch(query, MODULES, context)
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            memory.log_query_done(event_id, "router", str(e), latency_ms, status="error")
            print(f"\nGK: Something went wrong — {e}")
            continue

        latency_ms = int((time.monotonic() - t0) * 1000)
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
