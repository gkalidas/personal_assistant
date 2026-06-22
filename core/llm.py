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
from typing import Optional

import httpx

from core.config import OLLAMA_URL, TEXT_MODEL, FALLBACK_MODEL

log = logging.getLogger(__name__)

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
    """
    mdl = model or TEXT_MODEL
    do_stream = stream_to_stdout and not format   # can't stream + json reliably

    payload: dict = {
        "model": mdl,
        "messages": messages,
        "stream": do_stream,
        "think": think,
    }
    if format:
        payload["format"] = format

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
    return resp.json()["message"]["content"]


def _stream(payload: dict, prefix: str, timeout: float) -> str:
    """Stream Ollama SSE, print live, return accumulated text."""
    if prefix:
        print(prefix, end="", flush=True)
    parts: list[str] = []
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
                sys.stdout.write(token)
                sys.stdout.flush()
                parts.append(token)
            if chunk.get("done"):
                break
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(parts)


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
