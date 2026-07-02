"""
Central LLM client — wraps Ollama with:

  • Streaming output (tokens printed live when stream_to_stdout=True)
  • Automatic fallback to FALLBACK_MODEL when primary model times-out or errors
  • Uncertainty detection — is_uncertain(text) → bool
  • Typing spinner for non-streaming calls

Usage
-----
  from core.llm import call, is_uncertain

  # Streaming (search/chat responses):
  text = call(messages, stream_to_stdout=True, prefix="GK [search]: ")

  # Non-streaming with JSON (action routing):
  json_text = call(messages, format="json", think=False)

  # Uncertainty check:
  if is_uncertain(text):
      log_mistake(...)
"""

import json
import logging
import sys
import threading
import time
from typing import Iterator, Optional

import httpx

from core.config import (
    OLLAMA_URL, TEXT_MODEL, FALLBACK_MODEL, CHAT_KEEP_ALIVE, NUM_CTX,
)

log = logging.getLogger(__name__)


# ── Context-window helpers ──────────────────────────────────────────────────────

def _with_ctx(options: Optional[dict]) -> dict:
    """Ensure every request asks Ollama for our NUM_CTX window.

    Ollama defaults to a tiny 4096-token window when num_ctx is unset, silently
    truncating older turns. We set it unless a caller explicitly overrides.
    """
    opts = dict(options or {})
    opts.setdefault("num_ctx", NUM_CTX)
    return opts


def _log_usage(model: str, prompt_tokens, eval_tokens) -> None:
    """Log token usage for a completed call and warn as we approach the window.

    Ollama returns prompt_eval_count (context tokens) and eval_count (generated
    tokens) on the final response chunk. remaining = NUM_CTX − their sum is the
    real "tokens left before truncation/hallucination" figure.
    """
    if prompt_tokens is None and eval_tokens is None:
        return
    total = (prompt_tokens or 0) + (eval_tokens or 0)
    remaining = NUM_CTX - total
    pct = (total / NUM_CTX * 100) if NUM_CTX else 0
    log.info(
        "llm %s tokens prompt=%s gen=%s total=%s/%s (%.0f%% used, %s remaining)",
        model, prompt_tokens, eval_tokens, total, NUM_CTX, pct, remaining,
    )
    if remaining < NUM_CTX * 0.1:
        log.warning(
            "llm %s near context limit: %s tokens left of %s — oldest turns risk "
            "truncation. Raise NUM_CTX or lean on history compaction.",
            model, remaining, NUM_CTX,
        )

# ── Uncertainty phrases ────────────────────────────────────────────────────────
_UNCERTAIN = {
    "i don't know", "i do not know", "i'm not sure", "not sure",
    "cannot answer", "can't answer", "unable to answer",
    "i don't have", "i do not have", "no information",
    "i cannot help", "i can't help", "outside my knowledge",
    "beyond my training", "cannot determine", "i have no",
    "i'm unable", "unclear to me",
    # Marathi / Hindi
    "माहीत नाही", "नाही माहीत", "pata nahi", "पता नहीं",
}

# ── Typing spinner ─────────────────────────────────────────────────────────────
_FRAMES = "⣾⣽⣻⢿⡿⣟⣯⣷"


class Spinner:
    """Thread-safe CLI spinner. Call .stop() when done."""
    def __init__(self, msg: str = "  Thinking"):
        """Start the spinner animation in a background daemon thread."""
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, args=(msg,), daemon=True)
        self._t.start()

    def _run(self, msg: str):
        """Animate the spinner frames until stopped, then clear the line."""
        idx = 0
        while not self._stop.is_set():
            sys.stdout.write(f"\r{_FRAMES[idx % len(_FRAMES)]} {msg}…")
            sys.stdout.flush()
            self._stop.wait(0.09)
            idx += 1
        sys.stdout.write("\r" + " " * 30 + "\r")
        sys.stdout.flush()

    def stop(self):
        """Signal the animation thread to stop and wait briefly for it to exit."""
        self._stop.set()
        self._t.join(timeout=0.5)


# ── Core call ─────────────────────────────────────────────────────────────────

def call(
    messages: list[dict],
    model: Optional[str] = None,
    stream_to_stdout: bool = False,
    timeout: float = 120.0,
    prefix: str = "",
    format: Optional[str] = None,
    think: bool = False,
    use_fallback: bool = True,
    options: Optional[dict] = None,
) -> str:
    """
    Call Ollama. Returns full response text.

    Parameters
    ----------
    stream_to_stdout : bool
        Print tokens as they arrive. Ignored when format="json".
    prefix : str
        Text printed before the first streaming token (e.g. "GK [search]: ").
    format : str | None
        "json" forces non-streaming + JSON output schema.
    use_fallback : bool
        Retry with FALLBACK_MODEL if primary fails.
    options : dict | None
        Extra Ollama sampling options (e.g. {"num_predict": 256}) to cap
        generation length and speed up short replies.
    """
    mdl = model or TEXT_MODEL
    do_stream = stream_to_stdout and not format   # can't stream + json reliably

    payload: dict = {
        "model": mdl,
        "messages": messages,
        "stream": do_stream,
        "think": think,
        # Hold the model resident between calls so intermittent chats don't pay a
        # cold reload (Ollama's default keep_alive is only 5m).
        "keep_alive": CHAT_KEEP_ALIVE,
    }
    if format:
        payload["format"] = format
    payload["options"] = _with_ctx(options)

    return _call_with_fallback(payload, do_stream, prefix, timeout, use_fallback)


