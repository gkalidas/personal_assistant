"""Per-session chat transcripts — one JSONL file per conversation.

Claude/ChatGPT-style external session logs, designed to be greppable and
trivially loadable for offline analysis (behaviour, latency, quality):

    logs/chat_sessions/<session_id>.jsonl

Line types (each line is one JSON object):
  {"type":"session", "id","title","channel","started"}        — first line
  {"type":"meta",    "ts","title"}                            — title renamed
  {"type":"message", "ts","role","content","module","latency_ms","status"}

`channel` is "dashboard" (chatbox) or "cli" (main.py REPL, incl. voice).
Content is PII-redacted before writing, same as the events DB. Files are
append-only; the personalization idle-scan tracks a per-file line cursor so
each run only reads what's new (see user_messages_since).
"""

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import CHAT_SESSIONS_DIR
from core.sanitizer import redact_pii

_lock = threading.Lock()

# session_id -> last title written, so a rename appends one meta line instead
# of one per message. Re-appending a single meta line after a restart is fine.
_titles: dict[str, str] = {}

_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_id(session_id: str) -> str:
    """Sanitize a (browser-supplied) session id into a safe filename stem."""
    sid = _ID_RE.sub("", str(session_id or ""))[:64]
    return sid or "unknown"


def _path(session_id: str) -> Path:
    return Path(CHAT_SESSIONS_DIR) / f"{_safe_id(session_id)}.jsonl"


def _append(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_message(session_id: str, role: str, content: str, *,
                   channel: str = "dashboard", title: str | None = None,
                   module: str | None = None, latency_ms: int | None = None,
                   status: str = "ok") -> None:
    """Append one chat turn to the session's transcript file.

    Creates the file with a session header on first write. Never raises —
    transcript logging must not break the chat path.
    """
    try:
        sid = _safe_id(session_id)
        path = _path(sid)
        now = datetime.now().isoformat(timespec="seconds")
        records: list[dict] = []
        with _lock:
            if not path.exists():
                records.append({"type": "session", "id": sid,
                                "title": title or "", "channel": channel,
                                "started": now})
                _titles[sid] = title or ""
            elif title and _titles.get(sid, title) != title:
                records.append({"type": "meta", "ts": now, "title": title})
            if title:
                _titles[sid] = title
            msg: dict[str, Any] = {"type": "message", "ts": now, "role": role,
                                   "content": redact_pii(content or "")}
            if module:
                msg["module"] = module
            if latency_ms is not None:
                msg["latency_ms"] = latency_ms
            if status != "ok":
                msg["status"] = status
            records.append(msg)
            _append(path, records)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "transcript append failed for session %r", session_id, exc_info=True)


def new_cli_session_id() -> str:
    """Session id for one run of the CLI REPL."""
    return "cli-" + datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"


# ── Readers (offline analysis / personalization scan) ─────────────────────────

def list_sessions() -> list[dict]:
    """Return header info for every stored session, newest file first."""
    out = []
    root = Path(CHAT_SESSIONS_DIR)
    if not root.is_dir():
        return out
    for p in sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                    reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                head = json.loads(f.readline() or "{}")
        except Exception:
            continue
        if head.get("type") == "session":
            head["path"] = str(p)
            out.append(head)
    return out


def read_session(session_id: str) -> list[dict]:
    """Return every record of one session (header, meta and messages)."""
    path = _path(session_id)
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return records


def dashboard_turns() -> list[dict]:
    """Return every dashboard user→assistant exchange as an event-like dict.

    Shape matches rows of the events DB ({ts, module, query, response,
    latency_ms, status}) so the weekly analyser can merge both sources.
    CLI sessions are skipped — those turns already live in the events DB and
    would be double-counted.
    """
    turns: list[dict] = []
    for head in list_sessions():
        if head.get("channel") == "cli":
            continue
        pending = None   # last user message still awaiting its reply
        for rec in read_session(head.get("id") or ""):
            if rec.get("type") != "message":
                continue
            if rec.get("role") == "user":
                pending = rec
            elif rec.get("role") == "assistant" and pending is not None:
                turns.append({
                    "ts": rec.get("ts") or pending.get("ts") or "",
                    "module": rec.get("module") or "unknown",
                    "query": pending.get("content") or "",
                    "response": rec.get("content") or "",
                    "latency_ms": rec.get("latency_ms"),
                    "status": rec.get("status", "ok"),
                })
                pending = None
    return turns


def user_messages_since(cursor: dict[str, int] | None,
                        max_messages: int = 400) -> tuple[list[str], dict[str, int]]:
    """Return new dashboard user messages for the personalization idle-scan.

    `cursor` maps session_id -> number of lines already consumed per file;
    only lines past that point are read, so a run with no new chat is a no-op.
    CLI sessions are skipped — those queries already land in the events DB and
    are mined from there (scanning both would double-count them).
    """
    cursor = dict(cursor or {})
    texts: list[str] = []
    for head in list_sessions():
        sid = head.get("id") or ""
        if head.get("channel") == "cli":
            continue
        seen = int(cursor.get(sid) or 0)
        consumed = seen
        try:
            with open(head["path"], encoding="utf-8") as f:
                for n, line in enumerate(f, start=1):
                    if n <= seen:
                        continue
                    if len(texts) >= max_messages:
                        break   # leave the rest unconsumed for the next run
                    consumed = n
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") == "message" and rec.get("role") == "user":
                        if rec.get("content"):
                            texts.append(rec["content"])
        except OSError:
            continue
        cursor[sid] = consumed
    return texts, cursor