def _call_with_fallback(
    payload: dict, do_stream: bool, prefix: str, timeout: float, use_fallback: bool
) -> str:
    """Dispatch the request; on failure retry once with FALLBACK_MODEL (if allowed)."""
    mdl = payload["model"]
    t0 = time.monotonic()
    try:
        result = _dispatch(payload, do_stream, prefix, timeout)
        log.debug("llm %s %.0fms stream=%s", mdl, (time.monotonic() - t0) * 1000, do_stream)
        return result
    except Exception as exc:
        log.warning("llm %s failed %.0fms: %s", mdl, (time.monotonic() - t0) * 1000, exc)
        if not use_fallback or mdl == FALLBACK_MODEL:
            raise

    log.info("retrying with fallback model %s", FALLBACK_MODEL)
    payload["model"] = FALLBACK_MODEL
    try:
        result = _dispatch(payload, do_stream, prefix, timeout)
        log.info("fallback succeeded: %s", FALLBACK_MODEL)
        return result
    except Exception as exc2:
        log.error("fallback %s also failed: %s", FALLBACK_MODEL, exc2)
        raise


def _dispatch(payload: dict, do_stream: bool, prefix: str, timeout: float) -> str:
    """Send one chat request — streaming or buffered — and return the response text."""
    if do_stream:
        return _stream(payload, prefix, timeout)
    resp = httpx.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    _log_usage(payload["model"], data.get("prompt_eval_count"), data.get("eval_count"))
    return data["message"]["content"]


def _iter_stream(payload: dict, timeout: float) -> Iterator[str]:
    """Yield content tokens from an Ollama streaming chat response."""
    with httpx.stream(
        "POST", f"{OLLAMA_URL}/api/chat", json=payload, timeout=timeout
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            token = chunk.get("message", {}).get("content", "")
            if token:
                yield token
            if chunk.get("done"):
                _log_usage(
                    payload["model"],
                    chunk.get("prompt_eval_count"),
                    chunk.get("eval_count"),
                )
                break


def _stream(payload: dict, prefix: str, timeout: float) -> str:
    """Stream Ollama SSE, print live, return accumulated text."""
    if prefix:
        print(prefix, end="", flush=True)
    parts: list[str] = []
    for token in _iter_stream(payload, timeout):
        sys.stdout.write(token)
        sys.stdout.flush()
        parts.append(token)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(parts)


def stream_tokens(
    messages: list[dict],
    model: Optional[str] = None,
    timeout: float = 120.0,
    think: bool = False,
    options: Optional[dict] = None,
) -> Iterator[str]:
    """Yield reply tokens as they arrive from Ollama.

    For web/SSE callers that forward tokens to the browser. Unlike call(), this
    does no stdout printing and no fallback — the caller handles errors mid-stream.
    """
    payload: dict = {
        "model": model or TEXT_MODEL,
        "messages": messages,
        "stream": True,
        "think": think,
        "keep_alive": CHAT_KEEP_ALIVE,
        "options": _with_ctx(options),
    }
    yield from _iter_stream(payload, timeout)


# ── Conversation compaction ─────────────────────────────────────────────────────

_SUMMARY_SYSTEM = (
    "You compress a chat between GK (an assistant) and its user into terse notes. "
    "In 2-4 short sentences capture: what the user is trying to do, any decisions "
    "made, and key facts/values mentioned. Write notes, not prose. No preamble."
)


def summarize_history(turns: list[dict], timeout: float = 30.0) -> str:
    """Compress older chat turns into a short recap so context survives truncation.

    Used by the dashboard to keep a running summary of dropped turns. Runs on the
    warm TEXT_MODEL with a small num_predict cap; returns "" on any failure so the
    caller can degrade gracefully to recent-turns-only.
    """
    if not turns:
        return ""
    convo = "\n".join(f"{t.get('role', '?')}: {t.get('content', '')}" for t in turns)
    messages = [
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {"role": "user", "content": convo},
    ]
    try:
        return call(
            messages, think=False, use_fallback=False,
            timeout=timeout, options={"num_predict": 220},
        ).strip()
    except Exception as exc:
        log.warning("history compaction failed: %s", exc)
        return ""


# ── Uncertainty check ─────────────────────────────────────────────────────────

def is_uncertain(text: str) -> bool:
    """Return True when the model admits it doesn't know the answer."""
    lower = text.lower()
    return any(phrase in lower for phrase in _UNCERTAIN)


# ── Data-operation guard ──────────────────────────────────────────────────────

_DATA_VERBS = {
    "log", "add", "create", "delete", "remove", "update", "set", "save",
    "approve", "plant", "harvest", "record", "mark",
}


def is_data_operation(query: str) -> bool:
    """Return True for write operations — don't fallback to search for these."""
    first = query.strip().lower().split()[:2]
    return bool(_DATA_VERBS.intersection(first))
